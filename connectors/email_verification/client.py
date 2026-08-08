"""邮箱验证连接器骨架。"""

from __future__ import annotations

from typing import Any

from connectors.base import ConnectorManifest

MANIFEST = ConnectorManifest(
    connector_id="email_verification",
    capabilities=("contact.verify",),
    secret_refs=("EMAIL_VERIFY_API_KEY_REF",),
    compliance_note="结果缓存 30 天；risky/unknown 按不可用处理",
)


class EmailVerificationConnector:
    manifest = MANIFEST

    async def configure(self, secret_resolver: Any) -> None:
        raise NotImplementedError

    async def health_check(self) -> bool:
        raise NotImplementedError

    async def verify(self, email: str) -> dict:
        """返回 {result: valid|invalid|risky|unknown, provider_raw, cost_note}。"""
        raise NotImplementedError
