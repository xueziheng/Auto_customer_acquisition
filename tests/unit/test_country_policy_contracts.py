"""国家政策包的纯契约、来源边界、不可变事实与权限矩阵。"""

from __future__ import annotations

import hashlib
import importlib
import inspect
import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from typing import get_type_hints

import pytest
from pydantic import ValidationError as PydanticValidationError

from domains.approvals.service import ApprovalType
from domains.compliance.schemas import (
    CountryPolicyAction,
    CountryPolicyActivationView,
    CountryPolicyApprovalFact,
    CountryPolicyCoverage,
    CountryPolicyDecision,
    CountryPolicyField,
    CountryPolicyFieldSourceInput,
    CountryPolicyProposalCreate,
    CountryPolicyVersionView,
    normalize_country_key,
)
from domains.compliance.service import (
    ComplianceAction,
    ComplianceActor,
    ComplianceAuthorizer,
    ComplianceScope,
    Phase1ComplianceAuthorizer,
)
from infra.db.outbox import EVENT_REGISTRY
from shared.errors import PermissionDenied, ValidationError
from shared.events.catalog import CountryPolicyVersionProposed, DomainEvent
from shared.schemas.identifiers import (
    ApprovalId,
    CountryPolicyActivationId,
    CountryPolicyVersionId,
    EmployeeId,
    IdempotencyKey,
    TenantId,
)
from shared.schemas.provenance import Provenance, SourceType

_models = importlib.import_module("domains.compliance.models")
_repository = importlib.import_module("domains.compliance.repository")
CountryPolicyVersion = _models.CountryPolicyVersion
CountryPolicyActivation = _models.CountryPolicyActivation

NOW = datetime(2026, 8, 24, 9, 30, tzinfo=UTC)
TENANT = TenantId("tenant-synthetic-policy")
PROPOSER = EmployeeId("emp_01K00000000000000000000000")
APPROVER = EmployeeId("emp_01K00000000000000000000001")
VERSION_ID = CountryPolicyVersionId("cpp_01K00000000000000000000000")
ACTIVATION_ID = CountryPolicyActivationId("cpa_01K00000000000000000000000")

DECISION_FIELDS = {
    CountryPolicyField.PUBLIC_RESEARCH_ALLOWED,
    CountryPolicyField.CONTACT_ENRICHMENT_ALLOWED,
    CountryPolicyField.COLD_B2B_EMAIL_ALLOWED,
    CountryPolicyField.PERSONAL_DATA_BASIS_REQUIRED,
    CountryPolicyField.SUBJECT_TYPE_AFFECTS_JUDGMENT,
    CountryPolicyField.CONTACT_TYPE_AFFECTS_JUDGMENT,
    CountryPolicyField.OPT_OUT_DEADLINE_DAYS,
    CountryPolicyField.LOCAL_REPRESENTATIVE_REQUIRED,
    CountryPolicyField.REQUIREMENTS,
}


def _employee_source(index: int) -> dict[str, object]:
    return {
        "source_type": SourceType.EMPLOYEE_INPUT,
        "source_id": f"assessment:synthetic:{index}",
    }


def valid_payload() -> dict[str, object]:
    return {
        "country": "Synthetic Market",
        "public_research_allowed": True,
        "contact_enrichment_allowed": False,
        "cold_b2b_email_allowed": False,
        "personal_data_basis_required": True,
        "subject_type_affects_judgment": True,
        "contact_type_affects_judgment": True,
        "opt_out_deadline_days": 30,
        "local_representative_required": False,
        "requirements": ["retain.assessment_ref", "honor_opt_out"],
        "notes": "Synthetic policy fixture reviewed by an employee.",
        "field_sources": {
            field: _employee_source(index)
            for index, field in enumerate(sorted(DECISION_FIELDS, key=lambda item: item.value), 1)
        },
    }


def _command(**overrides: object) -> CountryPolicyProposalCreate:
    payload = valid_payload()
    payload.update(overrides)
    return CountryPolicyProposalCreate.model_validate(payload)


def _version(**overrides: object) -> CountryPolicyVersion:
    values: dict[str, object] = {
        "tenant_id": TENANT,
        "version_id": VERSION_ID,
        "version_number": 1,
        "command": _command(),
        "base_version_id": None,
        "base_content_hash": None,
        "proposed_by": PROPOSER,
        "proposed_at": NOW,
        "idempotency_key": IdempotencyKey("country-policy-proposal-1"),
    }
    values.update(overrides)
    return CountryPolicyVersion.from_command(**values)


def test_country_key_is_exact_deterministic_normalization() -> None:
    assert normalize_country_key("  SYNTHETIC\u3000Market  ") == "synthetic market"
    assert normalize_country_key("Synthetic Alpha") != normalize_country_key(
        "Synthetic Beta"
    )


@pytest.mark.parametrize("country", ["", "   ", "Synthetic\nMarket", "x" * 65])
def test_country_rejects_blank_control_or_overlong_values(country: str) -> None:
    with pytest.raises(PydanticValidationError):
        _command(country=country)


def test_policy_requires_source_for_every_decision_field() -> None:
    payload = valid_payload()
    sources = payload["field_sources"]
    assert isinstance(sources, dict)
    sources.pop(CountryPolicyField.CONTACT_ENRICHMENT_ALLOWED)
    with pytest.raises(PydanticValidationError):
        CountryPolicyProposalCreate.model_validate(payload)


def test_policy_constructs_a_separate_safe_source_for_every_decision_field() -> None:
    proposal = _command()

    assert set(proposal.field_sources) == DECISION_FIELDS
    assert len({source.source_id for source in proposal.field_sources.values()}) == 9


def test_policy_rejects_untrusted_sources_and_identity_facts() -> None:
    agent_payload = valid_payload()
    sources = agent_payload["field_sources"]
    assert isinstance(sources, dict)
    sources[CountryPolicyField.PUBLIC_RESEARCH_ALLOWED] = {
        "source_type": SourceType.AGENT_INFERENCE,
        "source_id": "agent:guess:1",
    }
    with pytest.raises(PydanticValidationError):
        CountryPolicyProposalCreate.model_validate(agent_payload)

    identity_payload = valid_payload()
    identity_sources = identity_payload["field_sources"]
    assert isinstance(identity_sources, dict)
    identity_sources[CountryPolicyField.PUBLIC_RESEARCH_ALLOWED] = {
        **_employee_source(99),
        "confirmed_by": str(PROPOSER),
        "confirmed_at": NOW.isoformat(),
        "extracted_by": "human:attacker",
        "extracted_at": NOW.isoformat(),
    }
    with pytest.raises(PydanticValidationError):
        CountryPolicyProposalCreate.model_validate(identity_payload)


@pytest.mark.parametrize(
    "field",
    [
        "public_research_allowed",
        "contact_enrichment_allowed",
        "cold_b2b_email_allowed",
        "personal_data_basis_required",
        "subject_type_affects_judgment",
        "contact_type_affects_judgment",
        "local_representative_required",
    ],
)
def test_policy_boolean_fields_are_strict_and_required(field: str) -> None:
    payload = valid_payload()
    payload[field] = 1
    with pytest.raises(PydanticValidationError):
        CountryPolicyProposalCreate.model_validate(payload)

    payload = valid_payload()
    payload.pop(field)
    with pytest.raises(PydanticValidationError):
        CountryPolicyProposalCreate.model_validate(payload)


@pytest.mark.parametrize("value", [0, 366, True])
def test_opt_out_deadline_is_a_bounded_strict_integer(value: object) -> None:
    with pytest.raises(PydanticValidationError):
        _command(opt_out_deadline_days=value)


@pytest.mark.parametrize(
    "requirements",
    [
        [""],
        ["1starts_with_digit"],
        ["starts with spaces"],
        ["-removal"],
        ["a" * 129],
        ["honor_opt_out", " honor_opt_out "],
    ],
)
def test_requirements_reject_malformed_or_normalized_duplicates(
    requirements: list[str],
) -> None:
    with pytest.raises(PydanticValidationError):
        _command(requirements=requirements)


def test_requirements_are_normalized_and_sorted_without_hidden_logic() -> None:
    proposal = _command(
        requirements=["RETAIN.ASSESSMENT_REF", "honor_opt_out"]
    )

    assert proposal.requirements == ["honor_opt_out", "retain.assessment_ref"]


def test_notes_are_required_bounded_and_not_a_hidden_default() -> None:
    payload = valid_payload()
    payload.pop("notes")
    with pytest.raises(PydanticValidationError):
        CountryPolicyProposalCreate.model_validate(payload)
    for value in ("", "Synthetic\npolicy", "x" * 4001):
        with pytest.raises(PydanticValidationError):
            _command(notes=value)


@pytest.mark.parametrize(
    "source_id",
    ["", "1does-not-start-with-a-letter", "contains space", "a" * 201],
)
def test_source_ids_are_safe_bounded_references(source_id: str) -> None:
    with pytest.raises(PydanticValidationError):
        CountryPolicyFieldSourceInput(
            source_type=SourceType.EMPLOYEE_INPUT,
            source_id=source_id,
        )


def test_web_source_requires_https_without_credentials_and_lowercase_page_hash() -> None:
    valid = CountryPolicyFieldSourceInput(
        source_type=SourceType.WEB_PAGE,
        source_id="page:synthetic:1",
        source_url="https://research.invalid/synthetic/policy",
        page_hash="a" * 64,
    )
    assert valid.page_hash == "a" * 64

    invalid_urls = (
        "http://research.invalid/policy",
        "https://user:password@research.invalid/policy",
        "https://research.invalid/" + "x" * 2030,
    )
    for url in invalid_urls:
        with pytest.raises(PydanticValidationError):
            CountryPolicyFieldSourceInput(
                source_type=SourceType.WEB_PAGE,
                source_id="page:synthetic:1",
                source_url=url,
                page_hash="a" * 64,
            )

    for page_hash in (None, "A" * 64, "a" * 63, "g" * 64):
        with pytest.raises(PydanticValidationError):
            CountryPolicyFieldSourceInput(
                source_type=SourceType.WEB_PAGE,
                source_id="page:synthetic:1",
                source_url="https://research.invalid/synthetic/policy",
                page_hash=page_hash,
            )


@pytest.mark.parametrize(
    "source_type", [SourceType.UPLOAD, SourceType.EMPLOYEE_INPUT]
)
def test_non_web_sources_forbid_webpage_only_fields(source_type: SourceType) -> None:
    with pytest.raises(PydanticValidationError):
        CountryPolicyFieldSourceInput(
            source_type=source_type,
            source_id="assessment:synthetic:1",
            source_url="https://research.invalid/synthetic/policy",
            page_hash="a" * 64,
        )


def test_policy_content_hash_is_deterministic_and_includes_safe_sources() -> None:
    command = _command()
    canonical = {
        "cold_b2b_email_allowed": False,
        "contact_enrichment_allowed": False,
        "country": "Synthetic Market",
        "country_key": "synthetic market",
        "field_sources": {
            field.value: {
                "page_hash": None,
                "source_id": command.field_sources[field].source_id,
                "source_type": "employee_input",
                "source_url": None,
            }
            for field in sorted(DECISION_FIELDS, key=lambda item: item.value)
        },
        "local_representative_required": False,
        "notes": "Synthetic policy fixture reviewed by an employee.",
        "opt_out_deadline_days": 30,
        "personal_data_basis_required": True,
        "public_research_allowed": True,
        "requirements": ["honor_opt_out", "retain.assessment_ref"],
        "subject_type_affects_judgment": True,
        "contact_type_affects_judgment": True,
    }
    expected = hashlib.sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    assert CountryPolicyVersion.content_hash_for(command) == expected
    changed = valid_payload()
    changed_sources = changed["field_sources"]
    assert isinstance(changed_sources, dict)
    changed_sources[CountryPolicyField.REQUIREMENTS] = _employee_source(99)
    assert CountryPolicyVersion.content_hash_for(
        CountryPolicyProposalCreate.model_validate(changed)
    ) != expected


def test_version_binds_every_field_source_to_human_confirmed_provenance() -> None:
    version = _version()

    assert version.country_key == "synthetic market"
    assert set(version.field_provenance) == DECISION_FIELDS
    for field, provenance in version.field_provenance.items():
        assert provenance.source_id == _command().field_sources[field].source_id
        assert provenance.extracted_by == f"human:{PROPOSER}"
        assert provenance.extracted_at == NOW
        assert provenance.confirmed_by == PROPOSER
        assert provenance.confirmed_at == NOW
        assert provenance.is_human_confirmed is True
    assert version.change_set_ref == f"country_policy:{VERSION_ID}:{version.content_hash}"
    assert version.to_view().field_provenance == version.field_provenance

    with pytest.raises(FrozenInstanceError):
        version.version_number = 2


def _agent_inference_provenance() -> Provenance:
    return Provenance(
        source_type=SourceType.AGENT_INFERENCE,
        source_id="inference:synthetic:1",
        extracted_by=f"human:{PROPOSER}",
        extracted_at=NOW,
        confirmed_by=PROPOSER,
        confirmed_at=NOW,
    )


def test_rehydrated_version_rejects_human_confirmed_agent_inference() -> None:
    version = _version()
    provenance = dict(version.field_provenance)
    provenance[CountryPolicyField.PUBLIC_RESEARCH_ALLOWED] = (
        _agent_inference_provenance()
    )

    with pytest.raises(ValidationError):
        replace(version, field_provenance=provenance)


def test_version_view_rejects_human_confirmed_agent_inference() -> None:
    view = _version().to_view()
    values = {
        name: getattr(view, name) for name in CountryPolicyVersionView.model_fields
    }
    provenance = dict(view.field_provenance)
    provenance[CountryPolicyField.CONTACT_ENRICHMENT_ALLOWED] = (
        _agent_inference_provenance()
    )
    values["field_provenance"] = provenance

    with pytest.raises(PydanticValidationError):
        CountryPolicyVersionView(**values)


def test_activation_is_an_append_only_exact_fact_and_maps_to_public_view() -> None:
    version = _version()
    activation = CountryPolicyActivation(
        tenant_id=TENANT,
        activation_id=ACTIVATION_ID,
        activation_sequence=1,
        country_key=version.country_key,
        country_policy_version_id=version.country_policy_version_id,
        content_hash=version.content_hash,
        approval_id=ApprovalId("apr_01K00000000000000000000000"),
        change_set_ref=version.change_set_ref,
        approved_by=APPROVER,
        approved_at=NOW,
        activated_by="system:country-policy-workflow",
        activated_at=NOW,
    )

    view = activation.to_view()
    assert isinstance(view, CountryPolicyActivationView)
    assert view.activation_sequence == 1
    assert view.country_policy_version_id == VERSION_ID
    assert view.approved_by == APPROVER
    with pytest.raises(FrozenInstanceError):
        activation.activation_sequence = 2


def test_decision_rejects_incoherent_configured_and_active_facts() -> None:
    with pytest.raises(PydanticValidationError):
        CountryPolicyDecision(
            country_key="synthetic market",
            action=CountryPolicyAction.CONTACT_ENRICHMENT,
            configured=False,
            allowed=True,
            active_version_id=None,
            content_hash=None,
            requirements=(),
        )
    with pytest.raises(PydanticValidationError):
        CountryPolicyDecision(
            country_key="synthetic market",
            action=CountryPolicyAction.CONTACT_ENRICHMENT,
            configured=False,
            allowed=False,
            active_version_id=VERSION_ID,
            content_hash="a" * 64,
            requirements=(),
        )
    with pytest.raises(PydanticValidationError):
        CountryPolicyDecision(
            country_key="synthetic market",
            action=CountryPolicyAction.CONTACT_ENRICHMENT,
            configured=True,
            allowed=False,
            active_version_id=VERSION_ID,
            content_hash=None,
            requirements=(),
        )


def test_coverage_is_nonnegative_and_enrichment_is_a_subset() -> None:
    assert CountryPolicyCoverage(
        active_policy_count=2,
        contact_enrichment_allowed_count=1,
    ).active_policy_count == 2
    with pytest.raises(PydanticValidationError):
        CountryPolicyCoverage(
            active_policy_count=1,
            contact_enrichment_allowed_count=2,
        )


def _actor(role: str, scope: ComplianceScope, tenant: TenantId = TENANT) -> ComplianceActor:
    return ComplianceActor(
        actor_id="system:policy" if role == "system" else str(PROPOSER),
        tenant_id=tenant,
        scope=scope,
        role=role,
    )


def test_phase1_compliance_authorizer_implements_the_exact_matrix() -> None:
    authorizer = Phase1ComplianceAuthorizer(TENANT)
    boss = _actor("boss", ComplianceScope.TENANT)
    system = _actor("system", ComplianceScope.SYSTEM)

    for action in (
        ComplianceAction.COUNTRY_POLICY_READ,
        ComplianceAction.COUNTRY_POLICY_PROPOSE,
    ):
        authorizer.require(boss, action, ComplianceScope.TENANT, TENANT)
    for action in (
        ComplianceAction.COUNTRY_POLICY_DECIDE,
        ComplianceAction.COUNTRY_POLICY_CHANGE_SNAPSHOT_READ,
        ComplianceAction.COUNTRY_POLICY_ACTIVATE,
    ):
        authorizer.require(system, action, ComplianceScope.SYSTEM, TENANT)

    with pytest.raises(PermissionDenied):
        authorizer.require(
            boss,
            ComplianceAction.COUNTRY_POLICY_ACTIVATE,
            ComplianceScope.TENANT,
            TENANT,
        )
    with pytest.raises(PermissionDenied):
        authorizer.require(
            system,
            ComplianceAction.COUNTRY_POLICY_READ,
            ComplianceScope.SYSTEM,
            TENANT,
        )


def test_authorizer_requires_actor_scope_authorizer_and_method_tenant_to_match() -> None:
    authorizer = Phase1ComplianceAuthorizer(TENANT)
    boss = _actor("boss", ComplianceScope.TENANT)
    other = TenantId("tenant-other")

    with pytest.raises(PermissionDenied):
        authorizer.require(
            boss,
            ComplianceAction.COUNTRY_POLICY_READ,
            ComplianceScope.SYSTEM,
            TENANT,
        )
    with pytest.raises(PermissionDenied):
        authorizer.require(
            _actor("boss", ComplianceScope.TENANT, other),
            ComplianceAction.COUNTRY_POLICY_READ,
            ComplianceScope.TENANT,
            TENANT,
        )
    with pytest.raises(PermissionDenied):
        authorizer.require(
            boss,
            ComplianceAction.COUNTRY_POLICY_READ,
            ComplianceScope.TENANT,
            other,
        )


def test_request_contract_has_no_system_actor_convenience_constructor() -> None:
    assert "system" not in ComplianceActor.__dict__
    assert "system_actor" not in ComplianceActor.__dict__


def test_repository_and_service_protocols_expose_tenant_scoped_contracts() -> None:
    uow = _repository.ComplianceUnitOfWork
    attrs = set(getattr(uow, "__protocol_attrs__", ()))
    assert {"versions", "provenance", "activations", "bus", "__aenter__", "__aexit__"} <= attrs

    for protocol_name in (
        "CountryPolicyVersionRepository",
        "CountryPolicyFieldProvenanceRepository",
        "CountryPolicyActivationRepository",
    ):
        protocol = getattr(_repository, protocol_name)
        for name, member in inspect.getmembers(protocol, inspect.isfunction):
            if name.startswith("_"):
                continue
            assert "tenant_id" in inspect.signature(member).parameters, (protocol_name, name)

    service = importlib.import_module("domains.compliance.service").ComplianceService
    methods = {
        "get_active_policy",
        "get_version",
        "list_active_policies",
        "list_versions",
        "get_country_policy_decision",
        "get_coverage",
        "get_change_snapshot",
        "propose_country_policy",
        "activate_country_policy",
    }
    assert methods <= set(getattr(service, "__protocol_attrs__", ()))
    approval_hint = get_type_hints(service.activate_country_policy)["approval"]
    assert approval_hint is CountryPolicyApprovalFact


def test_service_module_reexports_the_permission_contracts_for_consumers() -> None:
    service_module = importlib.import_module("domains.compliance.service")
    expected = {
        "ComplianceAction": ComplianceAction,
        "ComplianceActor": ComplianceActor,
        "ComplianceAuthorizer": ComplianceAuthorizer,
        "ComplianceScope": ComplianceScope,
        "Phase1ComplianceAuthorizer": Phase1ComplianceAuthorizer,
    }

    assert set(expected) <= set(service_module.__all__)
    for name, contract in expected.items():
        assert getattr(service_module, name) is contract


def test_country_policy_version_proposed_is_a_registered_metadata_event() -> None:
    assert issubclass(CountryPolicyVersionProposed, DomainEvent)
    event = CountryPolicyVersionProposed(
        tenant_id=TENANT,
        occurred_at=NOW,
        run_id=None,
        country_policy_version_id=VERSION_ID,
        country_key="synthetic market",
        content_hash="a" * 64,
        proposed_by=PROPOSER,
    )

    assert event.country_policy_version_id == VERSION_ID
    assert event.country_key == "synthetic market"
    assert event.content_hash == "a" * 64
    assert event.proposed_by == PROPOSER
    assert EVENT_REGISTRY["CountryPolicyVersionProposed"] is CountryPolicyVersionProposed


def test_country_policy_change_is_a_registered_mandatory_approval() -> None:
    assert ApprovalType.COUNTRY_POLICY_CHANGE.value == "country_policy_change"


def test_country_policy_ids_use_the_reserved_prefixes() -> None:
    assert str(VERSION_ID).startswith("cpp_")
    assert str(ACTIVATION_ID).startswith("cpa_")
