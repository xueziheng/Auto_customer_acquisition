"""只组装 QQ 公共 DNS 检查；认证判定与外部出口仍归原 Connector/Gateway。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime

from connectors.dns_auth.client import (
    AsyncTxtResolver,
    DnsAuthenticationConnector,
    DnsPythonAsyncResolver,
)
from shared.errors import ValidationError
from shared.schemas.dns_auth import DnsAuthenticationFacts, DnsAuthenticationRequest
from tool_gateway.handlers.dns_auth import DnsAuthenticationChecker

QQ_DNS_DOMAIN = "qq.com"
QQ_DKIM_SELECTOR = "s201512"
# 2026-08-29 公开原始邮件头的 d=qq.com、s=s201512；不是任意账号认证证明。
QQ_DKIM_SELECTOR_SOURCE_URL = (
    "https://mailarchive.ietf.org/arch/msg/acme/cdpGtPDNSHxACvNaw--raciG51A/"
)
_QQ_TXT_NAMES = frozenset(
    {"qq.com", "s201512._domainkey.qq.com", "_dmarc.qq.com"}
)


class _BoundedQQResolver:
    """每次只允许固定 TXT 名称，超时交由既有 Connector 脱敏。"""

    def __init__(self, resolver: AsyncTxtResolver) -> None:
        self._resolver = resolver

    async def resolve(self, name: str, rdtype: str) -> object:
        if name not in _QQ_TXT_NAMES or rdtype != "TXT":
            raise ValidationError("QQ DNS 查询超出已配置范围")
        return await asyncio.wait_for(self._resolver.resolve(name, rdtype), timeout=10)


class _QQDnsAuthenticationChecker:
    """先验证显式配置范围，再按原始 DNS 策略生成当次事实。"""

    def __init__(self, connector: DnsAuthenticationConnector) -> None:
        self._connector = connector

    async def check(self, request: DnsAuthenticationRequest) -> DnsAuthenticationFacts:
        if (
            not isinstance(request, DnsAuthenticationRequest)
            or request.domain != QQ_DNS_DOMAIN
            or request.dkim_selector != QQ_DKIM_SELECTOR
        ):
            raise ValidationError("QQ DNS 认证请求超出已配置范围")
        return await self._connector.check(request)


def build_qq_dns_authentication_checker(
    *,
    now: Callable[[], datetime],
    resolver: AsyncTxtResolver | None = None,
) -> DnsAuthenticationChecker:
    """创建独立 checker，供原 dns.auth.check handler 装配；构造不查询 DNS。

    入口必须保留原 Gateway 四阶段与账本。本函数不读取环境、不建立 Gateway、
    不缓存认证结果；QQ 的真实 DNS 变化仍按原 Connector 规则返回失败。
    """
    selected = DnsPythonAsyncResolver() if resolver is None else resolver
    return _QQDnsAuthenticationChecker(
        DnsAuthenticationConnector(_BoundedQQResolver(selected), now=now)
    )
