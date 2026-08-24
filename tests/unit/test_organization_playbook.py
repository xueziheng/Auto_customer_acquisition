from __future__ import annotations

import hashlib
import importlib
import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError as PydanticValidationError

from domains.approvals.service import requires_approval
from domains.organization.permissions import (
    OrganizationAction,
    OrganizationActor,
    OrganizationScope,
    OrganizationScopeLevel,
    Phase1OrganizationAuthorizer,
)
from domains.organization.schemas import PlaybookProposalCreate
from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import (
    ApprovalId,
    EmployeeId,
    IdempotencyKey,
    PlaybookActivationId,
    PlaybookVersionId,
    TenantId,
)
from shared.schemas.money import CurrencyCode, Money
from shared.schemas.provenance import SourceType

_models = importlib.import_module("domains.organization.models")
CompanyPlaybook = _models.CompanyPlaybook
CompanyPlaybookVersion = _models.CompanyPlaybookVersion
PlaybookActivation = _models.PlaybookActivation


NOW = datetime(2026, 8, 24, 9, 30, tzinfo=UTC)
TENANT = TenantId("tenant-one")
PROPOSER = EmployeeId("emp_01K00000000000000000000000")


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "company_type": " Trading Company ",
        "minimum_deal_amount": "10000.00",
        "minimum_deal_currency": "usd",
        "excluded_categories": ["Adult", "firearms"],
        "sourcing_regions": ["Guangdong", "Zhejiang"],
        "excluded_countries": ["North Korea", "IR"],
        "monthly_budget_credits": 500,
        "approval_requirements": ["catalog_reference_price"],
        "supply_capabilities_note": "  Hardware and furniture sourcing.  ",
    }
    payload.update(overrides)
    return payload


def _command(**overrides: object) -> PlaybookProposalCreate:
    return PlaybookProposalCreate.model_validate(_payload(**overrides))


def _version(
    *,
    version_id: str = "pbv_01K00000000000000000000000",
    version_number: int = 1,
    idempotency_key: str = "settings-submit-1",
    **command_overrides: object,
) -> CompanyPlaybookVersion:
    return CompanyPlaybookVersion.from_command(
        tenant_id=TENANT,
        version_id=PlaybookVersionId(version_id),
        version_number=version_number,
        command=_command(**command_overrides),
        base_version_id=None,
        base_content_hash=None,
        proposed_by=PROPOSER,
        proposed_at=NOW,
        idempotency_key=IdempotencyKey(idempotency_key),
    )


def _boss_actor(tenant_id: TenantId = TENANT) -> OrganizationActor:
    return OrganizationActor(
        actor_id=str(PROPOSER),
        scope=OrganizationScope(
            level=OrganizationScopeLevel.TENANT,
            tenant_id=tenant_id,
        ),
        role="boss",
    )


def _system_actor(tenant_id: TenantId = TENANT) -> OrganizationActor:
    return OrganizationActor(
        actor_id="system:playbook-workflow",
        scope=OrganizationScope(
            level=OrganizationScopeLevel.SYSTEM,
            tenant_id=tenant_id,
        ),
        role="system",
    )


def _active_playbook() -> CompanyPlaybook:
    return CompanyPlaybook(
        tenant_id=TENANT,
        playbook_version_id=PlaybookVersionId(
            "pbv_01K00000000000000000000000"
        ),
        activation_id=PlaybookActivationId(
            "pba_01K00000000000000000000000"
        ),
        version_number=1,
        content_hash="a" * 64,
        base_version_id=None,
        base_content_hash=None,
        approval_id="apr_01K00000000000000000000000",
        change_set_ref="playbook:pbv_01K00000000000000000000000:" + "a" * 64,
        company_type="trading_company",
        minimum_deal_value=Money(Decimal(10000), CurrencyCode("USD")),
        proposed_by=PROPOSER,
        proposed_at=NOW,
        approved_by=EmployeeId("emp_01K00000000000000000000001"),
        approved_at=NOW,
        activated_by="system:playbook-workflow",
        activated_at=NOW,
        content_provenance=_version().content_provenance,
        excluded_categories=("adult products", "firearms ammunition"),
        excluded_countries=("ir", "north korea"),
    )


def test_playbook_hash_is_stable_after_list_and_decimal_normalization() -> None:
    first = _version(
        excluded_categories=[" firearms ", "Adult", "adult"],
        minimum_deal_amount="10000.00",
    )
    second = _version(
        version_id="pbv_01K00000000000000000000001",
        version_number=2,
        idempotency_key="settings-submit-2",
        excluded_categories=["adult", "firearms"],
        minimum_deal_amount="10000",
    )

    assert first.content_hash == second.content_hash
    assert first.excluded_categories == ("adult", "firearms")


def test_playbook_hash_matches_hand_derived_canonical_business_payload() -> None:
    version = _version()
    canonical_payload = {
        "approval_requirements": ["catalog_reference_price"],
        "company_type": "trading_company",
        "excluded_categories": ["adult", "firearms"],
        "excluded_countries": ["ir", "north korea"],
        "minimum_deal_amount": "10000",
        "minimum_deal_currency": "USD",
        "monthly_budget_credits": 500,
        "sourcing_regions": ["guangdong", "zhejiang"],
        "supply_capabilities_note": "Hardware and furniture sourcing.",
    }
    expected = hashlib.sha256(
        json.dumps(
            canonical_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    assert version.content_hash == expected


def test_playbook_hash_changes_when_business_content_changes() -> None:
    first = _version(company_type="factory")
    second = _version(
        version_id="pbv_01K00000000000000000000001",
        version_number=2,
        idempotency_key="settings-submit-2",
        company_type="trading_company",
    )

    assert first.content_hash != second.content_hash


def test_playbook_hash_excludes_version_identity_and_proposal_metadata() -> None:
    first = _version()
    second = CompanyPlaybookVersion.from_command(
        tenant_id=TenantId("tenant-two"),
        version_id=PlaybookVersionId("pbv_01K00000000000000000000001"),
        version_number=99,
        command=_command(),
        base_version_id=PlaybookVersionId("pbv_01K00000000000000000000009"),
        base_content_hash="b" * 64,
        proposed_by=EmployeeId("emp_01K00000000000000000000009"),
        proposed_at=datetime(2026, 8, 25, 9, 30, tzinfo=UTC),
        idempotency_key=IdempotencyKey("different-request"),
    )

    assert first.content_hash == second.content_hash


@pytest.mark.parametrize(
    "value",
    [
        10.5,
        10,
        True,
        "NaN",
        "Infinity",
        "1e3",
        "-1",
        "01.0",
        "12345678901234567890123456789",
        "1.1234567890123",
    ],
)
def test_playbook_proposal_rejects_noncanonical_decimal_json_amount(
    value: object,
) -> None:
    with pytest.raises(PydanticValidationError):
        PlaybookProposalCreate.model_validate_json(
            json.dumps(_payload(minimum_deal_amount=value))
        )


def test_playbook_proposal_accepts_python_decimal_and_canonicalizes_signed_zero() -> None:
    command = PlaybookProposalCreate.model_validate(
        _payload(minimum_deal_amount=Decimal("-0.000"))
    )
    version = CompanyPlaybookVersion.from_command(
        tenant_id=TENANT,
        version_id=PlaybookVersionId("pbv_01K00000000000000000000000"),
        version_number=1,
        command=command,
        base_version_id=None,
        base_content_hash=None,
        proposed_by=PROPOSER,
        proposed_at=NOW,
        idempotency_key=IdempotencyKey("settings-submit-zero"),
    )

    assert version.to_view().minimum_deal_amount == "0"
    assert version.minimum_deal_value.currency == CurrencyCode("USD")


def test_playbook_proposal_rejects_identity_and_idempotency_inside_business_body() -> None:
    with pytest.raises(PydanticValidationError):
        PlaybookProposalCreate.model_validate(
            _payload(
                tenant_id="tenant-two",
                proposed_by="emp_attacker",
                idempotency_key="body-key",
            )
        )


def test_playbook_version_rejects_naive_proposal_time_instead_of_using_local_timezone() -> None:
    with pytest.raises(ValidationError):
        CompanyPlaybookVersion.from_command(
            tenant_id=TENANT,
            version_id=PlaybookVersionId("pbv_01K00000000000000000000000"),
            version_number=1,
            command=_command(),
            base_version_id=None,
            base_content_hash=None,
            proposed_by=PROPOSER,
            proposed_at=datetime(2026, 8, 24, 9, 30),  # noqa: DTZ001 - 拒绝样本
            idempotency_key=IdempotencyKey("settings-submit-naive"),
        )


def test_candidate_provenance_is_exact_employee_input_for_immutable_version() -> None:
    version = _version()

    assert version.content_provenance.source_type is SourceType.EMPLOYEE_INPUT
    assert version.content_provenance.source_id == str(version.playbook_version_id)
    assert version.content_provenance.extracted_by == f"human:{PROPOSER}"
    assert version.content_provenance.extracted_at == NOW
    assert version.content_provenance.confirmed_by is None
    assert version.content_provenance.confirmed_at is None


def test_active_playbook_combines_approval_confirmation_without_mutating_version() -> None:
    version = _version()
    approver = EmployeeId("emp_01K00000000000000000000001")
    approved_at = datetime(2026, 8, 24, 9, 35, tzinfo=UTC)
    activation = PlaybookActivation(
        tenant_id=TENANT,
        activation_id=PlaybookActivationId("pba_01K00000000000000000000000"),
        playbook_version_id=version.playbook_version_id,
        content_hash=version.content_hash,
        approval_id=ApprovalId("apr_01K00000000000000000000000"),
        change_set_ref=version.change_set_ref,
        approved_by=approver,
        approved_at=approved_at,
        activated_by="system:playbook-workflow",
        activated_at=datetime(2026, 8, 24, 9, 36, tzinfo=UTC),
    )

    active = CompanyPlaybook.from_facts(version, activation)

    assert version.content_provenance.confirmed_by is None
    assert active.content_provenance.confirmed_by == approver
    assert active.content_provenance.confirmed_at == approved_at


@pytest.mark.parametrize(
    "action",
    ["", "   ", "!quote_send", "-quote_send", "remove:quote_send", "quote\n_send"],
)
def test_approval_requirements_reject_blank_control_and_removal_shaped_actions(
    action: str,
) -> None:
    with pytest.raises(PydanticValidationError):
        _command(approval_requirements=[action])


def test_approval_requirements_are_additive_normalized_and_unknown_fail_closed() -> None:
    command = _command(
        approval_requirements=[" Custom_Action ", "custom_action", "QUOTE_SEND"]
    )

    assert command.approval_requirements == ["custom_action", "quote_send"]
    assert all(requires_approval(action) for action in command.approval_requirements)


def test_phase1_organization_authorizer_allows_only_boss_and_system_actions() -> None:
    authorizer = Phase1OrganizationAuthorizer(TENANT)

    authorizer.require(_boss_actor(), OrganizationAction.PLAYBOOK_READ, TENANT)
    authorizer.require(_boss_actor(), OrganizationAction.PLAYBOOK_PROPOSE, TENANT)
    authorizer.require(
        _system_actor(),
        OrganizationAction.PLAYBOOK_CHANGE_SNAPSHOT_READ,
        TENANT,
    )
    authorizer.require(
        _system_actor(), OrganizationAction.PLAYBOOK_ACTIVATE, TENANT
    )
    with pytest.raises(PermissionDenied):
        authorizer.require(
            _boss_actor(), OrganizationAction.PLAYBOOK_ACTIVATE, TENANT
        )
    with pytest.raises(PermissionDenied):
        authorizer.require(_system_actor(), OrganizationAction.PLAYBOOK_READ, TENANT)


def test_phase1_organization_authorizer_rejects_cross_tenant_and_manager() -> None:
    authorizer = Phase1OrganizationAuthorizer(TENANT)
    other_tenant = TenantId("tenant-two")
    manager = OrganizationActor(
        actor_id="emp_manager",
        scope=OrganizationScope(
            level=OrganizationScopeLevel.TENANT,
            tenant_id=TENANT,
        ),
        role="manager",
    )

    with pytest.raises(PermissionDenied):
        authorizer.require(
            _boss_actor(other_tenant), OrganizationAction.PLAYBOOK_READ, TENANT
        )
    with pytest.raises(PermissionDenied):
        authorizer.require(_boss_actor(), OrganizationAction.PLAYBOOK_READ, other_tenant)
    with pytest.raises(PermissionDenied):
        authorizer.require(manager, OrganizationAction.PLAYBOOK_READ, TENANT)


@pytest.mark.parametrize("actor_id", ["", " ", "x" * 201])
def test_organization_actor_rejects_blank_or_unbounded_identity(actor_id: str) -> None:
    with pytest.raises(ValidationError):
        OrganizationActor(
            actor_id=actor_id,
            scope=OrganizationScope(
                level=OrganizationScopeLevel.TENANT,
                tenant_id=TENANT,
            ),
            role="boss",
        )


def test_category_gate_is_normalized_and_conservative_for_phrase_matches() -> None:
    playbook = _active_playbook()

    assert playbook.is_category_allowed("premium ADULT-products catalog") is False
    assert playbook.is_category_allowed("Firearms ammunition accessories") is False
    assert playbook.is_category_allowed("stainless steel hinges") is True
    assert playbook.is_category_allowed("   ") is False


def test_country_gate_requires_normalized_exact_match_and_fails_closed() -> None:
    playbook = _active_playbook()

    assert playbook.is_country_allowed("  north   KOREA ") is False
    assert playbook.is_country_allowed("ir") is False
    assert playbook.is_country_allowed("Ireland") is True
    assert playbook.is_country_allowed("") is False
