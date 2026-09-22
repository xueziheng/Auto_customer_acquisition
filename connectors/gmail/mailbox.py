"""全邮箱只读同步；与旧 30 天外发关联通道独立。"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from shared.schemas.mailbox import (
    MailAttachment,
    MailboxFailure,
    MailboxMessage,
    MailboxPage,
    MailboxPhase,
    MailboxProvider,
)

PAGE_SIZE = 25
_ID = re.compile(r"[a-f0-9]{1,64}")


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "head"}:
            self.hidden += 1
        if not self.hidden and tag in {"p", "div", "br", "tr", "li"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "head"}:
            self.hidden = max(0, self.hidden - 1)
        if not self.hidden and tag in {"p", "div", "tr", "li"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def parse_message(raw: dict[str, Any]) -> MailboxMessage:
    """仅做 MIME 技术投影；原始 JSON 留在本人镜像中，可按 hash 校验。"""
    try:
        root = raw.get("payload", {})
        headers = {
            str(h["name"]).lower(): str(h["value"]) for h in root.get("headers", [])
        }
        plain: list[str] = []
        html: list[str] = []
        attachments: list[MailAttachment] = []

        def visit(part, depth=0):
            if depth > 40:
                raise ValueError()
            body = part.get("body", {})
            mime = str(part.get("mimeType", ""))
            filename = str(part.get("filename", ""))
            if filename or body.get("attachmentId"):
                attachments.append(
                    MailAttachment(
                        filename=filename or "未命名附件",
                        mime_type=mime,
                        size=int(body.get("size", 0)),
                    )
                )
            elif mime in {"text/plain", "text/html"} and body.get("data"):
                encoded = body["data"]
                decoded = base64.b64decode(
                    encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True
                )
                local_headers = {
                    h["name"].lower(): h["value"] for h in part.get("headers", [])
                }
                charset = re.search(
                    r'charset\s*=\s*"?([^;"\s]+)',
                    local_headers.get("content-type", ""),
                    re.IGNORECASE,
                )
                text = decoded.decode(
                    charset.group(1) if charset else "utf-8", errors="replace"
                )
                (plain if mime == "text/plain" else html).append(text)
            for child in part.get("parts", []):
                visit(child, depth + 1)

        visit(root)
        if plain:
            body_text = "\n".join(plain)
        else:
            parser = _Text()
            parser.feed("\n".join(html))
            body_text = "".join(parser.parts).strip()
        original = json.dumps(
            raw, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        return MailboxMessage(
            message_id=raw["id"],
            thread_id=raw["threadId"],
            occurred_at=datetime.fromtimestamp(int(raw["internalDate"]) // 1000, UTC),
            labels=raw.get("labelIds", []),
            subject=headers.get("subject", "（无主题）"),
            sender=headers.get("from", ""),
            recipients=headers.get("to", ""),
            snippet=raw.get("snippet", ""),
            body_text=body_text,
            attachments=attachments,
            source_sha256=hashlib.sha256(original.encode()).hexdigest(),
            raw=raw,
        )
    except Exception:  # noqa: BLE001 - 凭证和 Provider 原文不得越过安全边界
        raise MailboxFailure("invalid_response") from None


class _Cursor(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    v: int = Field(default=1, ge=1, le=1)
    phase: MailboxPhase = "backfill"
    anchor: str = Field(pattern=r"^[0-9]+$")
    page: str | None = Field(default=None, max_length=32000, repr=False)
    offset: int = Field(default=0, ge=0, strict=True)
    email: str = Field(max_length=320, repr=False)


class GmailMailboxReader:
    def __init__(self, provider: MailboxProvider, expected_email: str):
        self.provider, self.expected_email = provider, expected_email.casefold()

    async def fetch(self, cursor: str | None) -> MailboxPage:
        """每次核验账号；只在本页完整成功时交付新的检查点。"""
        profile = await self.provider.get("profile", {})
        if str(profile.get("emailAddress", "")).casefold() != self.expected_email:
            raise MailboxFailure("account_mismatch")
        try:
            state = (
                _Cursor.model_validate_json(cursor)
                if cursor
                else _Cursor(
                    anchor=str(profile["historyId"]), email=self.expected_email
                )
            )
            if state.email != self.expected_email:
                raise ValueError()
        except Exception:  # noqa: BLE001 - 凭证和 Provider 原文不得越过安全边界
            raise MailboxFailure("invalid_response") from None
        is_full = state.phase == "backfill"
        params = {"maxResults": str(PAGE_SIZE)}
        if state.page:
            params["pageToken"] = state.page
        if is_full:
            params["includeSpamTrash"] = "true"
        else:
            params["startHistoryId"] = state.anchor
        try:
            result: dict[str, Any] = await self.provider.get(
                "messages" if is_full else "history", params
            )
        except MailboxFailure as error:
            if not is_full and error.code == "history_expired":
                state = _Cursor(
                    anchor=str(profile["historyId"]), email=self.expected_email
                )
                return MailboxPage(
                    cursor=state.model_dump_json(), phase="backfill", reset=True
                )
            raise
        refs: list[str] = []
        try:
            if is_full:
                refs = [m["id"] for m in result.get("messages", [])]
            else:
                for entry in result.get("history", []):
                    for kind in (
                        "messagesAdded",
                        "messagesDeleted",
                        "labelsAdded",
                        "labelsRemoved",
                    ):
                        refs.extend(
                            item["message"]["id"] for item in entry.get(kind, [])
                        )
            refs = list(dict.fromkeys(refs))
            if any(not isinstance(i, str) or _ID.fullmatch(i) is None for i in refs):
                raise ValueError()
        except Exception:  # noqa: BLE001 - 凭证和 Provider 原文不得越过安全边界
            raise MailboxFailure("invalid_response") from None
        offset = state.offset
        messages: list[MailboxMessage] = []
        deleted: list[str] = []
        for mid in refs[offset : offset + PAGE_SIZE]:
            try:
                raw = await self.provider.get(f"messages/{mid}", {"format": "full"})
                message = parse_message(raw)
                if message.message_id != mid:
                    raise MailboxFailure("invalid_response")
                messages.append(message)
            except MailboxFailure as error:
                if error.code != "message_deleted":
                    raise
                deleted.append(mid)
        complete = False
        if offset + PAGE_SIZE < len(refs):
            state.offset += PAGE_SIZE
            if not is_full:
                state.phase = "catch_up"
        else:
            state.offset = 0
            state.page = result.get("nextPageToken")
            if state.page and not is_full:
                state.phase = "catch_up"
            if not state.page:
                if is_full:
                    complete = True
                    state.phase = "catch_up"
                else:
                    anchor = str(result.get("historyId", ""))
                    if not anchor.isdigit():
                        raise MailboxFailure("invalid_response")
                    state.phase, state.anchor = "synced", anchor
        return MailboxPage(
            cursor=state.model_dump_json(),
            phase=state.phase,
            messages=messages,
            deleted_ids=deleted,
            full_scan_complete=complete,
        )
