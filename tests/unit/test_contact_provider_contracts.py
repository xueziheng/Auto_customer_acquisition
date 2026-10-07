"""联系人 Provider 公共 typed 契约与 PII 表示安全。"""

from __future__ import annotations

import importlib
import inspect
from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime
from types import ModuleType, SimpleNamespace
from typing import get_type_hints

import pytest

from shared.errors import ValidationError

_enrichment = importlib.import_module("connectors.contact_enrichment.client")
_verification = importlib.import_module("connectors.email_verification.client")

_ENRICHMENT_EXPORTS = {
    "ContactEmailKind",
    "ContactSource",
    "ContactCandidate",
    "EnrichmentCostNote",
    "ContactEnrichmentResult",
    "ContactEnrichmentConnector",
}
_VERIFICATION_EXPORTS = {
    "EmailVerificationOutcome",
    "VerificationCostNote",
    "EmailVerificationResult",
    "EmailVerificationConnector",
}


def _types(module: ModuleType, names: set[str]) -> SimpleNamespace:
    missing = names - set(dir(module))
    if missing:
        pytest.skip(f"typed exports pending: {sorted(missing)}")
    return SimpleNamespace(**{name: getattr(module, name) for name in names})


def _contact_types() -> SimpleNamespace:
    return _types(_enrichment, _ENRICHMENT_EXPORTS)


def _verification_types() -> SimpleNamespace:
    return _types(_verification, _VERIFICATION_EXPORTS)


def test_typed_contract_exports_exist_without_generic_manifests() -> None:
    assert _ENRICHMENT_EXPORTS <= set(dir(_enrichment))
    assert _VERIFICATION_EXPORTS <= set(dir(_verification))
    assert not hasattr(_enrichment, "MANIFEST")
    assert not hasattr(_verification, "MANIFEST")


def test_contact_result_object_graph_repr_hides_pii_and_evidence() -> None:
    types = _contact_types()
    source_canary = "https://source-canary.example/people/private"
    email_canary = "private-email-canary@example.com"
    name_canary = "Private Name Canary"
    role_canary = "Private Role Canary"
    source = types.ContactSource(
        source_canary,
        date(2026, 8, 1),
        date(2026, 8, 20),
        True,
    )
    candidate = types.ContactCandidate(
        email_canary,
        name_canary,
        role_canary,
        types.ContactEmailKind.PERSONAL,
        (source,),
    )
    result = types.ContactEnrichmentResult(
        (candidate,), "hunter", types.EnrichmentCostNote.COUNTED
    )
    rendered = " ".join(repr(value) for value in (source, candidate, result))
    for canary in (source_canary, email_canary, name_canary, role_canary):
        assert canary not in rendered


def test_contact_contract_defensively_copies_input_collections() -> None:
    types = _contact_types()
    source = types.ContactSource(
        "https://example.com/contact", date(2026, 8, 1), date(2026, 8, 20), True
    )
    source_input = [source]
    candidate = types.ContactCandidate(
        "buyer@example.com",
        sources=source_input,  # type: ignore[arg-type]
    )
    source_input.clear()
    candidate_input = [candidate]
    result = types.ContactEnrichmentResult(
        candidate_input,  # type: ignore[arg-type]
        "hunter",
        types.EnrichmentCostNote.COUNTED,
    )
    candidate_input.clear()
    assert candidate.sources == (source,)
    assert result.candidates == (candidate,)


def test_contact_contracts_are_frozen() -> None:
    types = _contact_types()
    source = types.ContactSource(
        "https://example.com/contact", date(2026, 8, 1), date(2026, 8, 20), True
    )
    candidate = types.ContactCandidate("buyer@example.com", sources=(source,))
    result = types.ContactEnrichmentResult(
        (candidate,), "hunter", types.EnrichmentCostNote.COUNTED
    )
    with pytest.raises(FrozenInstanceError):
        source.uri = "https://changed.example"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        candidate.email = "changed@example.com"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.provider = "changed"  # type: ignore[misc]


def test_contact_source_rejects_reversed_dates() -> None:
    types = _contact_types()
    with pytest.raises(ValidationError, match="联系人来源无效"):
        types.ContactSource(
            "https://example.com/contact",
            date(2026, 8, 20),
            date(2026, 8, 1),
            True,
        )


def test_contact_candidate_rejects_missing_source() -> None:
    types = _contact_types()
    with pytest.raises(ValidationError, match="联系人候选无效"):
        types.ContactCandidate("buyer@example.com", sources=())


def test_contact_candidate_rejects_invalid_email_kind() -> None:
    types = _contact_types()
    source = types.ContactSource(
        "https://example.com/contact", date(2026, 8, 1), date(2026, 8, 20), True
    )
    with pytest.raises(ValidationError, match="联系人候选无效"):
        types.ContactCandidate(
            "buyer@example.com",
            email_kind="personal",  # type: ignore[arg-type]
            sources=(source,),
        )


def test_contact_result_rejects_blank_provider_and_raw_dict_candidate() -> None:
    types = _contact_types()
    with pytest.raises(ValidationError, match="联系人补全结果无效"):
        types.ContactEnrichmentResult((), "", types.EnrichmentCostNote.NO_RESULT)
    with pytest.raises(ValidationError, match="联系人补全结果无效"):
        types.ContactEnrichmentResult(
            ({"email": "buyer@example.com"},),  # type: ignore[arg-type]
            "hunter",
            types.EnrichmentCostNote.COUNTED,
        )


def test_enrichment_protocol_is_typed_and_runtime_checkable() -> None:
    types = _contact_types()
    hints = get_type_hints(types.ContactEnrichmentConnector.find_contacts)
    parameters = inspect.signature(
        types.ContactEnrichmentConnector.find_contacts
    ).parameters
    assert tuple(parameters) == ("self", "company_domain", "role_hints")
    assert hints["role_hints"] == tuple[str, ...]
    assert hints["return"] is types.ContactEnrichmentResult

    class StructuralConnector:
        async def find_contacts(
            self, company_domain: str, role_hints: tuple[str, ...]
        ) -> object:
            del company_domain, role_hints
            raise NotImplementedError

    assert isinstance(StructuralConnector(), types.ContactEnrichmentConnector)


def test_contact_contract_enum_values_are_fixed_labels() -> None:
    types = _contact_types()
    assert {item.value for item in types.ContactEmailKind} == {
        "personal",
        "generic",
    }
    assert {item.value for item in types.EnrichmentCostNote} == {
        "hunter.domain_search.counted",
        "hunter.domain_search.no_result",
    }


def test_verification_result_repr_hides_email_adjacent_payload_and_is_frozen() -> None:
    types = _verification_types()
    result = types.EmailVerificationResult(
        types.EmailVerificationOutcome.VERIFIED,
        "hunter",
        datetime(2026, 8, 21, tzinfo=UTC),
        types.VerificationCostNote.COUNTED,
    )
    assert "hunter" not in repr(result)
    with pytest.raises(FrozenInstanceError):
        result.provider = "changed"  # type: ignore[misc]


def test_verification_result_requires_utc_checked_at() -> None:
    types = _verification_types()
    with pytest.raises(ValidationError, match="邮箱验证结果无效"):
        types.EmailVerificationResult(
            types.EmailVerificationOutcome.UNVERIFIED,
            "hunter",
            datetime(2026, 8, 21),  # noqa: DTZ001 - 验证拒绝 naive 时间
            types.VerificationCostNote.UNKNOWN,
        )


def test_verification_result_rejects_invalid_outcome_cost_and_provider() -> None:
    types = _verification_types()
    checked_at = datetime(2026, 8, 21, tzinfo=UTC)
    with pytest.raises(ValidationError, match="邮箱验证结果无效"):
        types.EmailVerificationResult(
            "valid",  # type: ignore[arg-type]
            "hunter",
            checked_at,
            types.VerificationCostNote.COUNTED,
        )
    with pytest.raises(ValidationError, match="邮箱验证结果无效"):
        types.EmailVerificationResult(
            types.EmailVerificationOutcome.VERIFIED,
            "",
            checked_at,
            types.VerificationCostNote.COUNTED,
        )
    with pytest.raises(ValidationError, match="邮箱验证结果无效"):
        types.EmailVerificationResult(
            types.EmailVerificationOutcome.VERIFIED,
            "hunter",
            checked_at,
            "provider-text",  # type: ignore[arg-type]
        )


def test_verification_protocol_is_typed_and_runtime_checkable() -> None:
    types = _verification_types()
    hints = get_type_hints(types.EmailVerificationConnector.verify)
    parameters = inspect.signature(types.EmailVerificationConnector.verify).parameters
    assert tuple(parameters) == ("self", "email")
    assert hints["email"] is str
    assert hints["return"] is types.EmailVerificationResult

    class StructuralConnector:
        async def verify(self, email: str) -> object:
            del email
            raise NotImplementedError

    assert isinstance(StructuralConnector(), types.EmailVerificationConnector)


def test_verification_contract_enum_values_are_fixed_labels() -> None:
    types = _verification_types()
    assert {item.value for item in types.EmailVerificationOutcome} == {
        "verified",
        "invalid",
        "risky",
        "unverified",
    }
    assert {item.value for item in types.VerificationCostNote} == {
        "hunter.email_verifier.counted",
        "hunter.email_verifier.privacy_refused",
        "hunter.email_verifier.unknown",
        "hunter.email_verifier.cache_hit",
    }
