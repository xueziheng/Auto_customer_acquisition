"""DNS 认证 Connector：注入 resolver、严格边界和错误脱敏。"""

from __future__ import annotations

import importlib
import socket
from datetime import UTC, datetime

import dns.exception
import dns.resolver
import pytest
import requests.sessions

from shared.errors import TransientError, ValidationError

_NOW = datetime(2026, 8, 14, 12, tzinfo=UTC)


def _modules():
    try:
        contracts = importlib.import_module("shared.schemas.dns_auth")
        connector = importlib.import_module("connectors.dns_auth.client")
    except ModuleNotFoundError as exc:
        pytest.fail(f"RED：DNS Connector 尚未创建（{exc.name}）")
    return contracts, connector


class _Txt:
    def __init__(self, *chunks: bytes) -> None:
        self.strings = chunks


class _Resolver:
    def __init__(self, answers: dict[str, object]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str]] = []

    async def resolve(self, name: str, rdtype: str):
        self.calls.append((name, rdtype))
        answer = self.answers[name]
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _request(contracts, domain: str = "example.co.uk", selector: str = "s1"):
    return contracts.DnsAuthenticationRequest(domain, selector)


def test_offline_psl_constructor_never_uses_socket_or_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模块构造与 registrable-domain 验证都只能使用 wheel 内 PSL snapshot。"""

    def no_network(*args: object, **kwargs: object) -> None:
        del args, kwargs
        pytest.fail("离线 PSL 构造不得访问 socket/HTTP")

    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(requests.sessions.Session, "get", no_network)
    contracts = importlib.import_module("shared.schemas.dns_auth")
    connector = importlib.reload(importlib.import_module("connectors.dns_auth.client"))
    connector.DnsAuthenticationConnector(
        _Resolver({}), now=lambda: _NOW
    ).validate_request(_request(contracts))


async def test_connector_checks_exact_names_and_returns_only_typed_facts() -> None:
    """错误 qname 或原始 TXT 穿透会让结果不可审计且可能泄密。"""
    contracts, connector = _modules()
    resolver = _Resolver(
        {
            "example.co.uk": [_Txt(b"v=spf1 include:_spf.example.net -all")],
            "s1._domainkey.example.co.uk": [
                _Txt(b"v=DKIM1; k=rsa; p=QUJDREVGR0hJSktMTU5PUA==")
            ],
            "_dmarc.example.co.uk": [_Txt(b"v=DMARC1; p=reject")],
        }
    )
    facts = await connector.DnsAuthenticationConnector(
        resolver, now=lambda: _NOW
    ).check(_request(contracts))

    assert facts.all_passed
    assert facts.failures == ()
    assert len(facts.check_ref) == 64
    assert set(facts.check_ref) <= set("0123456789abcdef")
    assert resolver.calls == [
        ("example.co.uk", "TXT"),
        ("s1._domainkey.example.co.uk", "TXT"),
        ("_dmarc.example.co.uk", "TXT"),
    ]
    rendered = repr(facts)
    assert "include:_spf" not in rendered
    assert "QUJD" not in rendered


@pytest.mark.parametrize("domain", ["co.uk", "github.io"])
async def test_public_and_private_suffix_only_are_rejected_offline(
    domain: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PSL-only 输入不能查询；离线 snapshot 构造不得触碰 socket。"""
    contracts, connector = _modules()

    def no_network(*args: object, **kwargs: object) -> None:
        del args, kwargs
        pytest.fail("公共后缀判断不得访问网络")

    monkeypatch.setattr(socket, "create_connection", no_network)
    resolver = _Resolver({})
    with pytest.raises(ValidationError) as caught:
        await connector.DnsAuthenticationConnector(resolver, now=lambda: _NOW).check(
            _request(contracts, domain)
        )
    assert domain not in str(caught.value)
    assert resolver.calls == []


@pytest.mark.parametrize("domain", ["example.co.uk", "user.github.io"])
def test_registrable_public_and_private_suffix_domains_are_accepted_offline(
    domain: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """离线 PSL snapshot 必须同时识别 ICANN 与 private suffix 的注册域。"""
    contracts, connector = _modules()
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *args, **kwargs: pytest.fail("不得访问网络"),
    )
    instance = connector.DnsAuthenticationConnector(_Resolver({}), now=lambda: _NOW)
    instance.validate_request(_request(contracts, domain))


@pytest.mark.parametrize(
    ("missing_name", "expected_check"),
    [
        ("example.co.uk", "spf"),
        ("s1._domainkey.example.co.uk", "dkim"),
        ("_dmarc.example.co.uk", "dmarc"),
    ],
)
async def test_nxdomain_and_noanswer_become_missing_facts(
    missing_name: str, expected_check: str
) -> None:
    """不存在是永久业务事实，不是可重试 provider 故障。"""
    contracts, connector = _modules()
    answers: dict[str, object] = {
        "example.co.uk": [_Txt(b"v=spf1 -all")],
        "s1._domainkey.example.co.uk": [_Txt(b"v=DKIM1; p=QUJDREVGR0hJSktMTU5PUA==")],
        "_dmarc.example.co.uk": [_Txt(b"v=DMARC1; p=quarantine")],
    }
    answers[missing_name] = (
        dns.resolver.NXDOMAIN() if expected_check != "dkim" else dns.resolver.NoAnswer()
    )
    facts = await connector.DnsAuthenticationConnector(
        _Resolver(answers), now=lambda: _NOW
    ).check(_request(contracts))
    failure = next(item for item in facts.failures if item.check == expected_check)
    assert failure.category is contracts.DnsAuthenticationFailureCategory.MISSING


@pytest.mark.parametrize(
    ("name", "records", "category"),
    [
        (
            "example.co.uk",
            [_Txt(b"v=spf1 -all"), _Txt(b"v=spf1 include:x.example -all")],
            "malformed",
        ),
        ("example.co.uk", [_Txt(b"v=spf1 +all")], "policy_unsafe"),
        ("example.co.uk", [_Txt(b"v=spf1 all")], "policy_unsafe"),
        (
            "s1._domainkey.example.co.uk",
            [_Txt(b"v=DKIM1; p=")],
            "malformed",
        ),
        ("_dmarc.example.co.uk", [_Txt(b"v=DMARC1; p=none")], "policy_unsafe"),
        ("_dmarc.example.co.uk", [_Txt(b"v=DMARC1")], "malformed"),
    ],
)
async def test_malformed_multiple_and_unsafe_policies_are_typed(
    name: str, records: object, category: str
) -> None:
    """永久策略问题必须成为固定 facts，不能被误重试。"""
    contracts, connector = _modules()
    answers: dict[str, object] = {
        "example.co.uk": [_Txt(b"v=spf1 -all")],
        "s1._domainkey.example.co.uk": [_Txt(b"v=DKIM1; p=QUJDREVGR0hJSktMTU5PUA==")],
        "_dmarc.example.co.uk": [_Txt(b"v=DMARC1; p=reject")],
    }
    answers[name] = records
    facts = await connector.DnsAuthenticationConnector(
        _Resolver(answers), now=lambda: _NOW
    ).check(_request(contracts))
    assert any(item.category.value == category for item in facts.failures)


async def test_spf_ignores_unrelated_txt_but_rejects_invalid_mechanism() -> None:
    """SPF 候选按版本筛选；无效 mechanism 不能靠字符串前缀过关。"""
    contracts, connector = _modules()
    common = {
        "s1._domainkey.example.co.uk": [
            _Txt(b"v=DKIM1; p=QUJDREVGR0hJSktMTU5PUA==")
        ],
        "_dmarc.example.co.uk": [_Txt(b"v=DMARC1; p=reject")],
    }
    valid = await connector.DnsAuthenticationConnector(
        _Resolver(
            {
                **common,
                "example.co.uk": [
                    _Txt(b"site-verification=unrelated"),
                    _Txt(b"v=spf1 include:_spf.example.net -all"),
                ],
            }
        ),
        now=lambda: _NOW,
    ).check(_request(contracts))
    assert valid.spf_passed is True

    invalid = await connector.DnsAuthenticationConnector(
        _Resolver({**common, "example.co.uk": [_Txt(b"v=spf1 nonsense -all")]}),
        now=lambda: _NOW,
    ).check(_request(contracts))
    assert invalid.spf_passed is False
    assert invalid.failures[0].category.value == "malformed"


@pytest.mark.parametrize(
    "policy",
    [
        "v=spf1 a/24 -all",
        "v=spf1 MX/24 ~ALL",
        "v=spf1 a:mail.example.com/24//64 -all",
        "v=spf1  a/24   -all",
        "v=spf1 include:%{d}.example include:%%.example -all",
        "v=spf1 include:_spf.example.com redirect=%{d} -all",
        "v=spf1 include:_spf.example.3com -all",
        "v=spf1 include:_spf.example.3-com -all",
        "v=spf1 x-note=x x-note=x -all",
        "v=spf1 exists:%{l1r=}.example -all",
        "v=spf1 a:%{d/}.example -all",
    ],
)
async def test_spf_accepts_bounded_rfc7208_mechanism_grammar(policy: str) -> None:
    """a/mx dual CIDR 与 mechanism 大小写是 RFC 7208 的合法语法。"""
    contracts, connector = _modules()
    facts = await connector.DnsAuthenticationConnector(
        _Resolver(
            {
                "example.co.uk": [_Txt(policy.encode("ascii"))],
                "s1._domainkey.example.co.uk": [
                    _Txt(b"v=DKIM1; p=QUJDREVGR0hJSktMTU5PUA==")
                ],
                "_dmarc.example.co.uk": [_Txt(b"v=DMARC1; p=reject")],
            }
        ),
        now=lambda: _NOW,
    ).check(_request(contracts))
    assert facts.spf_passed is True


@pytest.mark.parametrize(
    "policy",
    [
        "v=spf1 -redirect=example.com",
        "v=spf1 a:example.com/999 -all",
        "v=spf1 ptr:example.com/24 -all",
        "v=spf1 a/00 -all",
        "v=spf1 a/01 -all",
        "v=spf1 mx//001 -all",
        "v=spf1 ip4:192.0.2.1/01 -all",
        "v=spf1 ip6:2001:db8::1/001 -all",
        "v=spf1 include:% -all",
        "v=spf1 include:%{ -all",
        "v=spf1 include:{} -all",
        "v=spf1 include:%{c}.example -all",
        "v=spf1 include:+ -all",
        "v=spf1 include:_ -all",
        "v=spf1 redirect=x -all",
    ],
)
async def test_spf_rejects_qualifier_modifier_and_invalid_cidr(policy: str) -> None:
    """modifier 不得带 qualifier，CIDR 必须匹配 mechanism 且在协议范围内。"""
    contracts, connector = _modules()
    facts = await connector.DnsAuthenticationConnector(
        _Resolver(
            {
                "example.co.uk": [_Txt(policy.encode("ascii"))],
                "s1._domainkey.example.co.uk": [
                    _Txt(b"v=DKIM1; p=QUJDREVGR0hJSktMTU5PUA==")
                ],
                "_dmarc.example.co.uk": [_Txt(b"v=DMARC1; p=reject")],
            }
        ),
        now=lambda: _NOW,
    ).check(_request(contracts))
    assert facts.spf_passed is False
    assert facts.failures[0].category.value == "malformed"


@pytest.mark.parametrize(
    ("name", "record"),
    [
        ("s1._domainkey.example.co.uk", b"v=DKIM1; p=not_base64%%%"),
        ("s1._domainkey.example.co.uk", b"v=DKIM1; P=QUJDREVGR0hJSktMTU5PUA=="),
        ("_dmarc.example.co.uk", b"v=DMARC1; p=reject; broken"),
    ],
)
async def test_structurally_invalid_dkim_and_dmarc_are_malformed(
    name: str, record: bytes
) -> None:
    """非 Base64 key 与无等号 DMARC tag 都是永久 malformed。"""
    contracts, connector = _modules()
    answers: dict[str, object] = {
        "example.co.uk": [_Txt(b"v=spf1 -all")],
        "s1._domainkey.example.co.uk": [
            _Txt(b"v=DKIM1; p=QUJDREVGR0hJSktMTU5PUA==")
        ],
        "_dmarc.example.co.uk": [_Txt(b"v=DMARC1; p=reject")],
    }
    answers[name] = [_Txt(record)]
    facts = await connector.DnsAuthenticationConnector(
        _Resolver(answers), now=lambda: _NOW
    ).check(_request(contracts))
    failure = next(item for item in facts.failures if item.check != "spf")
    assert failure.category.value == "malformed"


@pytest.mark.parametrize(
    "record",
    [
        b"p=QUJDREVGR0hJSktMTU5PUA==",
        b"v=DKIM1; p=QUJD REVG R0hJ SktM TU5P UA==",
    ],
)
async def test_dkim_accepts_optional_version_and_fws_base64(record: bytes) -> None:
    """DKIM v 缺省为 DKIM1，p 的 base64string 可含 bounded FWS。"""
    contracts, connector = _modules()
    facts = await connector.DnsAuthenticationConnector(
        _Resolver(
            {
                "example.co.uk": [_Txt(b"v=spf1 -all")],
                "s1._domainkey.example.co.uk": [_Txt(record)],
                "_dmarc.example.co.uk": [_Txt(b"v=DMARC1; p=reject")],
            }
        ),
        now=lambda: _NOW,
    ).check(_request(contracts))
    assert facts.dkim_passed is True


async def test_dmarc_requires_p_immediately_after_version() -> None:
    """DMARC v、p 必须作为前两个有序 tag，不能被 rua 插入。"""
    contracts, connector = _modules()
    facts = await connector.DnsAuthenticationConnector(
        _Resolver(
            {
                "example.co.uk": [_Txt(b"v=spf1 -all")],
                "s1._domainkey.example.co.uk": [
                    _Txt(b"v=DKIM1; p=QUJDREVGR0hJSktMTU5PUA==")
                ],
                "_dmarc.example.co.uk": [
                    _Txt(b"v=DMARC1; rua=mailto:dmarc@example.com; p=reject")
                ],
            }
        ),
        now=lambda: _NOW,
    ).check(_request(contracts))
    assert facts.dmarc_passed is False
    assert facts.failures[0].category.value == "malformed"


@pytest.mark.parametrize(
    "provider_error",
    [dns.exception.Timeout("raw-secret-timeout"), OSError("raw-secret-temporary")],
)
async def test_timeout_and_temporary_failure_are_retryable_and_redacted(
    provider_error: BaseException,
) -> None:
    """临时 resolver 故障可重试，但异常文本不得跨 Connector。"""
    contracts, connector = _modules()
    resolver = _Resolver({"example.co.uk": provider_error})
    with pytest.raises(TransientError) as caught:
        await connector.DnsAuthenticationConnector(resolver, now=lambda: _NOW).check(
            _request(contracts)
        )
    assert "raw-secret" not in str(caught.value)
    assert "raw-secret" not in repr(caught.value)


@pytest.mark.parametrize(
    "records",
    [
        [_Txt(b"v=spf1 -all")] * 17,
        [_Txt(b"x" * 4097)],
        [_Txt(b"x" * 1024)] * 9,
    ],
)
async def test_answer_record_and_byte_caps_fail_as_malformed_facts(
    records: object,
) -> None:
    """聚合前必须限制 answer/record/byte，防止内存放大。"""
    contracts, connector = _modules()
    answers = {
        "example.co.uk": records,
        "s1._domainkey.example.co.uk": [_Txt(b"v=DKIM1; p=QUJDREVGR0hJSktMTU5PUA==")],
        "_dmarc.example.co.uk": [_Txt(b"v=DMARC1; p=reject")],
    }
    facts = await connector.DnsAuthenticationConnector(
        _Resolver(answers), now=lambda: _NOW
    ).check(_request(contracts))
    assert facts.spf_passed is False
    assert facts.failures[0].category.value == "malformed"
