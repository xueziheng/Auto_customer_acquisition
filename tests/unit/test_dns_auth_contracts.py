"""DNS 认证共享合同的严格、安全边界。"""

from __future__ import annotations

import importlib
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone

import pytest

from shared.errors import ValidationError


def _contracts():
    try:
        return importlib.import_module("shared.schemas.dns_auth")
    except ModuleNotFoundError as exc:
        pytest.fail(f"RED：DNS 认证共享合同尚未创建（{exc.name}）")


def test_dns_request_is_frozen_and_accepts_only_canonical_domain_and_selector() -> None:
    """大小写、URL、凭证形态或可变请求会把非规范查询送到 resolver。"""
    dns_auth = _contracts()
    request = dns_auth.DnsAuthenticationRequest("example.co.uk", "selector1")
    assert request.domain == "example.co.uk"
    assert request.dkim_selector == "selector1"
    with pytest.raises(FrozenInstanceError):
        request.domain = "other.example"  # type: ignore[misc]

    unsafe = (
        ("Example.com", "selector1"),
        ("https://example.com", "selector1"),
        ("user@example.com", "selector1"),
        ("*.example.com", "selector1"),
        ("127.0.0.1", "selector1"),
        ("example..com", "selector1"),
        ("example.com..", "selector1"),
        ("example.com", "Selector1"),
        ("example.com", "selector.one"),
        ("example.com", "bearer-token"),
        ("example.com", "selector\x00one"),
    )
    for domain, selector in unsafe:
        with pytest.raises(ValidationError) as caught:
            dns_auth.DnsAuthenticationRequest(domain, selector)
        assert domain not in str(caught.value)
        assert selector not in str(caught.value)


def test_dns_request_rejects_label_and_wire_size_overflow() -> None:
    """超长 label/名称必须在任何 DNS 调用前失败。"""
    dns_auth = _contracts()
    with pytest.raises(ValidationError):
        dns_auth.DnsAuthenticationRequest(f"{'a' * 64}.example", "s1")
    with pytest.raises(ValidationError):
        dns_auth.DnsAuthenticationRequest(".".join(["a" * 63] * 5), "s1")
    with pytest.raises(ValidationError):
        dns_auth.DnsAuthenticationRequest("example.com", "s" * 64)


def test_dns_facts_are_consistent_utc_and_never_echo_raw_material() -> None:
    """事实必须逐失败项闭合，且 repr 只能出现固定 typed 信息。"""
    dns_auth = _contracts()
    failure = dns_auth.DnsAuthenticationFailure(
        "spf",
        dns_auth.DnsAuthenticationFailureCategory.MISSING,
        "configure_spf",
    )
    facts = dns_auth.DnsAuthenticationFacts(
        datetime(2026, 8, 14, tzinfo=UTC),
        False,
        True,
        True,
        (failure,),
        "a" * 64,
    )
    assert not facts.all_passed
    assert "v=spf1 include:private.example" not in repr(facts)

    with pytest.raises(ValidationError):
        dns_auth.DnsAuthenticationFacts(
            datetime(2026, 8, 14),  # noqa: DTZ001 - 验证 naive 时间拒绝
            False,
            True,
            True,
            (failure,),
            "a" * 64,
        )
    with pytest.raises(ValidationError):
        dns_auth.DnsAuthenticationFacts(
            datetime(2026, 8, 14, tzinfo=timezone(timedelta(hours=8))),
            False,
            True,
            True,
            (failure,),
            "a" * 64,
        )
    with pytest.raises(ValidationError):
        dns_auth.DnsAuthenticationFacts(
            datetime(2026, 8, 14, tzinfo=UTC),
            True,
            True,
            True,
            (failure,),
            "a" * 64,
        )
    for unsafe_ref in ("A" * 64, "a" * 63, "secret", "g" * 64):
        with pytest.raises(ValidationError):
            dns_auth.DnsAuthenticationFacts(
                datetime(2026, 8, 14, tzinfo=UTC),
                True,
                True,
                True,
                (),
                unsafe_ref,
            )


def test_dns_failure_rejects_free_text_and_unknown_check() -> None:
    """失败说明只能是固定修复代码，不能成为原始 TXT/异常泄漏通道。"""
    dns_auth = _contracts()
    for values in (
        ("mx", dns_auth.DnsAuthenticationFailureCategory.MISSING, "configure_spf"),
        ("spf", "missing", "configure_spf"),
        (
            "spf",
            dns_auth.DnsAuthenticationFailureCategory.MALFORMED,
            "v=spf1 include:private.example",
        ),
    ):
        with pytest.raises(ValidationError):
            dns_auth.DnsAuthenticationFailure(*values)
