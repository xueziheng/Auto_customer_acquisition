"""prospecting 公共 DTO 与 Protocol 不泄漏内部实体。"""

from __future__ import annotations

import importlib
from dataclasses import FrozenInstanceError, fields
from datetime import UTC, datetime
from typing import get_type_hints

import pytest

from domains.prospecting.schemas import (
    AccountResolveRequest,
    ContactCreateRequest,
    ContactPointCreateRequest,
    ContactPointKind,
    ContactPointView,
    ContactType,
    LegalBasisInput,
    LegalBasisType,
    ProspectAccountView,
    SubjectType,
    VerificationStatus,
)
from domains.prospecting.service import ProspectingService
from shared.schemas.identifiers import (
    ContactPointId,
    ProspectAccountId,
    ProspectContactId,
)

_schemas = importlib.import_module("domains.prospecting.schemas")
CHECKED_AT = datetime(2026, 8, 21, tzinfo=UTC)


def test_public_requests_are_constructible_without_internal_models() -> None:
    basis = LegalBasisInput(
        basis=LegalBasisType.LEGITIMATE_INTEREST,
        subject_type=SubjectType.LEGAL_ENTITY,
        contact_type=ContactType.ROLE_BASED,
        source="company_website",
        collected_at=datetime(2026, 8, 20, tzinfo=UTC),
        assessment_ref="lia_2026_001",
    )
    account = AccountResolveRequest(
        entity_name="Acme Manufacturing",
        country="DE",
        website_domain="acme.example",
        source_signal_refs=("sig_01K0000000000000000000000",),
    )
    contact = ContactCreateRequest(account_id=ProspectAccountId("acc-1"))
    point = ContactPointCreateRequest(
        contact_id=ProspectContactId("pc-1"),
        kind=ContactPointKind.EMAIL,
        value="sales@acme.example",
        legal_basis=basis,
    )
    assert account.website_domain == "acme.example"
    assert contact.account_id == ProspectAccountId("acc-1")
    assert point.legal_basis is basis


def test_service_public_annotations_use_views_and_requests() -> None:
    resolve = get_type_hints(ProspectingService.resolve_account)
    add_point = get_type_hints(ProspectingService.add_contact_point)
    get_account = get_type_hints(ProspectingService.get_account)

    assert resolve["request"] is AccountResolveRequest
    assert add_point["request"] is ContactPointCreateRequest
    assert get_account["return"] is ProspectAccountView


def test_public_request_collections_are_immutable() -> None:
    request = AccountResolveRequest(
        entity_name="Acme",
        country="US",
        source_signal_refs=("sig-1", "sig-2"),
    )
    assert request.source_signal_refs == ("sig-1", "sig-2")


def test_verification_record_request_is_frozen() -> None:
    request_type = getattr(_schemas, "VerificationRecordRequest", None)
    assert request_type is not None
    request = request_type(
        ContactPointId("cp_01J00000000000000000000000"),
        VerificationStatus.VERIFIED,
        "hunter",
        CHECKED_AT,
        "hunter.email_verifier.counted",
    )
    with pytest.raises(FrozenInstanceError):
        request.provider = "changed"


def test_contact_point_view_exposes_complete_verification_observation() -> None:
    field_names = {item.name for item in fields(ContactPointView)}
    assert {"verification_checked_at", "verification_cost_note"} <= field_names


def test_contact_point_verified_is_registered_for_transactional_outbox() -> None:
    from infra.db.outbox import resolve_event_type
    from shared.events.catalog import ContactPointVerified

    assert resolve_event_type("ContactPointVerified") is ContactPointVerified
