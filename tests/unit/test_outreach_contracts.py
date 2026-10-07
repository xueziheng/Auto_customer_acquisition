"""触达域公共 DTO、Protocol 与原子 outcome 契约。"""

from __future__ import annotations

import importlib
import inspect
from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime
from typing import get_type_hints

import pytest

from shared.errors import ValidationError
from shared.schemas.identifiers import (
    ApprovalId,
    CampaignId,
    ContactPointId,
    EmployeeId,
    EnrollmentId,
    IdempotencyKey,
    MessageAttemptId,
    MessageId,
    ProspectAccountId,
    SendingIdentityId,
    SuppressionId,
    TenantId,
    new_id,
)


def _schemas() -> object:
    return importlib.import_module("domains.outreach.schemas")


def _service() -> object:
    return importlib.import_module("domains.outreach.service")


def _repository() -> object:
    return importlib.import_module("domains.outreach.repository")


NOW = datetime(2026, 8, 11, 4, 0, tzinfo=UTC)
TENANT = TenantId(new_id("tn"))
CAMPAIGN = CampaignId(new_id("cmp"))
ENROLLMENT = EnrollmentId(new_id("enr"))
ACCOUNT = ProspectAccountId(new_id("acc"))
CONTACT = ContactPointId(new_id("cp"))
SENDER = SendingIdentityId(new_id("sid"))


_PUBLIC_SIGNATURES = {
    "create_campaign": ("self", "tenant_id", "request", "actor"),
    "submit_campaign": ("self", "tenant_id", "campaign_id", "actor"),
    "revise_campaign": ("self", "tenant_id", "campaign_id", "request", "actor"),
    "activate_campaign": ("self", "tenant_id", "campaign_id", "actor"),
    "pause_campaign": ("self", "tenant_id", "campaign_id", "reason", "actor"),
    "cancel_campaign": ("self", "tenant_id", "campaign_id", "actor"),
    "get_campaign": ("self", "tenant_id", "campaign_id", "actor"),
    "list_campaigns": ("self", "tenant_id", "scope", "limit", "actor"),
    "enroll": ("self", "tenant_id", "campaign_id", "request", "actor"),
    "prepare_message_attempt": ("self", "tenant_id", "enrollment_id", "actor"),
    "preflight_message_send": ("self", "tenant_id", "attempt_id", "actor"),
    "claim_message_send": ("self", "tenant_id", "attempt_id", "actor"),
    "record_sent": ("self", "tenant_id", "attempt_id", "provider_ref", "actor"),
    "record_send_failure": ("self", "tenant_id", "attempt_id", "category", "actor"),
    "stop_enrollment": ("self", "tenant_id", "enrollment_id", "reason", "actor"),
    "get_enrollment": ("self", "tenant_id", "enrollment_id", "actor"),
    "list_enrollments": ("self", "tenant_id", "scope", "limit", "actor"),
    "add_suppression": ("self", "tenant_id", "request", "actor"),
    "is_suppressed": ("self", "tenant_id", "target", "actor"),
    "list_suppressions": ("self", "tenant_id", "scope", "limit", "actor"),
    "bind_delivery_correlation": (
        "self",
        "tenant_id",
        "attempt_id",
        "binding",
        "actor",
    ),
    "resolve_delivery_feedback": ("self", "tenant_id", "lookup", "actor"),
    "apply_hard_bounce": (
        "self",
        "tenant_id",
        "target",
        "provider_event_id",
        "occurred_at",
        "actor",
    ),
}


def test_public_service_signatures_and_keyword_only_actor_are_exact() -> None:
    """旧骨架名字或可省略 actor 会形成未经授权的兼容旁路。"""
    service = _service()
    for name, expected in _PUBLIC_SIGNATURES.items():
        signature = inspect.signature(getattr(service.OutreachService, name))
        assert tuple(signature.parameters) == expected
        assert signature.parameters["actor"].kind is inspect.Parameter.KEYWORD_ONLY


def test_public_service_return_annotations_are_typed_views() -> None:
    """裸 ID/bool/None 返回会丢失版本、配额与幂等证据。"""
    service = _service()
    schemas = _schemas()
    expected = {
        "create_campaign": schemas.CampaignView,
        "submit_campaign": schemas.CampaignView,
        "revise_campaign": schemas.CampaignView,
        "activate_campaign": schemas.CampaignView,
        "pause_campaign": schemas.CampaignView,
        "cancel_campaign": schemas.CampaignView,
        "get_campaign": schemas.CampaignView,
        "enroll": schemas.EnrollmentView,
        "prepare_message_attempt": schemas.MessageAttemptView,
        "preflight_message_send": schemas.MessageSendPreflight,
        "claim_message_send": schemas.MessageAttemptView,
        "record_sent": schemas.MessageAttemptView,
        "record_send_failure": schemas.MessageAttemptView,
        "stop_enrollment": schemas.EnrollmentView,
        "get_enrollment": schemas.EnrollmentView,
        "add_suppression": schemas.SuppressionResult,
        "is_suppressed": schemas.SuppressionView | None,
        "bind_delivery_correlation": schemas.MessageAttemptView,
        "resolve_delivery_feedback": schemas.DeliveryFeedbackTarget | None,
        "apply_hard_bounce": schemas.SuppressionResult,
    }
    for name, return_type in expected.items():
        hints = get_type_hints(getattr(service.OutreachService, name))
        assert hints["return"] == return_type


def test_unsafe_skeleton_service_paths_are_removed() -> None:
    """verified bool、发送授权和自由 scope 旧接口不能继续可调用。"""
    service = _service()
    for name in (
        "submit_for_approval",
        "activate",
        "pause",
        "revise_boundary",
        "suppress",
        "unsuppress",
        "list_due_enrollments",
    ):
        assert not hasattr(service.OutreachService, name)
    assert "verified" not in inspect.signature(service.OutreachService.enroll).parameters


def test_provider_protocol_signatures_are_exact_async_contracts() -> None:
    """provider 改名或漏 account/version 会重新信任调用方伪造事实。"""
    service = _service()
    expected = {
        "ContactEligibilityProvider": (
            "get_contact_eligibility",
            ("self", "tenant_id", "contact_point_id", "account_id"),
        ),
        "SendingIdentityEligibilityProvider": (
            "get_sending_identity_eligibility",
            ("self", "tenant_id", "identity_id"),
        ),
        "CampaignApprovalProvider": (
            "get_campaign_approval",
            ("self", "tenant_id", "campaign_id", "version"),
        ),
        "ReplyStatusProvider": (
            "get_reply_status",
            ("self", "tenant_id", "contact_point_id", "account_id"),
        ),
    }
    for protocol_name, (method_name, parameters) in expected.items():
        method = getattr(getattr(service, protocol_name), method_name)
        assert inspect.iscoroutinefunction(method)
        assert tuple(inspect.signature(method).parameters) == parameters


def test_suppression_id_is_public_and_canonical() -> None:
    """抑制事实必须有独立 typed ID，不能复用 target 或幂等键。"""
    identifiers = importlib.import_module("shared.schemas.identifiers")
    value = identifiers.SuppressionId(new_id("sup"))
    assert value.startswith("sup_")
    assert len(value) == 30


def test_delivery_feedback_dtos_are_frozen_minimal_and_non_reflective() -> None:
    """删除校验、暴露 correlation repr 或加入地址字段都会被捕获。"""
    schemas = _schemas()
    digest = "a" * 64
    binding = schemas.DeliveryCorrelationBinding(
        deterministic_message_id=f"<route-a.{digest}@messages.tradeos.invalid>",
        idempotency_header=f"route-a.{digest}",
        route_id="route-a",
    )
    lookup = schemas.DeliveryCorrelationLookup(
        deterministic_message_id=binding.deterministic_message_id,
    )
    target = schemas.DeliveryFeedbackTarget(
        tenant_id=TENANT,
        attempt_id=MessageAttemptId(new_id("mat")),
        enrollment_id=ENROLLMENT,
        account_id=ACCOUNT,
        contact_point_id=CONTACT,
        sending_identity_id=SENDER,
    )

    assert set(binding.__dataclass_fields__) == {
        "deterministic_message_id",
        "idempotency_header",
        "route_id",
    }
    assert set(lookup.__dataclass_fields__) == {
        "deterministic_message_id",
        "idempotency_header",
    }
    assert set(target.__dataclass_fields__) == {
        "tenant_id",
        "attempt_id",
        "enrollment_id",
        "account_id",
        "contact_point_id",
        "sending_identity_id",
    }
    assert digest not in repr(binding)
    assert digest not in repr(lookup)
    with pytest.raises(FrozenInstanceError):
        target.account_id = ProspectAccountId(new_id("acc"))


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"deterministic_message_id": 1},
        {"idempotency_header": object()},
        {"deterministic_message_id": "<route-a.short@messages.tradeos.invalid>"},
        {"idempotency_header": f"route-a.{'A' * 64}"},
        {"deterministic_message_id": f"<token.{'a' * 64}@messages.tradeos.invalid>"},
        {"idempotency_header": f"route\n.{'a' * 64}"},
    ],
)
def test_delivery_correlation_lookup_rejects_missing_or_unsafe_values(
    values: dict[str, object],
) -> None:
    """缺键、自由文本、控制字符与 credential-like route 不能进入查询。"""
    schemas = _schemas()
    rendered = repr(values)
    with pytest.raises(ValidationError) as error:
        schemas.DeliveryCorrelationLookup(**values)
    assert rendered not in str(error.value)


def test_delivery_binding_requires_matching_route_and_digest_pair() -> None:
    """删除 pair 校验会允许两个 header 指向不同 Attempt。"""
    schemas = _schemas()
    with pytest.raises(ValidationError):
        schemas.DeliveryCorrelationBinding(
            deterministic_message_id=(
                f"<route-a.{'a' * 64}@messages.tradeos.invalid>"
            ),
            idempotency_header=f"route-a.{'b' * 64}",
            route_id="route-a",
        )
    with pytest.raises(ValidationError):
        schemas.DeliveryCorrelationBinding(
            deterministic_message_id=(
                f"<route-a.{'a' * 64}@messages.tradeos.invalid>"
            ),
            idempotency_header=f"route-a.{'a' * 64}",
            route_id="route-b",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tenant_id", 1),
        ("attempt_id", "enr_01K00000000000000000000000"),
        ("enrollment_id", "mat_01K00000000000000000000000"),
        ("account_id", "cp_01K00000000000000000000000"),
        ("contact_point_id", "acc_01K00000000000000000000000"),
        ("sending_identity_id", "mat_01K00000000000000000000000"),
    ],
)
def test_delivery_feedback_target_rejects_runtime_newtype_confusion(
    field: str, value: object
) -> None:
    """NewType 在运行时是 str，目标 DTO 必须自行验证命名空间。"""
    schemas = _schemas()
    values: dict[str, object] = {
        "tenant_id": TENANT,
        "attempt_id": MessageAttemptId(new_id("mat")),
        "enrollment_id": ENROLLMENT,
        "account_id": ACCOUNT,
        "contact_point_id": CONTACT,
        "sending_identity_id": SENDER,
    }
    values[field] = value
    with pytest.raises(ValidationError):
        schemas.DeliveryFeedbackTarget(**values)


def _contact_snapshot(**changes: object) -> object:
    schemas = _schemas()
    values: dict[str, object] = {
        "tenant_id": TENANT,
        "contact_point_id": CONTACT,
        "account_id": ACCOUNT,
        "verification": schemas.ContactVerificationStatus.VERIFIED,
        "verified_at": NOW,
        "legal_basis": schemas.ContactLegalBasis.LEGITIMATE_INTEREST,
        "legal_basis_ref": "basis_record_1",
        "contact_belongs_to_account": True,
        "country": "US",
        "entity_type": "importer",
        "qualified_categories": {"hardware"},
        "observed_at": NOW,
    }
    values.update(changes)
    return schemas.ContactEligibilitySnapshot(**values)


def test_contact_snapshot_is_frozen_and_defensively_copies_categories() -> None:
    """调用方不能在 provider 返回后扩充合格品类。"""
    categories = {"hardware"}
    snapshot = _contact_snapshot(qualified_categories=categories)
    categories.add("packaging")
    assert snapshot.qualified_categories == frozenset({"hardware"})
    with pytest.raises(FrozenInstanceError):
        snapshot.country = "DE"


@pytest.mark.parametrize(
    "changes",
    [
        {"tenant_id": 1},
        {"contact_point_id": "wrong_01K00000000000000000000000"},
        {"account_id": "cp_01K00000000000000000000000"},
        {"verified_at": datetime(2026, 8, 11, 4, 0)},  # noqa: DTZ001
        {"observed_at": "2026-08-11T04:00:00Z"},
        {"contact_belongs_to_account": 1},
        {"country": " us "},
        {"entity_type": ""},
        {"qualified_categories": ["hardware", ""]},
    ],
)
def test_contact_snapshot_rejects_malformed_runtime_values(
    changes: dict[str, object]
) -> None:
    """NewType 静态提示不能替代运行时 provider 边界校验。"""
    with pytest.raises(ValidationError):
        _contact_snapshot(**changes)


def test_provider_snapshots_reject_wrong_typed_id_namespaces() -> None:
    """运行时 NewType 仍是 str，错 namespace 不能穿过 provider 边界。"""
    schemas = _schemas()
    with pytest.raises(ValidationError):
        _contact_snapshot(tenant_id=TenantId(str(ACCOUNT)))
    with pytest.raises(ValidationError):
        schemas.SendingIdentityEligibilitySnapshot(
            tenant_id=TenantId(str(ACCOUNT)),
            identity_id=SENDER,
            role=schemas.OutreachSenderRole.COLD_OUTREACH,
            authentication_passed=True,
            sendable=True,
            remaining_slots=1,
            observed_at=NOW,
        )
    with pytest.raises(ValidationError):
        schemas.CampaignApprovalSnapshot(
            tenant_id=TENANT,
            campaign_id=CAMPAIGN,
            version=1,
            approval_id=ApprovalId(str(ACCOUNT)),
            state=schemas.CampaignApprovalState.PENDING,
            approved_by=None,
            approved_at=None,
        )


def test_contact_snapshot_verified_state_requires_time_and_legal_basis_ref() -> None:
    """verified 枚举若没有证据时间/依据仍不能进入序列。"""
    with pytest.raises(ValidationError):
        _contact_snapshot(verified_at=None)
    with pytest.raises(ValidationError):
        _contact_snapshot(legal_basis_ref="")


def test_approval_and_reply_snapshots_enforce_conditional_fields() -> None:
    """裸 approved/replied 状态不能缺少责任人或发生时间。"""
    schemas = _schemas()
    approval = schemas.CampaignApprovalSnapshot(
        tenant_id=TENANT,
        campaign_id=CAMPAIGN,
        version=1,
        approval_id=ApprovalId(new_id("apr")),
        state=schemas.CampaignApprovalState.APPROVED,
        approved_by=EmployeeId(new_id("emp")),
        approved_at=NOW,
    )
    assert approval.state is schemas.CampaignApprovalState.APPROVED
    with pytest.raises(ValidationError):
        schemas.CampaignApprovalSnapshot(
            tenant_id=TENANT,
            campaign_id=CAMPAIGN,
            version=1,
            approval_id=ApprovalId(new_id("apr")),
            state=schemas.CampaignApprovalState.APPROVED,
            approved_by=None,
            approved_at=None,
        )
    with pytest.raises(ValidationError):
        schemas.ReplyStatusSnapshot(
            tenant_id=TENANT,
            contact_point_id=CONTACT,
            account_id=ACCOUNT,
            state=schemas.ReplyState.REPLIED,
            replied_at=None,
            observed_at=NOW,
        )


def test_sender_snapshot_rejects_bool_slots_and_wrong_role_type() -> None:
    """remaining_slots=True 或自由 role 不能被当作可发送资格。"""
    schemas = _schemas()
    base = {
        "tenant_id": TENANT,
        "identity_id": SENDER,
        "role": schemas.OutreachSenderRole.COLD_OUTREACH,
        "authentication_passed": True,
        "sendable": True,
        "remaining_slots": 5,
        "observed_at": NOW,
    }
    schemas.SendingIdentityEligibilitySnapshot(**base)
    for changes in ({"remaining_slots": True}, {"role": "cold_outreach"}):
        with pytest.raises(ValidationError):
            schemas.SendingIdentityEligibilitySnapshot(**(base | changes))


def test_suppression_target_is_exact_typed_sum_and_request_copies_it() -> None:
    """自由 scope、零目标、双目标或错命名空间会绕过全局抑制。"""
    schemas = _schemas()
    target = schemas.SuppressionTarget(contact_point_id=CONTACT)
    assert target.scope.value == "contact"
    assert target.canonical_id == CONTACT
    with pytest.raises(ValidationError):
        schemas.SuppressionTarget()
    with pytest.raises(ValidationError):
        schemas.SuppressionTarget(contact_point_id=CONTACT, account_id=ACCOUNT)
    with pytest.raises(ValidationError):
        schemas.SuppressionTarget(contact_point_id=ContactPointId(str(ACCOUNT)))


@pytest.mark.parametrize(
    "value",
    ["", "Bearer_abc", "token_secret", "postgresql://db", "a\nb", "x" * 201],
)
def test_suppression_request_rejects_unsafe_key_or_source(value: str) -> None:
    """凭证、连接串和控制字符不能进入 append-only 事实。"""
    schemas = _schemas()
    models = importlib.import_module("domains.outreach.models")
    for field in ("source_ref", "idempotency_key"):
        kwargs = {
            "target": schemas.SuppressionTarget(account_id=ACCOUNT),
            "reason": models.SuppressionReason.MANUAL_BLOCK,
            "occurred_at": NOW,
            "source_ref": "manual_record_1",
            "idempotency_key": IdempotencyKey("suppression-key-1"),
        }
        kwargs[field] = value
        with pytest.raises(ValidationError):
            schemas.SuppressionRequest(**kwargs)


def test_campaign_request_copies_collections_and_rejects_unknown_handoff() -> None:
    """DTO 自身必须在进入 service 前封闭可变集合和 trigger 词表。"""
    schemas = _schemas()
    models = importlib.import_module("domains.outreach.models")
    markets = ["US"]
    request = schemas.CampaignCreateRequest(
        name="Hardware discovery",
        markets=markets,
        target_entity_types=["importer"],
        allowed_categories=["hardware"],
        sender_identity_ids=[SENDER],
        steps=[schemas.SequenceStepRequest(1, models.StepIntent.DISCOVERY, 0)],
        daily_new_contact_limit=5,
        daily_total_message_limit=10,
        handoff_triggers=["quote_requested"],
        stop_on_reply=True,
    )
    markets.append("DE")
    assert request.markets == ("US",)
    with pytest.raises(ValidationError):
        schemas.CampaignCreateRequest(
            name="Hardware discovery",
            markets=["US"],
            target_entity_types=["importer"],
            allowed_categories=["hardware"],
            sender_identity_ids=[SENDER],
            steps=[schemas.SequenceStepRequest(1, models.StepIntent.DISCOVERY, 0)],
            daily_new_contact_limit=5,
            daily_total_message_limit=10,
            handoff_triggers=["free_text"],
            stop_on_reply=True,
        )
    for changes in (
        {"sender_identity_ids": [object()]},
        {"steps": [object()]},
    ):
        base: dict[str, object] = {
            "name": "Hardware discovery",
            "markets": ["US"],
            "target_entity_types": ["importer"],
            "allowed_categories": ["hardware"],
            "sender_identity_ids": [SENDER],
            "steps": [
                schemas.SequenceStepRequest(1, models.StepIntent.DISCOVERY, 0)
            ],
            "daily_new_contact_limit": 5,
            "daily_total_message_limit": 10,
            "handoff_triggers": [],
            "stop_on_reply": True,
        }
        with pytest.raises(ValidationError):
            schemas.CampaignCreateRequest(**(base | changes))


def test_atomic_repository_outcomes_reject_impossible_combinations() -> None:
    """conflict/cap 携带 winner 或 existing 缺 winner 会让 service 错判幂等。"""
    repository = _repository()
    models = importlib.import_module("domains.outreach.models")
    target = _schemas().SuppressionTarget(account_id=ACCOUNT)
    enrollment = models.Enrollment(
        tenant_id=TENANT,
        enrollment_id=ENROLLMENT,
        campaign_id=CAMPAIGN,
        campaign_version=1,
        account_id=ACCOUNT,
        contact_point_id=CONTACT,
        sending_identity_id=SENDER,
        state=models.EnrollmentState.ENROLLED,
        current_step=0,
        next_send_at=NOW,
        enrolled_at=NOW,
        stopped_at=None,
        stop_reason=None,
        idempotency_key=IdempotencyKey("enrollment-key-1"),
    )
    suppression = models.SuppressionEntry(
        tenant_id=TENANT,
        suppression_id=SuppressionId(new_id("sup")),
        target=target,
        reason=models.SuppressionReason.MANUAL_BLOCK,
        occurred_at=NOW,
        source_ref="manual_record_1",
        idempotency_key=IdempotencyKey("suppression-key-1"),
        created_at=NOW,
    )
    quota = models.DailyQuotaUsage(TENANT, CAMPAIGN, date(2026, 8, 11), 1, 0)
    attempt = models.MessageAttempt(
        tenant_id=TENANT,
        attempt_id=MessageAttemptId(new_id("mat")),
        message_id=MessageId(new_id("msg")),
        campaign_id=CAMPAIGN,
        enrollment_id=ENROLLMENT,
        campaign_version=1,
        step_number=1,
        sending_identity_id=SENDER,
        idempotency_key=IdempotencyKey("attempt-key-1"),
        state=models.MessageAttemptState.RESERVED,
        provider_ref=None,
        failure_category=None,
        created_at=NOW,
        updated_at=NOW,
    )
    valid = [
        repository.EnrollmentInsertResult(repository.EnrollmentInsertStatus.CREATED, enrollment),
        repository.SuppressionAppendResult(repository.AppendStatus.EXISTING, suppression),
        repository.QuotaReservationResult(repository.QuotaReservationStatus.CAP_REACHED, None),
        repository.MessageAttemptCreateResult(repository.AppendStatus.CONFLICT, None),
        repository.QuotaReservationResult(repository.QuotaReservationStatus.RESERVED, quota),
        repository.MessageAttemptCreateResult(repository.AppendStatus.CREATED, attempt),
        repository.DeliveryCorrelationBindResult(
            repository.DeliveryCorrelationBindStatus.BOUND, attempt
        ),
    ]
    assert len(valid) == 7
    for constructor, status in (
        (repository.EnrollmentInsertResult, repository.EnrollmentInsertStatus.CREATED),
        (repository.SuppressionAppendResult, repository.AppendStatus.EXISTING),
        (repository.QuotaReservationResult, repository.QuotaReservationStatus.RESERVED),
        (repository.MessageAttemptCreateResult, repository.AppendStatus.CREATED),
        (
            repository.DeliveryCorrelationBindResult,
            repository.DeliveryCorrelationBindStatus.BOUND,
        ),
    ):
        with pytest.raises(ValidationError):
            constructor(status, object())
    with pytest.raises(ValidationError):
        repository.EnrollmentInsertResult(repository.EnrollmentInsertStatus.EXISTING, None)
    with pytest.raises(ValidationError):
        repository.SuppressionAppendResult(repository.AppendStatus.CONFLICT, suppression)
    with pytest.raises(ValidationError):
        repository.QuotaReservationResult(repository.QuotaReservationStatus.RESERVED, None)
    with pytest.raises(ValidationError):
        repository.MessageAttemptCreateResult(repository.AppendStatus.CREATED, None)
    with pytest.raises(ValidationError):
        repository.DeliveryCorrelationBindResult(
            repository.DeliveryCorrelationBindStatus.CONFLICT, attempt
        )


def test_attempt_repository_exposes_atomic_binding_and_tenant_scoped_lookups() -> None:
    """删除 typed bind 或 tenant 参数会迫使 service 解析数据库异常或跨租户查找。"""
    repository = _repository()
    expected = {
        "bind_delivery_correlation": ("self", "attempt"),
        "find_by_deterministic_message_id": (
            "self",
            "tenant_id",
            "deterministic_message_id",
        ),
        "find_by_idempotency_header": (
            "self",
            "tenant_id",
            "idempotency_header",
        ),
    }
    for method_name, parameters in expected.items():
        method = getattr(repository.MessageAttemptRepository, method_name)
        assert inspect.iscoroutinefunction(method)
        assert tuple(inspect.signature(method).parameters) == parameters


def test_uow_protocol_exposes_only_the_required_transactional_components() -> None:
    """少任一 repository 会迫使业务拆事务，多出 commit API 会形成旁路。"""
    repository = _repository()
    annotations = get_type_hints(repository.OutreachUnitOfWork)
    assert set(annotations) == {
        "campaigns",
        "enrollments",
        "suppressions",
        "quotas",
        "attempts",
        "actions",
        "bus",
    }
