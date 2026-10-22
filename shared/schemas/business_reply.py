"""普通 SMTP 回复的不可变传输契约；不代表联系人或 Campaign 已获授权。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from shared.errors import ValidationError

_MAILBOX = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
)
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}")


def validate_reply_address(value: object) -> None:
    """只接受单一 ASCII 裸邮箱，禁止显示名及协议控制字符。"""
    if not isinstance(value, str) or len(value) > 254 or not _MAILBOX.fullmatch(value):
        raise ValidationError("回复邮箱无效")
    local = value.rsplit("@", 1)[0]
    if local.startswith(".") or local.endswith(".") or ".." in local:
        raise ValidationError("回复邮箱无效")


def validate_reply_message_id(value: object) -> None:
    """校验带尖括号的有界 Message-ID；保留大小写供线程精确比较。"""
    if (
        not isinstance(value, str)
        or not value.startswith("<")
        or not value.endswith(">")
    ):
        raise ValidationError("回复线程标识无效")
    # Message-ID 大小写有意义；这里只在校验域名语法时转小写。
    validate_reply_address(value[1:-1].lower())


def parse_reply_references(value: str) -> tuple[str, ...]:
    """完整解析 References，不丢弃非法残余，不允许无界线程。"""
    tokens = value.split()
    if not tokens or len(tokens) > 20:
        raise ValidationError("回复线程无效")
    for token in tokens:
        validate_reply_message_id(token)
    return tuple(dict.fromkeys(tokens))


def _validate_text(value: object, *, limit: int, body: bool = False) -> None:
    if not isinstance(value, str) or not value:
        raise ValidationError("回复内容无效")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        raise ValidationError("回复内容无效") from None
    if size > limit or any(
        ord(char) == 127 or (ord(char) < 32 and not (body and char in "\n\t"))
        for char in value
    ):
        raise ValidationError("回复内容无效")


@dataclass(frozen=True)
class SMTPReplyRequest:
    """绑定发件身份、内容与原线程；调用方仍须经过 Gateway 业务门禁。"""

    from_address: str = field(repr=False)
    recipient_address: str = field(repr=False)
    subject: str = field(repr=False)
    body: str = field(repr=False)
    deterministic_message_id: str = field(repr=False)
    idempotency_header: str = field(repr=False)
    in_reply_to: str = field(repr=False)
    references: tuple[str, ...] = field(repr=False)

    def __post_init__(self) -> None:
        """拒绝注入、可变线程与凭证形状的幂等标记，不回显输入。"""
        validate_reply_address(self.from_address)
        validate_reply_address(self.recipient_address)
        _validate_text(self.subject, limit=998)
        _validate_text(self.body, limit=1_048_576, body=True)
        validate_reply_message_id(self.in_reply_to)
        if not isinstance(self.deterministic_message_id, str):
            raise ValidationError("回复消息标识无效")
        validate_reply_message_id(f"<{self.deterministic_message_id}>")
        if (
            not isinstance(self.idempotency_header, str)
            or not _SAFE_ID.fullmatch(self.idempotency_header)
            or any(
                marker in self.idempotency_header.lower()
                for marker in ("bearer", "secret", "token", "password", "authorization")
            )
        ):
            raise ValidationError("回复幂等标记无效")
        if (
            type(self.references) is not tuple
            or not 1 <= len(self.references) <= 20
            or self.references[-1] != self.in_reply_to
        ):
            raise ValidationError("回复线程无效")
        for reference in self.references:
            validate_reply_message_id(reference)
        if len(set(self.references)) != len(self.references):
            raise ValidationError("回复线程无效")

    @property
    def content_hash(self) -> str:
        """按固定 JSON 编码计算内容摘要，绑定双方地址、正文、主题与线程。"""
        payload = [
            self.from_address,
            self.recipient_address,
            self.subject,
            self.body,
            self.in_reply_to,
            self.references,
        ]
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
