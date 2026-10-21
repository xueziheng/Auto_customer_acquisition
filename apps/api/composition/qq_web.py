"""本人QQ邮箱连接；独立私有目录与具名Gateway插件。"""
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from connectors.qq_mail.client import QQMailConnector
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.aliyun_connect import MailAuthenticator

from .aliyun_web import AliyunWebService


class QQWebService(AliyunWebService):
    manifest = replace(AliyunWebService.manifest, tool_id="email.qq.connect",
        description="验证当前员工授权的QQ邮箱SMTP和IMAP，不发信")
    provider_label = "QQ邮箱"
    def __init__(self, root: Path, sessions: async_sessionmaker[AsyncSession], fingerprints: HmacFingerprintProvider, *, connector_factory: Callable[[str, SecretStr], MailAuthenticator] | None = None):
        super().__init__(root, sessions, fingerprints, connector_factory=connector_factory or qq_connector)

def qq_connector(address: str, password: SecretStr) -> QQMailConnector:
    return QQMailConnector(address, password)
