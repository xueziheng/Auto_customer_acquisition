"""Hunter Domain Search 的确定性筛选、脱敏与错误分类合同。"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import date
from enum import Enum

import pytest

from connectors.contact_enrichment.client import (
    ContactEmailKind,
    ContactEnrichmentConnector,
    EnrichmentCostNote,
)
from connectors.hunter.client import (
    MANIFEST,
    HunterAuthRequiredError,
    HunterConnector,
    HunterPermanentError,
    HunterRateLimitedError,
    HunterTransientError,
    HunterUncertainError,
    _canonical_role_hints,
)
from connectors.hunter.transport import (
    HunterErrorCode,
    HunterHttpResponse,
    HunterHttpStatusError,
    HunterNetworkError,
)
from shared.errors import ValidationError

API_KEY_CANARY = "hunter-domain-secret-canary"
EMAIL_CANARY = "private-domain-email-canary@example.com"
SOURCE_CANARY = "https://source.example/private-canary"


class _Resolver:
    def __init__(self, value: str = API_KEY_CANARY) -> None:
        self.value = value
        self.refs: list[str] = []

    def resolve(self, secret_ref: str) -> str:
        self.refs.append(secret_ref)
        return self.value


class _Transport:
    def __init__(
        self,
        responses: list[HunterHttpResponse | BaseException] | None = None,
    ) -> None:
        self.responses = list(responses or [])
        self.calls: list[tuple[str, tuple[tuple[str, str], ...], str]] = []

    async def get(
        self,
        path: str,
        params: tuple[tuple[str, str], ...],
        *,
        api_key: str,
    ) -> HunterHttpResponse:
        self.calls.append((path, params, api_key))
        if not self.responses:
            raise AssertionError("unexpected Hunter request")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _source(
    suffix: str = "one",
    *,
    first_seen: str = "2026-07-01",
    last_seen: str = "2026-08-20",
    still_on_page: bool = True,
) -> dict[str, object]:
    return {
        "uri": f"https://source.example/{suffix}",
        "extracted_on": first_seen,
        "last_seen_on": last_seen,
        "still_on_page": still_on_page,
    }


def _email(
    value: str,
    *,
    kind: str = "personal",
    position: str = "Sales Manager",
    department: str = "Sales",
    seniority: str = "senior",
    sources: list[dict[str, object]] | None = None,
    **unknown: object,
) -> dict[str, object]:
    return {
        "value": value,
        "type": kind,
        "first_name": "Ada",
        "last_name": "Buyer",
        "position": position,
        "department": department,
        "seniority": seniority,
        "sources": [_source()] if sources is None else sources,
        **unknown,
    }


def _response(
    emails: list[dict[str, object]],
    *,
    results: int | None = None,
    linked_domains: list[str] | None = None,
) -> HunterHttpResponse:
    return HunterHttpResponse(
        200,
        {
            "data": {
                "emails": emails,
                "linked_domains": linked_domains or [],
            },
            "meta": {"results": len(emails) if results is None else results},
        },
    )


async def _configured(
    transport: _Transport,
) -> tuple[HunterConnector, _Resolver]:
    connector = HunterConnector(transport)
    resolver = _Resolver()
    await connector.configure(resolver)
    return connector, resolver


def _object_graph(value: object) -> tuple[set[str], list[object]]:
    keys: set[str] = set()
    values: list[object] = []
    seen: set[int] = set()

    def visit(item: object) -> None:
        identity = id(item)
        if identity in seen:
            return
        seen.add(identity)
        if isinstance(item, (str, int, float, bool, type(None), date, Enum)):
            values.append(item)
            return
        if is_dataclass(item) and not isinstance(item, type):
            for field in fields(item):
                keys.add(field.name)
                visit(getattr(item, field.name))
            return
        if isinstance(item, Mapping):
            for key, child in item.items():
                keys.add(str(key))
                visit(child)
            return
        if isinstance(item, (tuple, list, set, frozenset)):
            for child in item:
                visit(child)

    visit(value)
    return keys, values


def test_manifest_advertises_only_implemented_enrichment_and_one_secret() -> None:
    assert MANIFEST.connector_id == "hunter"
    assert MANIFEST.capabilities == ("contact.enrich", "contact.verify")
    assert MANIFEST.secret_refs == ("HUNTER_API_KEY_REF",)
    assert MANIFEST.rate_limit_note == (
        "Domain Search 15/s 500/min; Email Verifier 10/s 300/min"
    )
    assert "score" in MANIFEST.compliance_note.casefold()


@pytest.mark.asyncio
async def test_configure_resolves_exact_secret_once_and_health_uses_account() -> None:
    transport = _Transport([HunterHttpResponse(200, {"data": {}})])
    connector = HunterConnector(transport)
    resolver = _Resolver()

    assert await connector.health_check() is False
    await connector.configure(resolver)
    assert resolver.refs == ["HUNTER_API_KEY_REF"]
    assert await connector.health_check() is True
    assert transport.calls == [("/account", (), API_KEY_CANARY)]
    assert API_KEY_CANARY not in repr(connector)


@pytest.mark.asyncio
async def test_find_contacts_requires_configuration_before_network() -> None:
    transport = _Transport()
    connector = HunterConnector(transport)
    with pytest.raises(HunterAuthRequiredError):
        await connector.find_contacts("example.com", ())
    assert transport.calls == []


@pytest.mark.asyncio
async def test_domain_search_canonicalizes_idna_and_uses_one_fixed_page() -> None:
    transport = _Transport(
        [
            _response(
                [_email("BUYER@BÜCHER.example")],
                linked_domains=["RELATED.example"],
            )
        ]
    )
    connector, _ = await _configured(transport)

    result = await connector.find_contacts(" BÜCHER.example. ", ())

    assert transport.calls == [
        (
            "/domain-search",
            (("domain", "xn--bcher-kva.example"), ("limit", "10"), ("offset", "0")),
            API_KEY_CANARY,
        )
    ]
    assert len(result.candidates) == 1
    assert result.candidates[0].email == "buyer@xn--bcher-kva.example"
    assert result.provider == "hunter"
    assert result.cost_note is EnrichmentCostNote.COUNTED


@pytest.mark.asyncio
async def test_linked_domain_is_allowed_but_unrelated_domain_is_dropped() -> None:
    transport = _Transport(
        [
            _response(
                [
                    _email("linked@related.example"),
                    _email("outside@unrelated.example"),
                ],
                linked_domains=["related.example", "bad domain"],
            )
        ]
    )
    connector, _ = await _configured(transport)
    result = await connector.find_contacts("example.com", ())
    assert [candidate.email for candidate in result.candidates] == [
        "linked@related.example"
    ]


def test_role_hints_are_sorted_unique_and_canonical() -> None:
    assert _canonical_role_hints(
        ("  SALES   Manager ", "procurement", "sales manager", "采购  经理")
    ) == ("procurement", "sales manager", "采购 经理")


@pytest.mark.asyncio
async def test_role_filter_matches_position_department_or_seniority_tokens() -> None:
    emails = [
        _email("position@example.com", position="International Sales Manager"),
        _email(
            "department@example.com",
            position="Buyer",
            department="Global Procurement",
        ),
        _email(
            "seniority@example.com",
            position="Buyer",
            department="Operations",
            seniority="Executive Director",
        ),
        _email(
            "miss@example.com",
            position="Engineer",
            department="Product",
            seniority="junior",
        ),
    ]
    transport = _Transport([_response(emails)])
    connector, _ = await _configured(transport)
    result = await connector.find_contacts(
        "example.com",
        ("executive director", " SALES manager ", "global procurement"),
    )
    assert {candidate.email for candidate in result.candidates} == {
        "department@example.com",
        "position@example.com",
        "seniority@example.com",
    }


@pytest.mark.asyncio
async def test_selection_is_personal_first_canonical_sort_and_max_five() -> None:
    emails = [
        _email("z-generic@example.com", kind="generic"),
        _email("d@example.com"),
        _email("A@example.com"),
        _email("c@example.com"),
        _email("b@example.com"),
        _email("e@example.com"),
        _email("a-generic@example.com", kind="generic"),
    ]
    transport = _Transport([_response(emails)])
    connector, _ = await _configured(transport)
    result = await connector.find_contacts("example.com", ())
    assert [candidate.email for candidate in result.candidates] == [
        "a@example.com",
        "b@example.com",
        "c@example.com",
        "d@example.com",
        "e@example.com",
    ]
    assert all(
        candidate.email_kind is ContactEmailKind.PERSONAL
        for candidate in result.candidates
    )


@pytest.mark.asyncio
async def test_sources_are_validated_deduplicated_bounded_and_required() -> None:
    valid_sources = [_source(f"{index:02}") for index in range(22, -1, -1)]
    invalid_sources = [
        _source("bad-date", first_seen="not-a-date"),
        _source("reversed", first_seen="2026-08-21", last_seen="2026-08-20"),
        {"uri": "javascript:private", "extracted_on": "2026-01-01", "last_seen_on": "2026-01-02", "still_on_page": True},
        {"uri": SOURCE_CANARY},
    ]
    transport = _Transport(
        [
            _response(
                [
                    _email(
                        "kept@example.com",
                        sources=valid_sources + [valid_sources[0]] + invalid_sources,
                    ),
                    _email("dropped@example.com", sources=invalid_sources),
                ]
            )
        ]
    )
    connector, _ = await _configured(transport)
    result = await connector.find_contacts("example.com", ())
    assert [candidate.email for candidate in result.candidates] == [
        "kept@example.com"
    ]
    sources = result.candidates[0].sources
    assert len(sources) == 20
    assert sources[0].uri == "https://source.example/00"
    assert sources[-1].uri == "https://source.example/19"


@pytest.mark.asyncio
async def test_empty_result_is_success_and_cost_comes_from_meta_results() -> None:
    transport = _Transport(
        [
            _response([], results=0),
            _response([_email("outside@other.example")], results=7),
        ]
    )
    connector, _ = await _configured(transport)
    empty = await connector.find_contacts("example.com", ())
    filtered = await connector.find_contacts("example.com", ())
    assert empty.candidates == ()
    assert empty.cost_note is EnrichmentCostNote.NO_RESULT
    assert filtered.candidates == ()
    assert filtered.cost_note is EnrichmentCostNote.COUNTED


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": []},
        {"data": {}, "meta": {}},
        {"data": {"emails": "not-a-list"}, "meta": {"results": 1}},
        {"data": {"emails": []}, "meta": {"results": True}},
    ],
)
@pytest.mark.asyncio
async def test_malformed_top_level_response_is_fixed_permanent_failure(
    payload: Mapping[str, object],
) -> None:
    transport = _Transport([HunterHttpResponse(200, payload)])
    connector, _ = await _configured(transport)
    with pytest.raises(HunterPermanentError) as captured:
        await connector.find_contacts("example.com", ())
    assert str(captured.value) == "Hunter 永久失败"


@pytest.mark.asyncio
async def test_provider_scores_and_unknown_fields_never_enter_result_graph() -> None:
    transport = _Transport(
        [
            _response(
                [
                    _email(
                        EMAIL_CANARY,
                        sources=[_source("private-canary")],
                        confidence=92,
                        score=100,
                        decision_maker=True,
                        raw_private={"score": 100},
                    )
                ]
            )
        ]
    )
    connector, _ = await _configured(transport)
    result = await connector.find_contacts("example.com", ())
    keys, values = _object_graph(result)
    assert {"confidence", "score", "decision_maker", "raw_private"}.isdisjoint(keys)
    assert 92 not in values
    assert 100 not in values
    assert EMAIL_CANARY not in repr(result)
    assert SOURCE_CANARY not in repr(result)


@pytest.mark.parametrize(
    ("provider_error", "expected_type", "retry_after"),
    [
        (HunterHttpStatusError(401, HunterErrorCode.OTHER), HunterAuthRequiredError, None),
        (HunterHttpStatusError(403, HunterErrorCode.OTHER, 19), HunterRateLimitedError, 19),
        (HunterHttpStatusError(429, HunterErrorCode.OTHER, 23), HunterRateLimitedError, 23),
        (HunterHttpStatusError(451, HunterErrorCode.OTHER), HunterPermanentError, None),
        (HunterHttpStatusError(400, HunterErrorCode.OTHER), HunterPermanentError, None),
        (HunterHttpStatusError(404, HunterErrorCode.OTHER), HunterPermanentError, None),
        (HunterHttpStatusError(422, HunterErrorCode.OTHER), HunterPermanentError, None),
        (HunterHttpStatusError(500, HunterErrorCode.OTHER), HunterTransientError, None),
        (HunterNetworkError(False), HunterTransientError, None),
        (HunterNetworkError(True), HunterUncertainError, None),
    ],
)
@pytest.mark.asyncio
async def test_transport_errors_map_to_safe_connector_errors(
    provider_error: BaseException,
    expected_type: type[BaseException],
    retry_after: int | None,
) -> None:
    transport = _Transport([provider_error])
    connector, _ = await _configured(transport)
    with pytest.raises(expected_type) as captured:
        await connector.find_contacts("example.com", ())
    assert getattr(captured.value, "retry_after_seconds", None) == retry_after
    assert captured.value.__context__ is None


@pytest.mark.asyncio
async def test_invalid_local_inputs_make_no_http_call() -> None:
    transport = _Transport()
    connector, _ = await _configured(transport)
    for domain, hints in (
        ("https://example.com", ()),
        ("127.0.0.1", ()),
        ("example.com/path", ()),
        ("example.com", ("",)),
        ("example.com", tuple(str(index) for index in range(11))),
    ):
        with pytest.raises(ValidationError, match="Hunter Domain Search 输入无效"):
            await connector.find_contacts(domain, hints)
    assert transport.calls == []


@pytest.mark.asyncio
async def test_errors_logs_and_repr_do_not_expose_key_or_email(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    transport = _Transport([HunterNetworkError(True)])
    connector, _ = await _configured(transport)
    with pytest.raises(HunterUncertainError) as captured:
        await connector.find_contacts("example.com", (EMAIL_CANARY,))
    rendered = " ".join(
        (repr(connector), repr(captured.value), str(captured.value), caplog.text)
    )
    assert API_KEY_CANARY not in rendered
    assert EMAIL_CANARY not in rendered
    assert isinstance(connector, ContactEnrichmentConnector)
