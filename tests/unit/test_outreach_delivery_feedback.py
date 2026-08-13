"""发送关联与投递反馈的权限、幂等和业务效果。"""

from __future__ import annotations

import copy
import importlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from domains.outreach.errors import (
    IdempotencyConflictError,
    MessageAttemptConflictError,
)
from domains.outreach.permissions import (
    Actor,
    DefaultDenyAuthorizer,
    OutreachScope,
    Phase1OutreachAuthorizer,
    ScopeLevel,
)
from domains.outreach.schemas import SuppressionTarget
from shared.errors import (
    PermissionDenied,
    TenantIsolationViolation,
    ValidationError,
)
from shared.schemas.identifiers import (
    ContactPointId,
    EnrollmentId,
    IdempotencyKey,
    MessageAttemptId,
    MessageId,
    SendingIdentityId,
    TenantId,
    new_id,
)
from tests.outreach_fakes import (
    FakeAttemptRepository,
    FakeAuthorizer,
    FakeStore,
    FakeUow,
    Trace,
)

NOW = datetime(2026, 8, 13, 6, 0, tzinfo=UTC)


def _models() -> object:
    return importlib.import_module("domains.outreach.models")


def _repository() -> object:
    return importlib.import_module("domains.outreach.repository")


def _schemas() -> object:
    return importlib.import_module("domains.outreach.schemas")


def _service_type() -> type[object]:
    return importlib.import_module(
        "domains.outreach.service_impl"
    ).OutreachServiceImpl


def _binding(route_id: str = "route-a", digest: str = "a" * 64) -> object:
    return _schemas().DeliveryCorrelationBinding(
        deterministic_message_id=(
            f"<{route_id}.{digest}@messages.tradeos.invalid>"
        ),
        idempotency_header=f"{route_id}.{digest}",
        route_id=route_id,
    )


class FeedbackAttemptRepository(FakeAttemptRepository):
    """仅模拟 Task 1 repository contract；不复制领域判断。"""

    async def bind_delivery_correlation(self, attempt):
        repository = _repository()
        current = self.store.attempts.get(attempt.attempt_id)
        if current is None or current.tenant_id != attempt.tenant_id:
            return repository.DeliveryCorrelationBindResult(
                repository.DeliveryCorrelationBindStatus.CONFLICT,
                None,
            )
        for candidate in self.store.attempts.values():
            if candidate.attempt_id == attempt.attempt_id:
                continue
            if (
                candidate.tenant_id == attempt.tenant_id
                and (
                    candidate.deterministic_message_id
                    == attempt.deterministic_message_id
                    or candidate.idempotency_header == attempt.idempotency_header
                )
            ):
                return repository.DeliveryCorrelationBindResult(
                    repository.DeliveryCorrelationBindStatus.CONFLICT,
                    None,
                )
        existing = (
            current.deterministic_message_id,
            current.idempotency_header,
        )
        desired = (
            attempt.deterministic_message_id,
            attempt.idempotency_header,
        )
        if existing == desired:
            status = repository.DeliveryCorrelationBindStatus.EXISTING
        elif existing == (None, None):
            current.bind_delivery_correlation(
                _schemas().DeliveryCorrelationBinding(
                    deterministic_message_id=attempt.deterministic_message_id,
                    idempotency_header=attempt.idempotency_header,
                    route_id=attempt.idempotency_header.split(".", 1)[0],
                )
            )
            self.store.attempts[current.attempt_id] = copy.deepcopy(current)
            status = repository.DeliveryCorrelationBindStatus.BOUND
        else:
            return repository.DeliveryCorrelationBindResult(
                repository.DeliveryCorrelationBindStatus.CONFLICT,
                None,
            )
        return repository.DeliveryCorrelationBindResult(
            status,
            copy.deepcopy(current),
        )

    async def find_by_deterministic_message_id(
        self, tenant_id, deterministic_message_id
    ):
        for attempt in self.store.attempts.values():
            if (
                tenant_id == self.tenant_id
                and attempt.deterministic_message_id == deterministic_message_id
            ):
                return copy.deepcopy(attempt)
        return None

    async def find_by_idempotency_header(self, tenant_id, idempotency_header):
        for attempt in self.store.attempts.values():
            if (
                tenant_id == self.tenant_id
                and attempt.idempotency_header == idempotency_header
            ):
                return copy.deepcopy(attempt)
        return None


class FeedbackUow(FakeUow):
    def __init__(self, store: FakeStore, tenant_id: TenantId, trace: Trace) -> None:
        super().__init__(store, tenant_id, trace)
        self.attempts = FeedbackAttemptRepository(store, tenant_id, trace)


class FeedbackUowFactory:
    def __init__(self, store: FakeStore, trace: Trace) -> None:
        self.store = store
        self.trace = trace

    def __call__(self, tenant_id: TenantId) -> FeedbackUow:
        return FeedbackUow(self.store, tenant_id, self.trace)


def _feedback_harness() -> object:
    helpers = importlib.import_module(
        "tests.unit.test_outreach_enrollment_service"
    )
    harness = helpers._build()
    harness.service = _service_type()(
        FeedbackUowFactory(harness.store, harness.trace),
        harness.contacts,
        harness.senders,
        harness.approvals,
        harness.replies,
        FakeAuthorizer(harness.trace),
        harness.audit,
        now=lambda: NOW,
    )
    return harness


async def _sent_attempt() -> tuple[object, object, object]:
    helpers = importlib.import_module(
        "tests.unit.test_outreach_enrollment_service"
    )
    harness = _feedback_harness()
    enrollment = await harness.service.enroll(
        harness.tenant,
        harness.campaign_id,
        helpers._request(harness),
        actor=harness.boss,
    )
    send_actor = helpers._system(harness, enrollment.enrollment_id)
    prepared = await harness.service.prepare_message_attempt(
        harness.tenant,
        enrollment.enrollment_id,
        actor=send_actor,
    )
    await harness.service.claim_message_send(
        harness.tenant,
        prepared.attempt_id,
        actor=send_actor,
    )
    attempt = await harness.service.record_sent(
        harness.tenant,
        prepared.attempt_id,
        "gmail_ref_safe_1",
        actor=send_actor,
    )
    harness.trace.calls.clear()
    harness.audit.records.clear()
    return harness, enrollment, attempt


def _attempt_actor(attempt_id: MessageAttemptId) -> Actor:
    return Actor(
        "system:feedback-bind",
        OutreachScope(
            level=ScopeLevel.SYSTEM,
            allowed_attempt_ids=frozenset({attempt_id}),
        ),
        "system",
    )


def _identity_actor(identity_id: SendingIdentityId) -> Actor:
    return Actor(
        "system:feedback-process",
        OutreachScope(
            level=ScopeLevel.SYSTEM,
            allowed_sending_identity_ids=frozenset({identity_id}),
        ),
        "system",
    )


def _allow_count(harness: object) -> int:
    return sum(
        record["rule"].startswith("allow:")
        for record in harness.audit.records
    )


def test_feedback_permission_vocabulary_and_exact_system_scope() -> None:
    """删除任一 exact-resource gate 会让 worker 获得不受限写权限。"""
    permissions = importlib.import_module("domains.outreach.permissions")
    assert {
        action.value
        for action in permissions.OutreachAction
        if action.value
        in {
            "message:delivery_bind",
            "delivery_feedback:resolve",
            "hard_bounce:apply",
        }
    } == {
        "message:delivery_bind",
        "delivery_feedback:resolve",
        "hard_bounce:apply",
    }
    tenant = TenantId(new_id("tn"))
    attempt = MessageAttemptId(new_id("mat"))
    identity = SendingIdentityId(new_id("sid"))
    authorizer = Phase1OutreachAuthorizer(tenant)
    attempt_actor = _attempt_actor(attempt)
    identity_actor = _identity_actor(identity)

    authorizer.preauthorize(
        attempt_actor,
        permissions.OutreachAction.MESSAGE_DELIVERY_BIND,
        attempt_actor.scope,
        tenant,
    )
    authorizer.require(
        attempt_actor,
        permissions.OutreachAction.MESSAGE_DELIVERY_BIND,
        attempt_actor.scope,
        tenant,
        attempt_id=attempt,
    )
    for action in (
        permissions.OutreachAction.DELIVERY_FEEDBACK_RESOLVE,
        permissions.OutreachAction.HARD_BOUNCE_APPLY,
    ):
        authorizer.preauthorize(identity_actor, action, identity_actor.scope, tenant)
        authorizer.require(
            identity_actor,
            action,
            identity_actor.scope,
            tenant,
            sending_identity_id=identity,
        )


@pytest.mark.parametrize("role", ["boss", "manager", "sales"])
def test_human_roles_and_default_authorizer_cannot_process_feedback(
    role: str,
) -> None:
    """把新 action 加入人工矩阵或默认放行会形成反馈写旁路。"""
    permissions = importlib.import_module("domains.outreach.permissions")
    tenant = TenantId(new_id("tn"))
    if role == "boss":
        scope = OutreachScope(level=ScopeLevel.TENANT)
    elif role == "manager":
        scope = OutreachScope(
            level=ScopeLevel.MANAGER,
            allowed_campaign_ids=frozenset({new_id("cmp")}),
        )
    else:
        scope = OutreachScope(
            level=ScopeLevel.SELF,
            allowed_enrollment_ids=frozenset({new_id("enr")}),
        )
    actor = Actor(f"{role}:feedback", scope, role)
    for authorizer in (
        Phase1OutreachAuthorizer(tenant),
        DefaultDenyAuthorizer(),
    ):
        for action in (
            permissions.OutreachAction.MESSAGE_DELIVERY_BIND,
            permissions.OutreachAction.DELIVERY_FEEDBACK_RESOLVE,
            permissions.OutreachAction.HARD_BOUNCE_APPLY,
        ):
            with pytest.raises(PermissionDenied):
                authorizer.preauthorize(actor, action, scope, tenant)


def test_feedback_system_scope_rejects_empty_multi_mismatched_and_mutable_expansion() -> None:
    """SYSTEM scope 必须冻结一个 Attempt 或一个 SendingIdentity。"""
    permissions = importlib.import_module("domains.outreach.permissions")
    tenant = TenantId(new_id("tn"))
    first = MessageAttemptId(new_id("mat"))
    second = MessageAttemptId(new_id("mat"))
    with pytest.raises(ValidationError):
        OutreachScope(level=ScopeLevel.SYSTEM, allowed_attempt_ids=frozenset())
    with pytest.raises(ValidationError):
        OutreachScope(
            level=ScopeLevel.SYSTEM,
            allowed_attempt_ids=frozenset({first, second}),
        )
    with pytest.raises(ValidationError):
        OutreachScope(
            level=ScopeLevel.SYSTEM,
            allowed_attempt_ids=frozenset({first}),
            allowed_sending_identity_ids=frozenset({new_id("sid")}),
        )

    mutable = {first}
    actor = Actor(
        "system:feedback-bind",
        OutreachScope(level=ScopeLevel.SYSTEM, allowed_attempt_ids=mutable),
        "system",
    )
    mutable.clear()
    mutable.add(second)
    assert actor.scope.allowed_attempt_ids == frozenset({first})
    authorizer = Phase1OutreachAuthorizer(tenant)
    with pytest.raises(PermissionDenied):
        authorizer.require(
            actor,
            permissions.OutreachAction.MESSAGE_DELIVERY_BIND,
            actor.scope,
            tenant,
            attempt_id=second,
        )
    with pytest.raises(PermissionDenied):
        authorizer.preauthorize(
            actor,
            permissions.OutreachAction.MESSAGE_DELIVERY_BIND,
            actor.scope,
            TenantId(new_id("tn")),
        )


@pytest.mark.asyncio
async def test_binding_is_idempotent_and_conflicting_rebind_rolls_back() -> None:
    """删除 model/repository 冲突分支会静默改写已发邮件的关联键。"""
    harness, _enrollment, attempt = await _sent_attempt()
    first = _binding()
    initial_actions = len(harness.store.actions)
    view = await harness.service.bind_delivery_correlation(
        harness.tenant,
        attempt.attempt_id,
        first,
        actor=harness.boss,
    )
    retry = await harness.service.bind_delivery_correlation(
        harness.tenant,
        attempt.attempt_id,
        first,
        actor=harness.boss,
    )
    assert view.deterministic_message_id == first.deterministic_message_id
    assert retry.idempotency_header == first.idempotency_header
    assert len(harness.store.actions) == initial_actions + 1
    assert _allow_count(harness) == 2
    before = copy.deepcopy(harness.store)
    allow_before = _allow_count(harness)
    with pytest.raises(MessageAttemptConflictError):
        await harness.service.bind_delivery_correlation(
            harness.tenant,
            attempt.attempt_id,
            _binding("route-b", "b" * 64),
            actor=harness.boss,
        )
    assert harness.store == before
    assert _allow_count(harness) == allow_before


@pytest.mark.asyncio
async def test_resolve_uses_either_key_requires_same_target_and_missing_has_no_allow() -> None:
    """模糊 fallback、半匹配或冲突 header 不能产生业务目标。"""
    harness, enrollment, attempt = await _sent_attempt()
    binding = _binding()
    await harness.service.bind_delivery_correlation(
        harness.tenant, attempt.attempt_id, binding, actor=harness.boss
    )
    harness.audit.records.clear()
    schemas = _schemas()
    message_only = await harness.service.resolve_delivery_feedback(
        harness.tenant,
        schemas.DeliveryCorrelationLookup(
            deterministic_message_id=binding.deterministic_message_id
        ),
        actor=harness.boss,
    )
    header_only = await harness.service.resolve_delivery_feedback(
        harness.tenant,
        schemas.DeliveryCorrelationLookup(
            idempotency_header=binding.idempotency_header
        ),
        actor=harness.boss,
    )
    both = await harness.service.resolve_delivery_feedback(
        harness.tenant,
        schemas.DeliveryCorrelationLookup(
            deterministic_message_id=binding.deterministic_message_id,
            idempotency_header=binding.idempotency_header,
        ),
        actor=harness.boss,
    )
    assert message_only == header_only == both
    assert both.enrollment_id == enrollment.enrollment_id
    assert both.sending_identity_id == attempt.sending_identity_id
    assert _allow_count(harness) == 3

    allow_before = _allow_count(harness)
    missing = await harness.service.resolve_delivery_feedback(
        harness.tenant,
        schemas.DeliveryCorrelationLookup(
            deterministic_message_id=(
                f"<route-a.{'f' * 64}@messages.tradeos.invalid>"
            )
        ),
        actor=harness.boss,
    )
    assert missing is None
    assert _allow_count(harness) == allow_before

    second_enrollment = copy.deepcopy(harness.store.enrollments[enrollment.enrollment_id])
    second_enrollment.enrollment_id = EnrollmentId(new_id("enr"))
    second_enrollment.idempotency_key = IdempotencyKey("feedback-second-enrollment")
    harness.store.enrollments[second_enrollment.enrollment_id] = second_enrollment
    second_attempt = copy.deepcopy(harness.store.attempts[attempt.attempt_id])
    second_attempt.attempt_id = MessageAttemptId(new_id("mat"))
    second_attempt.message_id = MessageId(new_id("msg"))
    second_attempt.enrollment_id = second_enrollment.enrollment_id
    second_attempt.idempotency_key = IdempotencyKey("feedback-second-attempt")
    second_attempt.deterministic_message_id = None
    second_attempt.idempotency_header = None
    harness.store.attempts[second_attempt.attempt_id] = second_attempt
    second_binding = _binding("route-a", "b" * 64)
    await harness.service.bind_delivery_correlation(
        harness.tenant,
        second_attempt.attempt_id,
        second_binding,
        actor=harness.boss,
    )
    allow_before = _allow_count(harness)
    with pytest.raises(MessageAttemptConflictError):
        await harness.service.resolve_delivery_feedback(
            harness.tenant,
            schemas.DeliveryCorrelationLookup(
                deterministic_message_id=binding.deterministic_message_id,
                idempotency_header=second_binding.idempotency_header,
            ),
            actor=harness.boss,
        )
    assert _allow_count(harness) == allow_before


@pytest.mark.asyncio
async def test_hard_bounce_suppresses_contact_stops_matching_enrollments_once() -> None:
    """删除任一 effect、canonical stop 或 duplicate guard 都会改变可观察结果。"""
    harness, enrollment, attempt = await _sent_attempt()
    binding = _binding()
    await harness.service.bind_delivery_correlation(
        harness.tenant, attempt.attempt_id, binding, actor=harness.boss
    )
    target = await harness.service.resolve_delivery_feedback(
        harness.tenant,
        _schemas().DeliveryCorrelationLookup(
            idempotency_header=binding.idempotency_header
        ),
        actor=harness.boss,
    )
    assert target is not None
    second = copy.deepcopy(harness.store.enrollments[enrollment.enrollment_id])
    second.enrollment_id = EnrollmentId(new_id("enr"))
    second.idempotency_key = IdempotencyKey("feedback-matching-enrollment")
    harness.store.enrollments[second.enrollment_id] = second
    harness.audit.records.clear()
    actions_before = len(harness.store.actions)
    events_before = len(harness.store.events)

    result = await harness.service.apply_hard_bounce(
        harness.tenant,
        target,
        "c" * 64,
        NOW,
        actor=harness.boss,
    )
    models = _models()
    assert result.created is True
    assert result.stopped_count == 2
    assert result.suppression.target == SuppressionTarget(
        contact_point_id=harness.contact
    )
    assert result.suppression.reason is models.SuppressionReason.HARD_BOUNCE
    assert {
        current.state
        for current in harness.store.enrollments.values()
        if current.contact_point_id == harness.contact
    } == {models.EnrollmentState.STOPPED_BOUNCED}
    assert len(harness.store.actions) == actions_before + 3
    assert len(harness.store.events) == events_before + 1
    assert _allow_count(harness) == 1
    lock_call = next(
        call
        for call in harness.trace.calls
        if call[0] == "enrollment_lock_matching"
    )
    assert lock_call[1] == tuple(sorted(lock_call[1]))
    assert harness.trace.calls[-2][0] == "uow_exit"
    assert harness.trace.calls[-1] == (
        "audit",
        "hard_bounce:apply",
        "allow:hard_bounce:apply",
    )

    state_after = copy.deepcopy(harness.store)
    audit_after = len(harness.audit.records)
    duplicate = await harness.service.apply_hard_bounce(
        harness.tenant,
        target,
        "c" * 64,
        NOW,
        actor=harness.boss,
    )
    assert duplicate.created is False
    assert duplicate.stopped_count == 0
    assert harness.store == state_after
    assert len(harness.audit.records) == audit_after

    with pytest.raises(IdempotencyConflictError):
        await harness.service.apply_hard_bounce(
            harness.tenant,
            target,
            "c" * 64,
            NOW - timedelta(seconds=1),
            actor=harness.boss,
        )
    assert harness.store == state_after
    assert len(harness.audit.records) == audit_after


@pytest.mark.parametrize(
    "provider_event_id",
    ["", "A" * 64, "a" * 63, "token_" + "a" * 58, "a\nb"],
)
@pytest.mark.asyncio
async def test_hard_bounce_rejects_unsafe_provider_event_without_reflection(
    provider_event_id: str,
) -> None:
    """自由 provider 字符串不能进入 action、suppression、audit 或错误。"""
    harness, enrollment, attempt = await _sent_attempt()
    target = _schemas().DeliveryFeedbackTarget(
        harness.tenant,
        attempt.attempt_id,
        enrollment.enrollment_id,
        harness.account,
        harness.contact,
        attempt.sending_identity_id,
    )
    before = copy.deepcopy(harness.store)
    with pytest.raises(ValidationError) as error:
        await harness.service.apply_hard_bounce(
            harness.tenant,
            target,
            provider_event_id,
            NOW,
            actor=harness.boss,
        )
    assert provider_event_id not in str(error.value) or not provider_event_id
    assert harness.store == before
    assert _allow_count(harness) == 0


@pytest.mark.parametrize(
    "occurred_at",
    [NOW.replace(tzinfo=None), NOW + timedelta(minutes=5, microseconds=1)],
)
@pytest.mark.asyncio
async def test_hard_bounce_rejects_non_utc_or_too_future_time(
    occurred_at: datetime,
) -> None:
    """删除 UTC/future 边界会把不可信 provider 时间写成事实。"""
    harness, enrollment, attempt = await _sent_attempt()
    target = _schemas().DeliveryFeedbackTarget(
        harness.tenant,
        attempt.attempt_id,
        enrollment.enrollment_id,
        harness.account,
        harness.contact,
        attempt.sending_identity_id,
    )
    before = copy.deepcopy(harness.store)
    with pytest.raises(ValidationError):
        await harness.service.apply_hard_bounce(
            harness.tenant,
            target,
            "d" * 64,
            occurred_at,
            actor=harness.boss,
        )
    assert harness.store == before
    assert _allow_count(harness) == 0


@pytest.mark.asyncio
async def test_feedback_resource_mismatch_and_wrong_tenant_fail_closed() -> None:
    """信任 caller target 或恶意 repository row 会产生跨资源/跨租户写。"""
    harness, enrollment, attempt = await _sent_attempt()
    binding = _binding()
    await harness.service.bind_delivery_correlation(
        harness.tenant, attempt.attempt_id, binding, actor=harness.boss
    )
    valid = _schemas().DeliveryFeedbackTarget(
        harness.tenant,
        attempt.attempt_id,
        enrollment.enrollment_id,
        harness.account,
        harness.contact,
        attempt.sending_identity_id,
    )
    corrupted = replace(valid, contact_point_id=ContactPointId(new_id("cp")))
    before = copy.deepcopy(harness.store)
    allow_before = _allow_count(harness)
    with pytest.raises(ValidationError):
        await harness.service.apply_hard_bounce(
            harness.tenant,
            corrupted,
            "e" * 64,
            NOW,
            actor=harness.boss,
        )
    assert harness.store == before
    assert _allow_count(harness) == allow_before

    harness.store.attempts[attempt.attempt_id].tenant_id = TenantId(new_id("tn"))
    harness.audit.records.clear()
    with pytest.raises(TenantIsolationViolation):
        await harness.service.resolve_delivery_feedback(
            harness.tenant,
            _schemas().DeliveryCorrelationLookup(
                deterministic_message_id=binding.deterministic_message_id
            ),
            actor=harness.boss,
        )
    assert [record["rule"] for record in harness.audit.records] == [
        "deny:authorization"
    ]


@pytest.mark.asyncio
async def test_full_require_mismatch_writes_one_deny_and_zero_allow() -> None:
    """只做 preauthorization 会让 identity-scoped worker 处理别的身份。"""
    harness, _enrollment, attempt = await _sent_attempt()
    binding = _binding()
    harness.service._authorizer = Phase1OutreachAuthorizer(harness.tenant)
    await harness.service.bind_delivery_correlation(
        harness.tenant,
        attempt.attempt_id,
        binding,
        actor=_attempt_actor(attempt.attempt_id),
    )
    harness.audit.records.clear()
    wrong_actor = _identity_actor(SendingIdentityId(new_id("sid")))
    with pytest.raises(PermissionDenied):
        await harness.service.resolve_delivery_feedback(
            harness.tenant,
            _schemas().DeliveryCorrelationLookup(
                idempotency_header=binding.idempotency_header
            ),
            actor=wrong_actor,
        )
    assert [record["rule"] for record in harness.audit.records] == [
        "deny:authorization"
    ]


@pytest.mark.asyncio
async def test_hard_bounce_commit_failure_rolls_back_and_writes_zero_allow() -> None:
    """把 allow 移入 UoW 或漏掉原子回滚会在提交失败时留下假成功。"""
    class CommitFailure(RuntimeError):
        pass

    sentinel = CommitFailure("commit failure marker")
    harness, _enrollment, attempt = await _sent_attempt()
    binding = _binding()
    await harness.service.bind_delivery_correlation(
        harness.tenant, attempt.attempt_id, binding, actor=harness.boss
    )
    target = await harness.service.resolve_delivery_feedback(
        harness.tenant,
        _schemas().DeliveryCorrelationLookup(
            deterministic_message_id=binding.deterministic_message_id
        ),
        actor=harness.boss,
    )
    assert target is not None
    harness.audit.records.clear()
    before = copy.deepcopy(harness.store)

    class CommitFailureUow(FeedbackUow):
        async def __aexit__(self, exc_type, exc, tb):
            if exc_type is None:
                await super().__aexit__(CommitFailure, sentinel, tb)
                raise sentinel
            return await super().__aexit__(exc_type, exc, tb)

    class CommitFailureFactory:
        def __call__(self, tenant_id):
            return CommitFailureUow(harness.store, tenant_id, harness.trace)

    harness.service._uow_factory = CommitFailureFactory()
    with pytest.raises(CommitFailure) as captured:
        await harness.service.apply_hard_bounce(
            harness.tenant,
            target,
            "f" * 64,
            NOW,
            actor=harness.boss,
        )
    assert captured.value is sentinel
    assert harness.store == before
    assert _allow_count(harness) == 0
