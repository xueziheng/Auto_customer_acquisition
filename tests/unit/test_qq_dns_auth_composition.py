"""QQ DNS 装配只缩小查询范围，不替代现有 DNS 判定。"""

from __future__ import annotations

import asyncio
import importlib
from datetime import UTC, datetime

import dns.resolver
import pytest

from shared.errors import TransientError, ValidationError
from shared.schemas.dns_auth import (
    DnsAuthenticationFailureCategory,
    DnsAuthenticationRequest,
)

_NOW = datetime(2026, 10, 14, 13, 38, 9, tzinfo=UTC)


def _module():
    try:
        return importlib.import_module("apps.composition_support.qq_dns_auth")
    except ModuleNotFoundError:
        pytest.fail("受限 QQ DNS 装配尚未实现")


class _Txt:
    def __init__(self, *chunks: bytes) -> None:
        self.strings = chunks


class _Resolver:
    def __init__(self, *, dmarc: bytes = b"v=DMARC1; p=reject") -> None:
        self.calls: list[tuple[str, str]] = []
        self.answers: dict[str, object] = {
            "qq.com": [_Txt(b"v=spf1 include:spf.mail.qq.com ~all")],
            "s201512._domainkey.qq.com": [
                _Txt(b"v=DKIM1; k=rsa; ", b"p=QUJDREVGR0hJSktMTU5PUA==")
            ],
            "_dmarc.qq.com": [_Txt(dmarc)],
        }

    async def resolve(self, name: str, rdtype: str) -> object:
        self.calls.append((name, rdtype))
        answer = self.answers[name]
        if isinstance(answer, Exception):
            raise answer
        return answer


async def test_qq_check_uses_exact_public_names_and_actual_dns_parser() -> None:
    """域/selector 拼错或跳过真实解析都会破坏三项认证结果。"""
    resolver = _Resolver()
    checker = _module().build_qq_dns_authentication_checker(
        now=lambda: _NOW, resolver=resolver
    )
    facts = await checker.check(DnsAuthenticationRequest("qq.com", "s201512"))
    assert facts.all_passed
    assert facts.checked_at == _NOW
    assert resolver.calls == [
        ("qq.com", "TXT"),
        ("s201512._domainkey.qq.com", "TXT"),
        ("_dmarc.qq.com", "TXT"),
    ]
    assert "spf.mail.qq.com" not in repr(facts)
    assert "QUJD" not in repr(facts)


@pytest.mark.parametrize(
    ("domain", "selector"),
    [
        ("example.com", "s201512"),
        ("qq.com.example.com", "s201512"),
        ("mail.qq.com", "s201512"),
        ("qq.com", "other-selector"),
    ],
)
async def test_unconfigured_domain_or_selector_is_rejected_before_dns(
    domain: str, selector: str
) -> None:
    """删除范围判断会让 QQ selector 进入未授权域的外部查询。"""
    resolver = _Resolver()
    checker = _module().build_qq_dns_authentication_checker(
        now=lambda: _NOW, resolver=resolver
    )
    with pytest.raises(ValidationError):
        await checker.check(DnsAuthenticationRequest(domain, selector))
    assert resolver.calls == []


async def test_qq_public_domain_never_overrides_unsafe_dmarc_policy() -> None:
    """给 QQ 特判通过会掩盖真实 p=none 策略失败。"""
    checker = _module().build_qq_dns_authentication_checker(
        now=lambda: _NOW, resolver=_Resolver(dmarc=b"v=DMARC1; p=none")
    )
    facts = await checker.check(DnsAuthenticationRequest("qq.com", "s201512"))
    assert (facts.spf_passed, facts.dkim_passed, facts.dmarc_passed) == (
        True, True, False
    )
    assert [(failure.check, failure.category) for failure in facts.failures] == [
        ("dmarc", DnsAuthenticationFailureCategory.POLICY_UNSAFE)
    ]


async def test_missing_selector_remains_a_typed_missing_fact() -> None:
    """selector 轮换后不得把以前通过的结果当缓存复用。"""
    resolver = _Resolver()
    checker = _module().build_qq_dns_authentication_checker(
        now=lambda: _NOW, resolver=resolver
    )
    assert (await checker.check(DnsAuthenticationRequest("qq.com", "s201512"))).all_passed
    resolver.answers["s201512._domainkey.qq.com"] = dns.resolver.NoAnswer()
    facts = await checker.check(DnsAuthenticationRequest("qq.com", "s201512"))
    assert facts.dkim_passed is False
    assert [(failure.check, failure.category) for failure in facts.failures] == [
        ("dkim", DnsAuthenticationFailureCategory.MISSING)
    ]


async def test_provider_failure_is_redacted_and_never_retried_by_composition() -> None:
    """wrapper 不得泄漏 provider 文本或用自动重试掩盖失败。"""
    resolver = _Resolver()
    resolver.answers["qq.com"] = OSError("private-provider-canary")
    checker = _module().build_qq_dns_authentication_checker(
        now=lambda: _NOW, resolver=resolver
    )
    with pytest.raises(TransientError) as error:
        await checker.check(DnsAuthenticationRequest("qq.com", "s201512"))
    assert "private-provider-canary" not in str(error.value)
    assert resolver.calls == [("qq.com", "TXT")]


async def test_dns_query_has_a_real_upper_time_bound_and_cancels_io() -> None:
    """删除 timeout 会使单次 DNS 读取永久占用 workflow。"""
    cancelled = asyncio.Event()

    class HangingResolver:
        async def resolve(self, name: str, rdtype: str) -> object:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    checker = _module().build_qq_dns_authentication_checker(
        now=lambda: _NOW, resolver=HangingResolver()
    )
    async with asyncio.timeout(12):
        with pytest.raises(TransientError):
            await checker.check(DnsAuthenticationRequest("qq.com", "s201512"))
    assert cancelled.is_set()


async def test_default_resolver_is_constructed_per_composition_without_querying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """构造时查询或跨实例共享 resolver 都会破坏显式 Gateway 生命周期。"""
    module = _module()
    constructed: list[_Resolver] = []

    def resolver_factory() -> _Resolver:
        resolver = _Resolver()
        constructed.append(resolver)
        return resolver

    monkeypatch.setattr(module, "DnsPythonAsyncResolver", resolver_factory)
    first = module.build_qq_dns_authentication_checker(now=lambda: _NOW)
    second = module.build_qq_dns_authentication_checker(now=lambda: _NOW)
    assert len(constructed) == 2
    assert all(not resolver.calls for resolver in constructed)
    assert (await first.check(DnsAuthenticationRequest("qq.com", "s201512"))).all_passed
    assert constructed[1].calls == []
    constructed[1].answers["_dmarc.qq.com"] = [_Txt(b"v=DMARC1; p=none")]
    assert not (await second.check(DnsAuthenticationRequest("qq.com", "s201512"))).all_passed
