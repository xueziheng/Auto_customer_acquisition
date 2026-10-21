"""普通回复的来源头解析；邮件正文不是收件人或线程身份依据。"""

from __future__ import annotations

from dataclasses import dataclass, field
from email.headerregistry import AddressHeader
from email.parser import BytesHeaderParser
from email.policy import default

from shared.errors import ValidationError
from shared.schemas.business_reply import (
    parse_reply_references,
    validate_reply_address,
    validate_reply_message_id,
)

_MAX_HEADERS = 32_768


@dataclass(frozen=True)
class ReplyHeaderFacts:
    """仅承载来源头事实；不声明邮箱可达性或业务发送授权。"""

    from_address: str = field(repr=False)
    reply_to_address: str | None = field(repr=False)
    recipient_address: str = field(repr=False)
    message_id: str = field(repr=False)
    references: tuple[str, ...] = field(repr=False)


def parse_reply_headers(raw: bytes, *, mailbox_address: str) -> ReplyHeaderFacts:
    """只接受发给绑定邮箱的明确人工来信，拒绝歧义、自动信和列表信。"""
    validate_reply_address(mailbox_address)
    if not isinstance(raw, bytes):
        raise ValidationError("回复来源无效")
    head, separator, _ = raw[: _MAX_HEADERS + 4].partition(b"\r\n\r\n")
    if not separator or len(head) > _MAX_HEADERS:
        raise ValidationError("回复来源无效")
    if any(byte == 127 or (byte < 32 and byte not in (9, 10, 13)) for byte in head):
        raise ValidationError("回复来源无效")
    try:
        headers = BytesHeaderParser(policy=default).parsebytes(head + b"\r\n\r\n")
        if headers.defects:
            raise ValidationError("回复来源无效")
        for name, header in headers.items():
            values = headers.get_all(name, [])
            if (
                (
                    name.lower()
                    in {
                        "from",
                        "to",
                        "reply-to",
                        "message-id",
                        "references",
                        "in-reply-to",
                        "subject",
                        "auto-submitted",
                        "precedence",
                        "content-type",
                        "mime-version",
                        "sender",
                        "return-path",
                    }
                    and len(values) != 1
                )
                or getattr(header, "defects", False)
                or any(
                    ord(char) == 127 or (ord(char) < 32 and char != "\t")
                    for char in str(header)
                )
            ):
                raise ValidationError("回复来源无效")
            if name.lower().startswith("list-"):
                raise ValidationError("回复来源无效")
        auto = headers.get("Auto-Submitted")
        if auto is not None and str(auto).strip().lower() != "no":
            raise ValidationError("回复来源无效")
        if str(headers.get("Precedence", "")).strip().lower() in {
            "bulk",
            "list",
            "junk",
        }:
            raise ValidationError("回复来源无效")
        if headers.get_content_type() in {
            "multipart/report",
            "message/delivery-status",
            "message/disposition-notification",
        }:
            raise ValidationError("回复来源无效")

        def addresses(name: str) -> tuple[str, ...]:
            value = headers[name]
            if not isinstance(value, AddressHeader) or any(
                group.display_name is not None for group in value.groups
            ):
                raise ValidationError("回复来源无效")
            result = tuple(address.addr_spec for address in value.addresses)
            if not result:
                raise ValidationError("回复来源无效")
            for address in result:
                validate_reply_address(address)
            return result

        sender = addresses("From")
        recipients = addresses("To")
        reply = addresses("Reply-To") if headers.get("Reply-To") is not None else None
        if (
            len(sender) != 1
            or (reply is not None and len(reply) != 1)
            or mailbox_address not in recipients
        ):
            raise ValidationError("回复来源无效")
        recipient = reply[0] if reply else sender[0]
        if (
            sender[0].lower() == mailbox_address.lower()
            or recipient.lower() == mailbox_address.lower()
        ):
            raise ValidationError("回复来源无效")
        message_id = str(headers.get("Message-ID", ""))
        validate_reply_message_id(message_id)
        references = (
            parse_reply_references(str(headers["References"]))
            if headers.get("References") is not None
            else ()
        )
        references = tuple(ref for ref in references if ref != message_id) + (
            message_id,
        )
        if len(references) > 20:
            raise ValidationError("回复来源无效")
        return ReplyHeaderFacts(
            sender[0], reply[0] if reply else None, recipient, message_id, references
        )
    except (ValueError, TypeError, IndexError, AttributeError, UnicodeError):
        raise ValidationError("回复来源无效") from None
