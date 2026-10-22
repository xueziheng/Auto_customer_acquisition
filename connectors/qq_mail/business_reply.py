"""QQ单封原线程回复；复用SMTP/IMAP确定性发送协议，不使用冷邮件契约。"""

import asyncio
import hashlib
import re
import smtplib
from contextlib import suppress
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.policy import SMTP

from connectors.aliyun_mail.client import (
    _check_smtp_reply,
    _close_imap,
    _close_smtp,
    _has_sent_message,
    _safe_error,
    _sent_mailbox,
)
from connectors.gmail.client import GmailSendResult, GmailTransactionalSendRequest
from connectors.qq_mail.client import QQMailConnector
from shared.errors import ValidationError
from tool_gateway.errors import DeliveryCertainty, ToolErrorCategory, ToolGatewayError


@dataclass(frozen=True, repr=False)
class QQReplyEnvelope:
    from_address: str
    recipient_address: str
    subject: str
    body: str
    deterministic_message_id: str
    idempotency_header: str
    in_reply_to: str = field(repr=False)

    def __post_init__(self):
        GmailTransactionalSendRequest(
            self.from_address,
            self.recipient_address,
            self.subject,
            self.body,
            self.deterministic_message_id,
            self.idempotency_header,
        )
        if not re.fullmatch(r"<[^<>\s@]{1,200}@[^<>\s@]{1,200}>", self.in_reply_to):
            raise ValidationError("原信关联头无效")


def reply_mime(request: QQReplyEnvelope) -> bytes:
    request.__post_init__()
    message = EmailMessage(policy=SMTP)
    for name, value in {
        "From": request.from_address,
        "To": request.recipient_address,
        "Subject": request.subject,
        "Message-ID": f"<{request.deterministic_message_id}>",
        "X-TradeOS-Idempotency-V1": request.idempotency_header,
        "In-Reply-To": request.in_reply_to,
        "References": request.in_reply_to,
        "Auto-Submitted": "auto-replied",
        "X-Auto-Response-Suppress": "All",
    }.items():
        message[name] = value
    message.set_content(request.body)
    return message.as_bytes()


class QQBusinessReplyConnector:
    def __init__(self, mail: QQMailConnector):
        self.mail = mail

    async def send_once(self, request: QQReplyEnvelope) -> GmailSendResult:
        return await asyncio.to_thread(self._execute, request, False)

    async def reconcile_once(self, request: QQReplyEnvelope) -> GmailSendResult:
        return await asyncio.to_thread(self._execute, request, True)

    def _execute(self, request: QQReplyEnvelope, reconcile: bool) -> GmailSendResult:
        request.__post_init__()
        if request.from_address != self.mail._address:
            raise ValidationError("当前QQ发件邮箱不匹配")
        imap = smtp = None
        data_started = False
        provider_ref = (
            "qq_"
            + hashlib.sha256(
                (request.from_address + "\n" + request.deterministic_message_id).encode(
                    "ascii"
                )
            ).hexdigest()
        )
        try:
            imap = self.mail._imap()
            mailbox = _sent_mailbox(imap)
            # 共享检索只消费相同邮件头，不包含冷邮件退订或Campaign规则。
            if _has_sent_message(imap, request):
                return GmailSendResult(provider_ref, DeliveryCertainty.SENT, True)
            if reconcile:
                raise ToolGatewayError(ToolErrorCategory.RECONCILIATION_REQUIRED)
            raw = reply_mime(request)
            smtp = self.mail._smtp()
            code, _ = smtp.mail(request.from_address)
            _check_smtp_reply(code)
            code, _ = smtp.rcpt(request.recipient_address)
            _check_smtp_reply(code, allow_forward=True)
            data_started = True
            code, _ = smtp.data(raw)
            if code != 250 and not 400 <= code < 600:
                raise ToolGatewayError(ToolErrorCategory.RECONCILIATION_REQUIRED)
            _check_smtp_reply(code)
            with suppress(Exception):
                imap.append(mailbox, r"\Seen", None, raw)
            return GmailSendResult(provider_ref, DeliveryCertainty.SENT, False)
        except Exception as error:  # noqa: BLE001 -- 外部异常映射为固定类别，DATA后保守按未知处理
            if data_started and not isinstance(
                error, (ToolGatewayError, smtplib.SMTPDataError)
            ):
                raise ToolGatewayError(
                    ToolErrorCategory.RECONCILIATION_REQUIRED
                ) from None
            raise _safe_error(error) from None
        finally:
            _close_smtp(smtp)
            _close_imap(imap)
