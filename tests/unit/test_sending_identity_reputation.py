"""发件身份信誉事件、阈值、域级熔断与自动恢复契约。"""

from __future__ import annotations

import importlib
from datetime import timedelta
from decimal import Decimal

import pytest

from domains.sending_identity.errors import (
    AuthenticationNotVerifiedError,
    InvalidDeliveryEventError,
)
from domains.sending_identity.schemas import DeliveryEventRecord
from shared.errors import InvalidStateTransition
from shared.events.catalog import (
    ReputationThresholdBreached,
    SendingIdentitySuspended,
    SendingIdentityThrottled,
)
from shared.schemas.identifiers import IdempotencyKey, SendingIdentityId
from tests.unit.test_sending_identity_service import (
    _NOW,
    _TENANT,
    _auth,
    _build,
    _seed,
    _seed_sendable,
    _system,
)

_models = importlib.import_module("domains.sending_identity.models")
_permissions = importlib.import_module("domains.sending_identity.permissions")
DeliveryEventType = _models.DeliveryEventType
DomainRole = _models.DomainRole
IdentityState = _models.IdentityState
ReputationMetric = _models.ReputationMetric
ReputationSeverity = _models.ReputationSeverity
ReputationThresholds = _models.ReputationThresholds
ReputationWindow = _models.ReputationWindow
SendingIdentity = _models.SendingIdentity
SuspensionCategory = _models.SuspensionCategory
WarmupPlan = _models.WarmupPlan
SendingIdentityAction = _permissions.SendingIdentityAction


def _window(
    *,
    sent: int = 0,
    delivered: int = 0,
    hard_bounced: int = 0,
    complaints: int = 0,
    spam_trap_hits: int = 0,
    blocklist_hits: int = 0,
) -> ReputationWindow:
    return ReputationWindow(
        7,
        _NOW,
        sent,
        delivered,
        hard_bounced,
        0,
        complaints,
        0,
        spam_trap_hits,
        blocklist_hits,
    )


def _event(
    identity_id: SendingIdentityId,
    *,
    key: str,
    occurred_at=_NOW,
    event_type: DeliveryEventType = DeliveryEventType.DELIVERED,
) -> DeliveryEventRecord:
    return DeliveryEventRecord(
        tenant_id=_TENANT,
        identity_id=identity_id,
        event_type=event_type,
        occurred_at=occurred_at,
        dedup_key=IdempotencyKey(key),
        source_ref=f"provider-event-{key}",
    )


@pytest.mark.asyncio
async def test_delivery_event_time_boundaries_and_duplicate_are_exactly_idempotent() -> None:
    """闭区间边界可记录；重复 dedup 只多一条成功审计，不重复业务写。"""
    service, factory, audit, _ = _build()
    identity = _seed_sendable(factory)
    actor = _system(identity.identity_id)
    lower = _event(identity.identity_id, key="provider:lower", occurred_at=identity.created_at)
    upper = _event(
        identity.identity_id,
        key="provider:upper",
        occurred_at=_NOW + timedelta(minutes=5),
    )
    assert await service.record_delivery_event(
        _TENANT, identity.identity_id, lower, actor=actor
    )
    assert await service.record_delivery_event(
        _TENANT, identity.identity_id, upper, actor=actor
    )
    business_counts = (
        len(factory.delivery_events),
        len(factory.actions.records),
        len(factory.bus.events),
    )
    assert not await service.record_delivery_event(
        _TENANT, identity.identity_id, lower, actor=actor
    )
    assert (
        len(factory.delivery_events),
        len(factory.actions.records),
        len(factory.bus.events),
    ) == business_counts
    assert business_counts == (2, 0, 0)
    assert len(audit.records) == 3

    for index, invalid_time in enumerate(
        (
            identity.created_at - timedelta(microseconds=1),
            _NOW + timedelta(minutes=5, microseconds=1),
        )
    ):
        with pytest.raises(InvalidDeliveryEventError, match="^投递事件无效$"):
            await service.record_delivery_event(
                _TENANT,
                identity.identity_id,
                _event(
                    identity.identity_id,
                    key=f"provider:invalid-{index}",
                    occurred_at=invalid_time,
                ),
                actor=actor,
            )
    assert (
        len(factory.delivery_events),
        len(factory.actions.records),
        len(factory.bus.events),
        len(audit.records),
    ) == (*business_counts, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("window", "expected_state", "expected_metric"),
    [
        (_window(sent=49, hard_bounced=49), IdentityState.WARMING, None),
        (
            _window(sent=50, hard_bounced=2),
            IdentityState.THROTTLED,
            ReputationMetric.HARD_BOUNCE_RATE,
        ),
        (
            _window(sent=100, hard_bounced=3),
            IdentityState.THROTTLED,
            ReputationMetric.HARD_BOUNCE_RATE,
        ),
        (
            _window(sent=100, hard_bounced=5),
            IdentityState.SUSPENDED,
            ReputationMetric.HARD_BOUNCE_RATE,
        ),
        (
            _window(sent=1000, complaints=1),
            IdentityState.THROTTLED,
            ReputationMetric.COMPLAINT_RATE,
        ),
        (
            _window(sent=1000, complaints=3),
            IdentityState.SUSPENDED,
            ReputationMetric.COMPLAINT_RATE,
        ),
        (
            _window(spam_trap_hits=1),
            IdentityState.SUSPENDED,
            ReputationMetric.SPAM_TRAP,
        ),
        (
            _window(blocklist_hits=1),
            IdentityState.SUSPENDED,
            ReputationMetric.BLOCKLISTED,
        ),
    ],
)
async def test_decimal_threshold_truth_table_is_exact_and_returns_source_window(
    window: ReputationWindow,
    expected_state: IdentityState,
    expected_metric: ReputationMetric | None,
) -> None:
    """最小样本、等号、Decimal 比率与立即危害都按确定性真值表执行。"""
    service, factory, _, _ = _build()
    identity = _seed_sendable(factory)
    factory.identity_windows[str(identity.identity_id)] = window
    view = await service.evaluate_reputation(
        _TENANT, identity.identity_id, actor=_system(identity.identity_id)
    )
    assert view.sent_attempts == window.sent_attempts
    assert view.hard_bounce_rate == window.hard_bounce_rate
    assert view.complaint_rate == window.complaint_rate
    assert identity.state is expected_state
    if expected_metric is None:
        assert factory.actions.records == []
        assert factory.bus.events == []
        return
    assert len(factory.actions.records) == 1
    assert len(factory.bus.events) == 2
    state_event, breach = factory.bus.events
    assert isinstance(breach, ReputationThresholdBreached)
    assert breach.metric == expected_metric.value
    if expected_state is IdentityState.THROTTLED:
        assert isinstance(state_event, SendingIdentityThrottled)
        assert identity.suspension_category is None
    else:
        assert isinstance(state_event, SendingIdentitySuspended)
        assert identity.suspension_category is SuspensionCategory(expected_metric.value)


@pytest.mark.asyncio
async def test_equal_severity_metric_priority_is_stable() -> None:
    """同为停用时固定选择 blocklisted，而不受条件判断或 row 顺序影响。"""
    service, factory, _, _ = _build()
    identity = _seed_sendable(factory)
    factory.identity_windows[str(identity.identity_id)] = _window(
        sent=100,
        hard_bounced=10,
        complaints=10,
        spam_trap_hits=1,
        blocklist_hits=1,
    )
    await service.evaluate_reputation(
        _TENANT, identity.identity_id, actor=_system(identity.identity_id)
    )
    assert identity.suspension_category is SuspensionCategory.BLOCKLISTED
    breach = next(
        event
        for event in factory.bus.events
        if isinstance(event, ReputationThresholdBreached)
    )
    assert breach.metric == ReputationMetric.BLOCKLISTED.value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("window", "expected_metric", "expected_value", "expected_threshold"),
    [
        (
            _window(sent=1000, hard_bounced=30, complaints=3),
            ReputationMetric.COMPLAINT_RATE,
            Decimal(".003"),
            Decimal(".003"),
        ),
        (
            _window(sent=100, hard_bounced=5, spam_trap_hits=1),
            ReputationMetric.SPAM_TRAP,
            Decimal(1),
            Decimal(0),
        ),
        (
            _window(sent=1000, hard_bounced=50, complaints=3),
            ReputationMetric.HARD_BOUNCE_RATE,
            Decimal(".05"),
            Decimal(".05"),
        ),
        (
            _window(spam_trap_hits=1, blocklist_hits=1),
            ReputationMetric.BLOCKLISTED,
            Decimal(1),
            Decimal(0),
        ),
    ],
)
async def test_combined_metrics_choose_severity_before_full_stable_priority(
    window: ReputationWindow,
    expected_metric: ReputationMetric,
    expected_value: Decimal,
    expected_threshold: Decimal,
) -> None:
    """组合输入独立锁定 severity-first 与完整四级 metric 优先级。"""
    service, factory, _, _ = _build()
    identity = _seed_sendable(factory)
    factory.identity_windows[str(identity.identity_id)] = window
    await service.evaluate_reputation(
        _TENANT, identity.identity_id, actor=_system(identity.identity_id)
    )
    assert identity.state is IdentityState.SUSPENDED
    assert identity.suspension_category is SuspensionCategory(expected_metric.value)
    assert [
        (
            record.identity_id,
            record.before_state,
            record.after_state,
            record.action,
        )
        for record in factory.actions.records
    ] == [
        (
            identity.identity_id,
            IdentityState.WARMING,
            IdentityState.SUSPENDED,
            SendingIdentityAction.REPUTATION_EVALUATE,
        )
    ]
    state_event, breach = factory.bus.events
    assert isinstance(state_event, SendingIdentitySuspended)
    assert state_event.sending_identity_id == identity.identity_id
    assert state_event.reason == expected_metric.value
    assert isinstance(breach, ReputationThresholdBreached)
    assert (
        breach.sending_identity_id,
        breach.metric,
        Decimal(breach.value),
        Decimal(breach.threshold),
        breach.severity,
    ) == (
        identity.identity_id,
        expected_metric.value,
        expected_value,
        expected_threshold,
        ReputationSeverity.SUSPENDED.value,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [IdentityState.CREATED, IdentityState.AUTH_PENDING])
async def test_pre_sendable_states_record_facts_but_never_self_restrict(
    state: IdentityState,
) -> None:
    """CREATED/AUTH_PENDING 有危险事实也不能走非法状态跃迁。"""
    service, factory, _, _ = _build()
    identity = _seed(factory, state=state)
    factory.identity_windows[str(identity.identity_id)] = _window(blocklist_hits=1)
    result = await service.record_delivery_event(
        _TENANT,
        identity.identity_id,
        _event(identity.identity_id, key=f"provider:{state.value}"),
        actor=_system(identity.identity_id),
    )
    assert result
    assert identity.state is state
    assert len(factory.delivery_events) == 1
    assert factory.actions.records == []
    assert factory.bus.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [IdentityState.SUSPENDED, IdentityState.RETIRED])
async def test_already_terminal_restrictions_are_never_downgraded(
    state: IdentityState,
) -> None:
    """较轻 throttle 决策不得改变已停用或退休身份。"""
    service, factory, _, _ = _build()
    if state is IdentityState.SUSPENDED:
        identity = _seed_sendable(factory, state=IdentityState.SUSPENDED)
    else:
        identity = _seed(factory, state=IdentityState.RETIRED)
    factory.identity_windows[str(identity.identity_id)] = _window(
        sent=100, hard_bounced=3
    )
    await service.evaluate_reputation(
        _TENANT, identity.identity_id, actor=_system(identity.identity_id)
    )
    assert identity.state is state
    assert factory.actions.records == []
    assert factory.bus.events == []


@pytest.mark.asyncio
async def test_throttled_identity_only_upgrades_to_suspended() -> None:
    """THROTTLED 遇到 suspend 决策须升级且保留原 WARMING 恢复态。"""
    service, factory, _, _ = _build()
    identity = _seed_sendable(factory, state=IdentityState.THROTTLED)
    factory.identity_windows[str(identity.identity_id)] = _window(
        sent=100, hard_bounced=5
    )
    await service.evaluate_reputation(
        _TENANT, identity.identity_id, actor=_system(identity.identity_id)
    )
    assert identity.state is IdentityState.SUSPENDED
    assert identity.sendable_state_before_restriction is IdentityState.WARMING
    assert identity.suspension_category is SuspensionCategory.HARD_BOUNCE_RATE


def _add_domain_identity(
    factory,
    source: SendingIdentity,
    *,
    identity_id: SendingIdentityId,
    state: IdentityState,
    thresholds: ReputationThresholds,
) -> SendingIdentity:
    saved = IdentityState.WARMING if state is IdentityState.THROTTLED else None
    identity = SendingIdentity(
        identity_id=identity_id,
        tenant_id=_TENANT,
        address=f"{identity_id[-4:].lower()}@{source.domain}",
        domain=source.domain,
        role=DomainRole.COLD_OUTREACH,
        created_at=source.created_at,
        state=state,
        warmup_plan=WarmupPlan(_NOW.date() - timedelta(days=5), 100),
        thresholds=thresholds,
        activated_at=_NOW - timedelta(days=1) if state is IdentityState.ACTIVE else None,
        sendable_state_before_restriction=saved,
    )
    factory.identities[str(identity_id)] = identity
    return identity


@pytest.mark.asyncio
async def test_domain_fuse_uses_conservative_thresholds_and_fixed_lock_order() -> None:
    """阈值取同域最保守组合，锁 domain 后按 identity 排序，一次停用全部可发送态。"""
    service, factory, _, order = _build()
    source = _seed_sendable(factory)
    source.thresholds = ReputationThresholds(
        throttle_hard_bounce_rate=Decimal(".04"),
        suspend_hard_bounce_rate=Decimal(".08"),
        throttle_complaint_rate=Decimal(".002"),
        suspend_complaint_rate=Decimal(".004"),
        suspend_on_spam_trap=False,
        suspend_on_blocklist=False,
        minimum_sample=200,
    )
    active = _add_domain_identity(
        factory,
        source,
        identity_id=SendingIdentityId("sid_01K27XZA00ABCDEFGHJKMNPQRT"),
        state=IdentityState.ACTIVE,
        thresholds=ReputationThresholds(),
    )
    throttled = _add_domain_identity(
        factory,
        source,
        identity_id=SendingIdentityId("sid_01K27XZA00ABCDEFGHJKMNPQRU"),
        state=IdentityState.THROTTLED,
        thresholds=ReputationThresholds(minimum_sample=80),
    )
    factory.domain_windows[source.domain] = _window(sent=100, hard_bounced=5)
    await service.evaluate_reputation(
        _TENANT, source.identity_id, actor=_system(source.identity_id)
    )
    assert [source.state, active.state, throttled.state] == [
        IdentityState.SUSPENDED,
        IdentityState.SUSPENDED,
        IdentityState.SUSPENDED,
    ]
    assert all(
        item.suspension_category is SuspensionCategory.HARD_BOUNCE_RATE
        for item in (source, active, throttled)
    )
    assert {
        (
            record.identity_id,
            record.before_state,
            record.after_state,
            record.action,
        )
        for record in factory.actions.records
    } == {
        (
            source.identity_id,
            IdentityState.WARMING,
            IdentityState.SUSPENDED,
            SendingIdentityAction.REPUTATION_EVALUATE,
        ),
        (
            active.identity_id,
            IdentityState.ACTIVE,
            IdentityState.SUSPENDED,
            SendingIdentityAction.REPUTATION_EVALUATE,
        ),
        (
            throttled.identity_id,
            IdentityState.THROTTLED,
            IdentityState.SUSPENDED,
            SendingIdentityAction.REPUTATION_EVALUATE,
        ),
    }
    assert (
        source.sendable_state_before_restriction,
        active.sendable_state_before_restriction,
        throttled.sendable_state_before_restriction,
    ) == (IdentityState.WARMING, IdentityState.ACTIVE, IdentityState.WARMING)
    state_events = [
        event
        for event in factory.bus.events
        if isinstance(event, SendingIdentitySuspended)
    ]
    assert {
        (event.sending_identity_id, event.reason) for event in state_events
    } == {
        (source.identity_id, ReputationMetric.HARD_BOUNCE_RATE.value),
        (active.identity_id, ReputationMetric.HARD_BOUNCE_RATE.value),
        (throttled.identity_id, ReputationMetric.HARD_BOUNCE_RATE.value),
    }
    breaches = [
        event
        for event in factory.bus.events
        if isinstance(event, ReputationThresholdBreached)
    ]
    assert len(breaches) == 1
    assert (
        breaches[0].sending_identity_id,
        breaches[0].metric,
        Decimal(breaches[0].value),
        Decimal(breaches[0].threshold),
        breaches[0].severity,
    ) == (
        source.identity_id,
        ReputationMetric.HARD_BOUNCE_RATE.value,
        Decimal(".05"),
        Decimal(".05"),
        ReputationSeverity.SUSPENDED.value,
    )
    assert order.index("domain:ensure") < order.index("identity:list_domain")

    counts = (len(factory.actions.records), len(factory.bus.events))
    await service.evaluate_reputation(
        _TENANT, source.identity_id, actor=_system(source.identity_id)
    )
    assert (len(factory.actions.records), len(factory.bus.events)) == counts


@pytest.mark.asyncio
async def test_domain_immediate_hazard_switches_use_or_and_metric_priority() -> None:
    """任一同域 identity 开启 immediate fuse 即生效，且 blocklist 优先 spam trap。"""
    service, factory, _, _ = _build()
    source = _seed_sendable(factory)
    source.thresholds = ReputationThresholds(
        suspend_on_spam_trap=False,
        suspend_on_blocklist=False,
    )
    sibling = _add_domain_identity(
        factory,
        source,
        identity_id=SendingIdentityId("sid_01K27XZA00ABCDEFGHJKMNPQRT"),
        state=IdentityState.WARMING,
        thresholds=ReputationThresholds(),
    )
    factory.domain_windows[source.domain] = _window(
        spam_trap_hits=1, blocklist_hits=1
    )
    await service.evaluate_reputation(
        _TENANT, source.identity_id, actor=_system(source.identity_id)
    )
    assert source.state is sibling.state is IdentityState.SUSPENDED
    assert source.suspension_category is sibling.suspension_category is SuspensionCategory.BLOCKLISTED
    breach = next(
        event
        for event in factory.bus.events
        if isinstance(event, ReputationThresholdBreached)
    )
    assert breach.metric == ReputationMetric.BLOCKLISTED.value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("identity_window", "domain_window"),
    [
        (_window(sent=125, hard_bounced=3), _window()),
        (_window(), _window(sent=125, hard_bounced=3)),
        (_window(sent=1250, complaints=1), _window()),
        (_window(), _window(sent=1250, complaints=1)),
        (_window(spam_trap_hits=1), _window()),
        (_window(), _window(blocklist_hits=1)),
    ],
)
async def test_resume_from_throttle_rejects_equal_80_percent_and_hazards(
    identity_window: ReputationWindow,
    domain_window: ReputationWindow,
) -> None:
    """identity/domain 任一层等于 80% 或仍有 immediate hazard 都不得恢复。"""
    service, factory, audit, _ = _build()
    identity = _seed_sendable(factory, state=IdentityState.THROTTLED)
    factory.identity_windows[str(identity.identity_id)] = identity_window
    factory.domain_windows[identity.domain] = domain_window
    with pytest.raises(InvalidStateTransition):
        await service.resume_from_throttle(
            _TENANT, identity.identity_id, actor=_system(identity.identity_id)
        )
    assert identity.state is IdentityState.THROTTLED
    assert factory.actions.records == []
    assert audit.records == []


@pytest.mark.asyncio
@pytest.mark.parametrize("saved", [IdentityState.WARMING, IdentityState.ACTIVE])
async def test_resume_from_throttle_requires_auth_and_restores_exact_saved_state(
    saved: IdentityState,
) -> None:
    """低于固定恢复线且认证全过时只回 saved state，并清空受限字段。"""
    service, factory, audit, _ = _build()
    identity = _seed_sendable(factory, state=saved)
    identity.transition_to(IdentityState.THROTTLED)
    factory.identity_windows[str(identity.identity_id)] = _window(
        sent=100, hard_bounced=2
    )
    factory.domain_windows[identity.domain] = _window(
        sent=2000, complaints=1
    )
    await service.resume_from_throttle(
        _TENANT, identity.identity_id, actor=_system(identity.identity_id)
    )
    assert identity.state is saved
    assert identity.sendable_state_before_restriction is None
    assert identity.suspension_category is None
    assert len(factory.actions.records) == 1
    assert len(audit.records) == 1


@pytest.mark.asyncio
async def test_resume_from_throttle_rejects_latest_auth_failure_and_suspended_state() -> None:
    """minimum sample 不旁路认证，SUSPENDED 也不能走自动 throttle 恢复。"""
    service, factory, audit, _ = _build()
    identity = _seed_sendable(
        factory, state=IdentityState.THROTTLED, auth_passed=False
    )
    with pytest.raises(AuthenticationNotVerifiedError):
        await service.resume_from_throttle(
            _TENANT, identity.identity_id, actor=_system(identity.identity_id)
        )
    assert identity.state is IdentityState.THROTTLED
    assert audit.records == []

    identity.transition_to(
        IdentityState.SUSPENDED,
        suspension_category=SuspensionCategory.HARD_BOUNCE_RATE,
    )
    factory.auth_records.append(
        type(factory.auth_records[0])(
            auth_check_id="auth-latest-pass",
            tenant_id=_TENANT,
            identity_id=identity.identity_id,
            result=_auth(checked_at=_NOW + timedelta(seconds=1), check_ref="latest-pass"),
            created_at=_NOW + timedelta(seconds=1),
        )
    )
    with pytest.raises(InvalidStateTransition):
        await service.resume_from_throttle(
            _TENANT, identity.identity_id, actor=_system(identity.identity_id)
        )
