"""QQ 普通回复的单封传输；调用方负责业务审批及持久幂等，不启用生产发送。"""

from __future__ import annotations

import asyncio
import hashlib
import imaplib
import re
import smtplib
from contextlib import suppress
from email.message import EmailMessage
from email.parser import BytesHeaderParser
from email.policy import SMTP, default

from pydantic import SecretStr

from connectors.aliyun_mail.client import (
    AliyunMailTransport,
    _check_smtp_reply,
    _close_imap,
    _close_smtp,
    _safe_error,
    _sent_mailbox,
)
from connectors.gmail.client import GmailSendResult
from shared.errors import ValidationError
from shared.schemas.business_reply import SMTPReplyRequest
from tool_gateway.errors import DeliveryCertainty, ToolErrorCategory, ToolGatewayError

from .client import QQMailConnector
from .manifest import MANIFEST


class QQBusinessReplyConnector:
    """固定 QQ TLS 传输，不继承冷开发请求契约或其营销头。"""

    manifest = MANIFEST

    def __init__(
        self,
        address: str,
        password: SecretStr,
        *,
        transport: AliyunMailTransport | None = None,
    ) -> None:
        self._client = QQMailConnector(address, password, transport=transport)
        self._address = address

    async def send_once(self, request: SMTPReplyRequest) -> GmailSendResult:
        """核对精确已发送头后提交一封；外部结果未知交由调用方挂起核对。"""
        self._validate_request(request)
        return await asyncio.to_thread(self._execute, request, False)

    async def reconcile_once(self, request: SMTPReplyRequest) -> GmailSendResult:
        """只检索已有发送证据；未命中不得建立 SMTP 连接或重发。"""
        self._validate_request(request)
        return await asyncio.to_thread(self._execute, request, True)

    def _validate_request(self, request: SMTPReplyRequest) -> None:
        if not isinstance(request, SMTPReplyRequest):
            raise ValidationError("QQ 回复参数无效")
        request.__post_init__()
        if request.from_address != self._address:
            raise ValidationError("QQ 回复身份不匹配")

    def _execute(self, request: SMTPReplyRequest, reconcile: bool) -> GmailSendResult:
        imap: imaplib.IMAP4_SSL | None = None
        smtp: smtplib.SMTP_SSL | None = None
        data_started = False
        accepted = False
        provider_ref = (
            "qq_"
            + hashlib.sha256(
                (self._address + "\n" + request.deterministic_message_id).encode(
                    "ascii"
                )
            ).hexdigest()
        )
        try:
            imap = self._client._imap()
            mailbox = _sent_mailbox(imap)
            if _has_sent_reply(imap, request):
                return GmailSendResult(provider_ref, DeliveryCertainty.SENT, True)
            if reconcile:
                raise ToolGatewayError(ToolErrorCategory.RECONCILIATION_REQUIRED)
            raw = _message(request)
            smtp = self._client._smtp()
            code, _ = smtp.mail(request.from_address)
            _check_smtp_reply(code)
            code, _ = smtp.rcpt(request.recipient_address)
            _check_smtp_reply(code, allow_forward=True)
            data_started = True
            code, _ = smtp.data(raw)
            if code != 250 and not 400 <= code < 600:
                raise ToolGatewayError(ToolErrorCategory.RECONCILIATION_REQUIRED)
            _check_smtp_reply(code)
            accepted = True
            # SMTP 已接受是事实，Sent 副本失败不能撤销该事实。
            with suppress(Exception):
                imap.append(mailbox, r"\Seen", None, raw)
            return GmailSendResult(provider_ref, DeliveryCertainty.SENT, False)
        except Exception as error:  # noqa: BLE001 — 外部文本不能进入异常边界
            if accepted:
                return GmailSendResult(provider_ref, DeliveryCertainty.SENT, False)
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


def _expected_headers(request: SMTPReplyRequest) -> dict[str, str]:
    """同一组绑定字段用于生成与对账；摘要包含正文而无需 IMAP 拉取正文。"""
    return {
        "From": request.from_address,
        "To": request.recipient_address,
        "Subject": request.subject,
        "Message-ID": f"<{request.deterministic_message_id}>",
        "X-TradeOS-Idempotency-V1": request.idempotency_header,
        "In-Reply-To": request.in_reply_to,
        "References": " ".join(request.references),
        "X-TradeOS-Content-SHA256": request.content_hash,
    }


def _message(request: SMTPReplyRequest) -> bytes:
    """生成普通回复 MIME，不添加营销列表头。"""
    message = EmailMessage(policy=SMTP)
    for key, value in _expected_headers(request).items():
        message[key] = value
    message.set_content(request.body)
    return message.as_bytes()


def _has_sent_reply(client: imaplib.IMAP4_SSL, request: SMTPReplyRequest) -> bool:
    """有界获取已发送候选；任何重复、缺失、畸形或不匹配头均挂起核对。"""
    status, result = client.uid(
        "SEARCH",
        "HEADER",
        "Message-ID",
        f'"<{request.deterministic_message_id}>"',
        "HEADER",
        "X-TradeOS-Idempotency-V1",
        f'"{request.idempotency_header}"',
    )
    if status != "OK" or len(result) != 1 or not isinstance(result[0], bytes):
        raise ToolGatewayError(ToolErrorCategory.RECONCILIATION_REQUIRED)
    uids = result[0].split()
    if len(uids) > 20 or any(
        not re.fullmatch(rb"[1-9][0-9]{0,9}", uid) for uid in uids
    ):
        raise ToolGatewayError(ToolErrorCategory.RECONCILIATION_REQUIRED)
    expected = _expected_headers(request)
    for uid in uids:
        status, data = client.uid(
            "FETCH",
            uid.decode("ascii"),
            "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID X-TRADEOS-IDEMPOTENCY-V1 FROM TO SUBJECT IN-REPLY-TO REFERENCES X-TRADEOS-CONTENT-SHA256)])",
        )
        if status != "OK" or len(data) > 3:
            raise ToolGatewayError(ToolErrorCategory.RECONCILIATION_REQUIRED)
        literals = [
            item[1] for item in data if isinstance(item, tuple) and len(item) == 2
        ]
        if (
            len(literals) != 1
            or not isinstance(literals[0], bytes)
            or len(literals[0]) > 32_768
        ):
            raise ToolGatewayError(ToolErrorCategory.RECONCILIATION_REQUIRED)
        headers = BytesHeaderParser(policy=default).parsebytes(literals[0])
        if headers.defects or any(
            len(headers.get_all(key, [])) != 1
            or getattr(headers[key], "defects", ())
            or str(headers[key]) != value
            for key, value in expected.items()
        ):
            raise ToolGatewayError(ToolErrorCategory.RECONCILIATION_REQUIRED)
    return bool(uids)
