"""阿里企业邮箱单封发送；SMTP 接受与不确定交付严格分开。"""

from __future__ import annotations

import asyncio
import hashlib
import imaplib
import re
import smtplib
import ssl
from contextlib import suppress
from email.message import EmailMessage
from email.parser import BytesHeaderParser
from email.policy import SMTP, default
from typing import Protocol, runtime_checkable

from pydantic import SecretStr

from connectors.gmail.client import GmailSendRequest, GmailSendResult
from shared.errors import ValidationError
from tool_gateway.errors import DeliveryCertainty, ToolErrorCategory, ToolGatewayError

from .manifest import MANIFEST

_MAILBOX = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
)
_LIST_ENTRY = re.compile(rb'^\(([^)]*)\) (?:"(?:[^"\\]|\\.)*"|NIL) (.+)$')
_MAX_HEADERS = 32_768
_MAX_MATCHES = 20


class BoundedIMAP4SSL(imaplib.IMAP4_SSL):
    """限制协议行、literal 和总响应，避免已发送检索拉取无界内容。"""

    def __init__(
        self, host: str, port: int, *, ssl_context: ssl.SSLContext, timeout: int
    ) -> None:
        self._response_budget = 262_144
        super().__init__(host, port, ssl_context=ssl_context, timeout=timeout)

    def read(self, size: int) -> bytes:
        if size > _MAX_HEADERS or size > self._response_budget:
            raise imaplib.IMAP4.error("邮箱响应超过上限")
        result = super().read(size)
        self._response_budget -= len(result)
        return result

    def readline(self) -> bytes:
        result = self.file.readline(min(16_385, self._response_budget + 1))
        self._response_budget -= len(result)
        if len(result) > 16_384 or self._response_budget < 0:
            raise imaplib.IMAP4.error("邮箱响应超过上限")
        return result


@runtime_checkable
class AliyunMailTransport(Protocol):
    """只承载固定 TLS 外部 IO；认证与发送确定性由连接器负责。"""

    def open_smtp(self) -> smtplib.SMTP_SSL:
        """建立固定官方 SMTP TLS 连接，不认证、不发信。"""
        ...

    def open_imap(self) -> imaplib.IMAP4_SSL:
        """建立固定官方 IMAP TLS 连接，不认证、不读邮件。"""
        ...


class DefaultAliyunMailTransport:
    """生产端点不可配置；使用系统证书库和十五秒 socket 超时。"""

    def open_smtp(self) -> smtplib.SMTP_SSL:
        return smtplib.SMTP_SSL(
            "smtp.qiye.aliyun.com",
            465,
            context=ssl.create_default_context(),
            timeout=15,
        )

    def open_imap(self) -> imaplib.IMAP4_SSL:
        return BoundedIMAP4SSL(
            "imap.qiye.aliyun.com",
            993,
            ssl_context=ssl.create_default_context(),
            timeout=15,
        )


class AliyunMailConnector:
    """绑定一个已授权邮箱；只由受信 Gateway 在执行阶段构造。"""

    manifest = MANIFEST
    provider_prefix = "aliyun_"

    def __init__(
        self,
        address: str,
        password: SecretStr,
        *,
        transport: AliyunMailTransport | None = None,
    ) -> None:
        if (
            not isinstance(address, str)
            or len(address) > 254
            or not _MAILBOX.fullmatch(address)
        ):
            raise ValidationError("阿里邮箱地址无效")
        local_part = address.rsplit("@", 1)[0]
        if local_part.startswith(".") or local_part.endswith(".") or ".." in local_part:
            raise ValidationError("阿里邮箱地址无效")
        if not isinstance(password, SecretStr):
            raise ValidationError("阿里邮箱凭证无效")
        value = password.get_secret_value()
        if (
            not value
            or len(value) > 1024
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            raise ValidationError("阿里邮箱凭证无效")
        if transport is not None and not isinstance(transport, AliyunMailTransport):
            raise ValidationError("阿里邮箱传输无效")
        self._address = address
        self._password = password
        self._transport = (
            transport if transport is not None else DefaultAliyunMailTransport()
        )

    async def authenticate(self) -> None:
        """只验证 SMTP 和 IMAP 凭证及已发送文件夹，不发送诊断邮件。"""
        await asyncio.to_thread(self._authenticate)

    async def send_once(self, request: GmailSendRequest) -> GmailSendResult:
        """检查精确已发送证据后只发送一次；不确定写入禁止自动重发。"""
        self._validate_request(request)
        return await asyncio.to_thread(self._execute, request, False)

    async def reconcile_once(self, request: GmailSendRequest) -> GmailSendResult:
        """只读取确定性头关联的已发送候选；未命中仍需人工对账。"""
        self._validate_request(request)
        return await asyncio.to_thread(self._execute, request, True)

    def _validate_request(self, request: GmailSendRequest) -> None:
        if not isinstance(request, GmailSendRequest):
            raise ValidationError("阿里邮箱发送参数无效")
        request.__post_init__()
        if request.from_address != self._address:
            raise ValidationError("阿里邮箱发件身份不匹配")

    def _imap(self) -> imaplib.IMAP4_SSL:
        client = self._transport.open_imap()
        try:
            try:
                status, _ = client.login(
                    self._address, self._password.get_secret_value()
                )
                if status != "OK":
                    raise ToolGatewayError(ToolErrorCategory.PROVIDER_AUTH_REQUIRED)
            except imaplib.IMAP4.abort:
                raise ToolGatewayError(ToolErrorCategory.PROVIDER_TRANSIENT) from None
            except imaplib.IMAP4.error:
                raise ToolGatewayError(
                    ToolErrorCategory.PROVIDER_AUTH_REQUIRED
                ) from None
            return client
        except Exception:
            _close_imap(client)
            raise

    def _smtp(self) -> smtplib.SMTP_SSL:
        client = self._transport.open_smtp()
        try:
            client.login(self._address, self._password.get_secret_value())
            return client
        except Exception:
            _close_smtp(client)
            raise

    def _authenticate(self) -> None:
        imap: imaplib.IMAP4_SSL | None = None
        smtp: smtplib.SMTP_SSL | None = None
        try:
            imap = self._imap()
            _sent_mailbox(imap)
            smtp = self._smtp()
        except Exception as error:  # noqa: BLE001 — 外部异常必须转成固定安全分类
            raise _safe_error(error) from None
        finally:
            _close_smtp(smtp)
            _close_imap(imap)

    def _execute(self, request: GmailSendRequest, reconcile: bool) -> GmailSendResult:
        imap: imaplib.IMAP4_SSL | None = None
        smtp: smtplib.SMTP_SSL | None = None
        data_started = False
        accepted = False
        provider_ref = (
            self.provider_prefix
            + hashlib.sha256(
                (self._address + "\n" + request.deterministic_message_id).encode(
                    "ascii"
                )
            ).hexdigest()
        )
        try:
            imap = self._imap()
            mailbox = _sent_mailbox(imap)
            if _has_sent_message(imap, request):
                return GmailSendResult(provider_ref, DeliveryCertainty.SENT, True)
            if reconcile:
                raise ToolGatewayError(ToolErrorCategory.RECONCILIATION_REQUIRED)
            raw = _message(request)
            smtp = self._smtp()
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
            # 已接受才生成本地客户端 Sent 副本；该失败不能把事实退回未发送。
            with suppress(Exception):
                imap.append(mailbox, r"\Seen", None, raw)
            return GmailSendResult(provider_ref, DeliveryCertainty.SENT, False)
        except Exception as error:  # noqa: BLE001 — 外部异常必须转成固定安全分类
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


def _message(request: GmailSendRequest) -> bytes:
    message = EmailMessage(policy=SMTP)
    message["From"] = request.from_address
    message["To"] = request.recipient_address
    message["Subject"] = request.subject
    message["Message-ID"] = f"<{request.deterministic_message_id}>"
    message["X-TradeOS-Idempotency-V1"] = request.idempotency_header
    message["List-Unsubscribe"] = f"<{request.unsubscribe_url}>"
    message["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    message.set_content(request.body)
    return message.as_bytes()


def _sent_mailbox(client: imaplib.IMAP4_SSL) -> str:
    status, entries = client.list()
    if status != "OK" or len(entries) > 64:
        raise ToolGatewayError(ToolErrorCategory.PROVIDER_PERMANENT)
    special: list[str] = []
    known: list[str] = []
    for entry in entries:
        if not isinstance(entry, bytes) or len(entry) > 1024:
            raise ToolGatewayError(ToolErrorCategory.PROVIDER_PERMANENT)
        match = _LIST_ENTRY.fullmatch(entry)
        if match is None:
            continue
        flags, raw_name = match.groups()
        if b"\\noselect" in flags.lower().split():
            continue
        # IMAP mailbox 使用 ASCII modified UTF-7；不把未校验名字作为协议参数。
        name = raw_name.decode("ascii")
        if name.startswith('"') and name.endswith('"'):
            name = re.sub(r"\\(.)", r"\1", name[1:-1])
        if (
            not name
            or len(name) > 256
            or any(ord(char) < 32 or ord(char) == 127 for char in name)
        ):
            raise ToolGatewayError(ToolErrorCategory.PROVIDER_PERMANENT)
        quoted = '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'
        if b"\\sent" in flags.lower().split():
            special.append(quoted)
        elif name.lower() in {"sent", "sent items", "sent messages", "&xfjt0zab-"}:
            known.append(quoted)
    choices = special or known
    if len(choices) != 1:
        raise ToolGatewayError(ToolErrorCategory.PROVIDER_PERMANENT)
    status, _ = client.select(choices[0], readonly=True)
    if status != "OK":
        raise ToolGatewayError(ToolErrorCategory.PROVIDER_PERMANENT)
    return choices[0]


def _has_sent_message(client: imaplib.IMAP4_SSL, request: GmailSendRequest) -> bool:
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
        raise ToolGatewayError(ToolErrorCategory.PROVIDER_PERMANENT)
    uids = result[0].split()
    if len(uids) > _MAX_MATCHES or any(
        not re.fullmatch(rb"[1-9][0-9]{0,9}", uid) for uid in uids
    ):
        raise ToolGatewayError(ToolErrorCategory.RECONCILIATION_REQUIRED)
    expected = {
        "Message-ID": f"<{request.deterministic_message_id}>",
        "X-TradeOS-Idempotency-V1": request.idempotency_header,
        "From": request.from_address,
        "To": request.recipient_address,
        "Subject": request.subject,
    }
    for uid in uids:
        status, data = client.uid(
            "FETCH",
            uid.decode("ascii"),
            "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID X-TRADEOS-IDEMPOTENCY-V1 FROM TO SUBJECT)])",
        )
        if status != "OK" or len(data) > 3:
            raise ToolGatewayError(ToolErrorCategory.RECONCILIATION_REQUIRED)
        literals = [
            item[1] for item in data if isinstance(item, tuple) and len(item) == 2
        ]
        if (
            len(literals) != 1
            or not isinstance(literals[0], bytes)
            or len(literals[0]) > _MAX_HEADERS
        ):
            raise ToolGatewayError(ToolErrorCategory.RECONCILIATION_REQUIRED)
        headers = BytesHeaderParser(policy=default).parsebytes(literals[0])
        if headers.defects or any(
            len(headers.get_all(key, [])) != 1 or str(headers[key]) != value
            for key, value in expected.items()
        ):
            raise ToolGatewayError(ToolErrorCategory.RECONCILIATION_REQUIRED)
    return bool(uids)


def _check_smtp_reply(code: int, *, allow_forward: bool = False) -> None:
    if code == 250 or (allow_forward and code == 251):
        return
    category = (
        ToolErrorCategory.PROVIDER_TRANSIENT
        if 400 <= code < 500
        else ToolErrorCategory.PROVIDER_PERMANENT
    )
    raise ToolGatewayError(category)


def _safe_error(error: Exception) -> ToolGatewayError:
    if isinstance(error, ToolGatewayError):
        return error
    if isinstance(error, smtplib.SMTPAuthenticationError):
        return ToolGatewayError(ToolErrorCategory.PROVIDER_AUTH_REQUIRED)
    if isinstance(error, smtplib.SMTPResponseException):
        category = (
            ToolErrorCategory.PROVIDER_TRANSIENT
            if 400 <= error.smtp_code < 500
            else ToolErrorCategory.PROVIDER_PERMANENT
        )
        return ToolGatewayError(category)
    if isinstance(error, ssl.SSLCertVerificationError):
        return ToolGatewayError(ToolErrorCategory.PROVIDER_PERMANENT)
    if isinstance(
        error, (OSError, imaplib.IMAP4.abort, smtplib.SMTPServerDisconnected)
    ):
        return ToolGatewayError(ToolErrorCategory.PROVIDER_TRANSIENT)
    return ToolGatewayError(ToolErrorCategory.PROVIDER_PERMANENT)


def _close_smtp(client: smtplib.SMTP_SSL | None) -> None:
    if client is not None:
        with suppress(Exception):
            client.close()


def _close_imap(client: imaplib.IMAP4_SSL | None) -> None:
    if client is not None:
        with suppress(Exception):
            client.shutdown()
