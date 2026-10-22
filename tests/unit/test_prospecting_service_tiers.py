"""三档研究证据筛选：处理上限不替代需求验证与触达授权。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from domains.prospecting import service

NOW = datetime(2026, 10, 10, 8, tzinfo=UTC)
TENANT = "tenant_screening"
ACCOUNT = "pa_screening"


def _evidence(kind: str = "business_match", **overrides: Any) -> Any:
    assert hasattr(service, "ScreeningEvidence"), "缺少公开筛选证据契约"
    values: dict[str, Any] = {
        "tenant_id": TENANT,
        "account_id": ACCOUNT,
        "kind": kind,
        "source_ref": f"signal_{kind}",
        "source_url": "https://manufacturer.co.uk/news/expansion",
        "source_quote": "The company is expanding its manufacturing capacity.",
        "occurred_at": NOW - timedelta(days=10),
        "recorded_at": NOW - timedelta(days=1),
    }
    values.update(overrides)
    return service.ScreeningEvidence(**values)


def _screen(tier: str, evidence: list[Any], **overrides: Any) -> Any:
    assert hasattr(service, "evaluate_screening"), "缺少公开筛选函数"
    arguments = {
        "tier": tier,
        "tenant_id": TENANT,
        "account_id": ACCOUNT,
        "evidence": evidence,
        "now": NOW,
    }
    arguments.update(overrides)
    return service.evaluate_screening(**arguments)


def _standard_evidence() -> list[Any]:
    return [
        _evidence(kind) for kind in ("business_match", "recent_need", "contact_role")
    ]


@pytest.mark.parametrize(
    ("tier", "expected"),
    [
        ("basic", (100, 200, 20, 168, 2, 10, 10, 10, 10)),
        ("standard", (300, 600, 120, 56, 4, 20, 25, 20, 20)),
        ("advanced", (600, 1200, 240, 24, 6, 30, 40, 30, 30)),
    ],
)
def test_tier_selection_returns_authorized_processing_limits(
    tier: str, expected: tuple
) -> None:
    assert hasattr(service, "tier_policy"), "缺少公开档位政策函数"
    policy = service.tier_policy(tier)
    assert (
        policy.candidate_limit,
        policy.search_limit,
        policy.contact_limit,
        policy.cadence_hours,
        policy.max_pages_per_company,
        policy.per_run_search_limit,
        policy.per_run_page_limit,
        policy.per_run_signal_limit,
        policy.per_run_hypothesis_limit,
    ) == expected
    assert policy.period_days == 30
    assert policy.policy_version == "2026-10-10-v2"
    assert policy.tier == tier


def test_unknown_tier_does_not_fall_back_to_basic() -> None:
    assert hasattr(service, "tier_policy"), "缺少公开档位政策函数"
    with pytest.raises(ValueError):
        service.tier_policy("premium")


def test_public_source_domain_normalizes_subdomains_and_rejects_unsafe_urls() -> None:
    assert hasattr(service, "evidence_source_domain")
    assert (
        service.evidence_source_domain("https://www.MANUFACTURER.co.uk./one")
        == "manufacturer.co.uk"
    )
    assert (
        service.evidence_source_domain("https://press.manufacturer.co.uk/two")
        == "manufacturer.co.uk"
    )
    assert service.evidence_source_domain("https://8.8.8.8/proof") is None
    with pytest.raises(ValueError):
        service.evidence_source_domain("http://127.0.0.1/proof")


@pytest.mark.parametrize(
    ("tier", "expected"), [("basic", 1), ("standard", 1), ("advanced", 2)]
)
def test_tier_policy_limits_contact_processing_per_company(
    tier: str, expected: int
) -> None:
    policy = service.tier_policy(tier)
    assert getattr(policy, "max_contacts_per_company", None) == expected


@pytest.mark.parametrize("tier", ["basic", "standard", "advanced"])
def test_all_tiers_allow_automatic_enrichment_without_manual_evidence(tier: str) -> None:
    result = _screen(tier, [])
    assert result.status == "ready_for_enrichment"
    assert result.missing_requirements == ()
    assert result.satisfied_requirements == ()
    assert result.evidence_refs == ()


def test_canonical_business_evidence_needs_no_model_boolean_or_human_review() -> None:
    result = _screen("basic", [_evidence(occurred_at=None)])
    assert result.status == "ready_for_enrichment"
    assert result.satisfied_requirements == ("business_match",)
    assert result.missing_requirements == ()
    assert result.evidence_refs == ("signal_business_match",)
    assert result.policy_version == "2026-10-10-v2"
    assert result.tier == "basic"


@pytest.mark.parametrize(
    ("occurred_at", "is_recent"),
    [
        (NOW, True),
        (NOW - timedelta(days=90), True),
        (NOW - timedelta(days=90, microseconds=1), False),
        (None, False),
    ],
)
def test_recent_need_uses_event_time_not_collection_time(
    occurred_at: datetime | None,
    is_recent: bool,
) -> None:
    result = _screen(
        "standard",
        [
            _evidence(),
            _evidence("contact_role"),
            _evidence("recent_need", occurred_at=occurred_at, recorded_at=NOW),
        ],
    )
    assert result.status == "ready_for_enrichment"
    assert result.missing_requirements == ()
    assert ("recent_need" in result.satisfied_requirements) is is_recent


@pytest.mark.parametrize(
    ("confirmation_url", "is_independent"),
    [
        ("https://manufacturer.co.uk/another-story", False),
        ("https://press.manufacturer.co.uk/story", False),
        ("https://MANUFACTURER.CO.UK./story", False),
        ("https://industrynews.co.uk/story", True),
        ("https://8.8.8.8/story", False),
        ("https://industrynews.unknownsuffix/story", False),
    ],
)
def test_optional_independent_confirmation_requires_a_different_registrable_domain(
    confirmation_url: str,
    is_independent: bool,
) -> None:
    evidence = _standard_evidence() + [
        _evidence("independent_confirmation", source_url=confirmation_url),
        _evidence("human_review", reviewed_by="emp_reviewer", reviewed_at=NOW),
    ]
    result = _screen("advanced", evidence)
    assert result.status == "ready_for_enrichment"
    assert result.missing_requirements == ()
    assert ("independent_confirmation" in result.satisfied_requirements) is is_independent


def test_independence_is_relative_to_recent_need_not_arbitrary_business_evidence() -> (
    None
):
    evidence = [
        _evidence(source_url="https://othercompany.com/about"),
        _evidence("recent_need"),
        _evidence("contact_role"),
        _evidence("independent_confirmation"),
        _evidence("human_review", reviewed_by="emp_reviewer", reviewed_at=NOW),
    ]
    result = _screen("advanced", evidence)
    assert "independent_confirmation" not in result.satisfied_requirements
    assert result.missing_requirements == ()


@pytest.mark.parametrize(
    "review_fields",
    [{}, {"reviewed_by": "emp_reviewer"}, {"reviewed_at": NOW}],
)
def test_incomplete_historical_review_never_blocks_automatic_enrichment(
    review_fields: dict,
) -> None:
    evidence = _standard_evidence() + [
        _evidence(
            "independent_confirmation", source_url="https://industrynews.com/news"
        ),
        _evidence("human_review", **review_fields),
    ]
    result = _screen("advanced", evidence)
    assert result.status == "ready_for_enrichment"
    assert result.missing_requirements == ()
    assert "human_review" not in result.satisfied_requirements


@pytest.mark.parametrize("field", ["tenant_id", "account_id"])
def test_foreign_evidence_is_rejected_instead_of_silently_ignored(field: str) -> None:
    with pytest.raises(ValueError):
        _screen("basic", [_evidence(**{field: "foreign_identity"})])


@pytest.mark.parametrize("field", ["occurred_at", "recorded_at", "reviewed_at"])
def test_future_evidence_timestamps_are_rejected(field: str) -> None:
    with pytest.raises(ValueError):
        _screen("basic", [_evidence(**{field: NOW + timedelta(seconds=1)})])


@pytest.mark.parametrize("field", ["occurred_at", "recorded_at", "reviewed_at"])
def test_naive_evidence_timestamps_are_rejected(field: str) -> None:
    with pytest.raises(ValueError):
        _screen("basic", [_evidence(**{field: NOW.replace(tzinfo=None)})])


def test_naive_evaluation_clock_is_rejected_even_without_evidence() -> None:
    with pytest.raises(ValueError):
        _screen("basic", [], now=NOW.replace(tzinfo=None))


@pytest.mark.parametrize(
    "field", ["tenant_id", "account_id", "source_ref", "source_quote", "reviewed_by"]
)
def test_blank_provenance_or_identity_is_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        _evidence(**{field: "  "})


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/proof",
        "http://127.0.0.1/proof",
        "https://user:secret@example.com/proof",
    ],
)
def test_non_public_or_credential_bearing_evidence_url_is_rejected(url: str) -> None:
    with pytest.raises(ValidationError):
        _evidence(source_url=url)


def test_evidence_reference_order_is_stable_and_duplicate_refs_are_deduplicated() -> (
    None
):
    first = _evidence(source_ref="signal_z")
    second = _evidence(source_ref="signal_a")
    forward = _screen("basic", [first, second, first])
    reverse = _screen("basic", [second, first])
    assert forward == reverse
    assert forward.evidence_refs == ("signal_a", "signal_z")


def test_contracts_are_immutable_and_reject_unrecognized_claims() -> None:
    evidence = _evidence()
    policy = service.tier_policy("basic")
    assessment = _screen("basic", [evidence])
    for value, field, replacement in (
        (evidence, "source_quote", "替换原文"),
        (policy, "tier", "advanced"),
        (assessment, "status", "needs_evidence"),
    ):
        with pytest.raises(ValidationError):
            setattr(value, field, replacement)
        with pytest.raises(ValidationError):
            type(value).model_validate(value.model_dump() | {"validated_need": True})
    with pytest.raises(ValidationError):
        _evidence(business_match=True)
