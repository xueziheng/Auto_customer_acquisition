"""QQ邮箱固定TLS端点；复用已验收的单封邮件与对账协议。"""
import smtplib
import ssl

from pydantic import SecretStr

from connectors.aliyun_mail.client import (
    AliyunMailConnector,
    AliyunMailTransport,
    BoundedIMAP4SSL,
)
from shared.errors import ValidationError

from .manifest import MANIFEST


class QQMailTransport:
    def open_smtp(self) -> smtplib.SMTP_SSL:
        return smtplib.SMTP_SSL("smtp.qq.com", 465, context=ssl.create_default_context(), timeout=15)
    def open_imap(self) -> BoundedIMAP4SSL:
        return BoundedIMAP4SSL("imap.qq.com", 993, ssl_context=ssl.create_default_context(), timeout=15)

class QQMailConnector(AliyunMailConnector):
    manifest = MANIFEST
    provider_prefix = "qq_"
    def __init__(self, address: str, password: SecretStr, *, transport: AliyunMailTransport | None = None):
        if not address.endswith("@qq.com"):
            raise ValidationError("QQ邮箱地址无效")
        super().__init__(address, password, transport=transport or QQMailTransport())
