"""发件身份纯领域模型的行为契约。"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from domains.sending_identity import errors, models
from shared.errors import InvalidStateTransition, ValidationError
from shared.schemas.identifiers import SendingIdentityId, TenantId


def test_normalize_sending_domain_applies_idna_lowercase_and_root_dot() -> None:
    """缺少 IDNA/大小写/根点规范化会让同一域绕过角色隔离。"""
    assert models.normalize_sending_domain("EXAMPLE.COM.") == "example.com"
    assert models.normalize_sending_domain("bücher.example") == "xn--bcher-kva.example"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "localhost",
        "https://example.com",
        "a@example.com",
        "a..example",
        "example.com..",
        "a:443",
    ],
)
def test_normalize_sending_domain_rejects_non_domain_shapes(raw: str) -> None:
    """URL、邮箱或空标签不能作为发件域名。"""
    with pytest.raises(errors.InvalidSendingDomainError):
        models.normalize_sending_domain(raw)


@pytest.mark.parametrize(
    "raw",
    ["", "Bearer abc", "https://vault/item", "a\nb", "password" + "=" + "value", "x" * 65],
)
def test_validate_connector_ref_rejects_secret_or_transport_shapes(raw: str) -> None:
    """凭证或传输形态绝不能进入领域模型。"""
    with pytest.raises(errors.InvalidConnectorReferenceError):
        models.validate_connector_ref(raw)


def test_normalize_sending_address_requires_normalized_matching_domain() -> None:
    """邮箱域名不匹配会绕过域名角色和信誉聚合。"""
    assert (
        models.normalize_sending_address(" Sales@EXAMPLE.COM ", "example.com")
        == "sales@example.com"
    )
    with pytest.raises(errors.InvalidSendingAddressError):
        models.normalize_sending_address("sales@other.example", "example.com")


_EXPECTED_TRANSITIONS = {
    models.IdentityState.CREATED: {
        models.IdentityState.AUTH_PENDING,
        models.IdentityState.RETIRED,
    },
    models.IdentityState.AUTH_PENDING: {
        models.IdentityState.WARMING,
        models.IdentityState.RETIRED,
    },
    models.IdentityState.WARMING: {
        models.IdentityState.ACTIVE,
        models.IdentityState.THROTTLED,
        models.IdentityState.SUSPENDED,
        models.IdentityState.RETIRED,
    },
    models.IdentityState.ACTIVE: {
        models.IdentityState.THROTTLED,
        models.IdentityState.SUSPENDED,
        models.IdentityState.RETIRED,
    },
    models.IdentityState.THROTTLED: {
        models.IdentityState.WARMING,
        models.IdentityState.ACTIVE,
        models.IdentityState.SUSPENDED,
        models.IdentityState.RETIRED,
    },
    models.IdentityState.SUSPENDED: {
        models.IdentityState.WARMING,
        models.IdentityState.ACTIVE,
        models.IdentityState.RETIRED,
    },
    models.IdentityState.RETIRED: set(),
}


@pytest.mark.parametrize("state", list(models.IdentityState))
def test_state_transition_table_matches_the_independent_phase1_contract(
    state: models.IdentityState,
) -> None:
    """放宽、收紧或遗漏任一状态边都会被独立表格捕获。"""
    for target in models.IdentityState:
        if target in _EXPECTED_TRANSITIONS[state]:
            models.validate_identity_transition(state, target)
        else:
            with pytest.raises(InvalidStateTransition):
                models.validate_identity_transition(state, target)


def test_restricted_identity_recovers_only_to_saved_sendable_state() -> None:
    """预热中的身份熔断后不能借恢复直接变为 active。"""
    assert (
        models.recovery_state(models.IdentityState.THROTTLED, models.IdentityState.WARMING)
        is models.IdentityState.WARMING
    )
    assert (
        models.recovery_state(models.IdentityState.SUSPENDED, models.IdentityState.ACTIVE)
        is models.IdentityState.ACTIVE
    )
    with pytest.raises(InvalidStateTransition):
        models.recovery_state(models.IdentityState.THROTTLED, models.IdentityState.AUTH_PENDING)


def _identity(
    state: models.IdentityState,
    previous: models.IdentityState | None = None,
    category: object | None = None,
) -> models.SendingIdentity:
    kwargs = {"suspension_category": category} if category is not None else {}
    return models.SendingIdentity(
        identity_id=SendingIdentityId("sid-1"),
        tenant_id=TenantId("tenant-1"),
        address="sales@example.com",
        domain="example.com",
        role=models.DomainRole.COLD_OUTREACH,
        created_at=datetime(2026, 8, 10, tzinfo=UTC),
        state=state,
        sendable_state_before_restriction=previous,
        **kwargs,
    )


def test_restricting_and_escalating_identity_preserves_original_sendable_state() -> None:
    """熔断升级若覆盖 warming 原值，会让恢复绕过剩余预热。"""
    identity = _identity(models.IdentityState.WARMING)
    identity.transition_to(models.IdentityState.THROTTLED)
    assert identity.sendable_state_before_restriction is models.IdentityState.WARMING
    identity.transition_to(
        models.IdentityState.SUSPENDED,
        suspension_category=models.SuspensionCategory.SPAM_TRAP,
    )
    assert identity.sendable_state_before_restriction is models.IdentityState.WARMING
    assert identity.suspension_category is models.SuspensionCategory.SPAM_TRAP
    identity.transition_to(models.IdentityState.WARMING)
    assert identity.sendable_state_before_restriction is None
    assert identity.suspension_category is None


@pytest.mark.parametrize(
    ("state", "previous"),
    [
        (models.IdentityState.THROTTLED, None),
        (models.IdentityState.SUSPENDED, models.IdentityState.AUTH_PENDING),
        (models.IdentityState.ACTIVE, models.IdentityState.WARMING),
    ],
)
def test_identity_rejects_invalid_persisted_restriction_state(
    state: models.IdentityState, previous: models.IdentityState | None
) -> None:
    """损坏的持久化 restriction state 不能在后续恢复时扩权。"""
    with pytest.raises(ValidationError):
        _identity(state, previous)


def test_reputation_and_suspension_enums_are_closed_vocabularies() -> None:
    """信誉事件和停用原因必须是固定 typed vocabulary，不能携带自由文本。"""
    assert {item.value for item in models.SuspensionCategory} == {
        "authentication_regression",
        "hard_bounce_rate",
        "complaint_rate",
        "spam_trap",
        "blocklisted",
    }
    assert {item.value for item in models.ReputationMetric} == {
        "hard_bounce_rate",
        "complaint_rate",
        "spam_trap",
        "blocklisted",
    }
    assert {item.value for item in models.ReputationSeverity} == {
        "watch",
        "throttled",
        "suspended",
    }


def test_suspended_identity_requires_typed_category_and_other_states_forbid_it() -> None:
    """只有 suspended 可保存固定停用类别；缺失、字符串或其他状态携带均拒绝。"""
    with pytest.raises(ValidationError):
        _identity(models.IdentityState.SUSPENDED, models.IdentityState.ACTIVE)
    with pytest.raises(ValidationError):
        _identity(
            models.IdentityState.SUSPENDED,
            models.IdentityState.ACTIVE,
            "spam_trap",
        )
    suspended = _identity(
        models.IdentityState.SUSPENDED,
        models.IdentityState.ACTIVE,
        models.SuspensionCategory.SPAM_TRAP,
    )
    assert suspended.suspension_category is models.SuspensionCategory.SPAM_TRAP
    with pytest.raises(ValidationError):
        _identity(
            models.IdentityState.THROTTLED,
            models.IdentityState.ACTIVE,
            models.SuspensionCategory.SPAM_TRAP,
        )
    with pytest.raises(ValidationError):
        _identity(
            models.IdentityState.ACTIVE,
            category=models.SuspensionCategory.BLOCKLISTED,
        )


def test_transition_api_cannot_construct_invalid_suspension_state() -> None:
    """转为 suspended 必须显式 typed category，且其他目标不得夹带 category。"""
    identity = _identity(models.IdentityState.ACTIVE)
    with pytest.raises(InvalidStateTransition):
        identity.transition_to(models.IdentityState.SUSPENDED)
    assert identity.state is models.IdentityState.ACTIVE
    with pytest.raises(InvalidStateTransition):
        identity.transition_to(
            models.IdentityState.THROTTLED,
            suspension_category=models.SuspensionCategory.COMPLAINT_RATE,
        )
    assert identity.state is models.IdentityState.ACTIVE


@pytest.mark.parametrize(
    ("day_number", "expected"),
    [
        (0, 0),
        (1, 5),
        (3, 5),
        (4, 15),
        (7, 15),
        (8, 30),
        (14, 30),
        (15, 50),
        (21, 50),
        (22, 58),
        (28, 100),
        (29, 100),
    ],
)
def test_warmup_plan_target_100_has_exact_boundaries(
    day_number: int, expected: int
) -> None:
    """错误的第 22--28 天插值会过早扩大新域名发信量。"""
    started_on = date(2026, 8, 1)
    plan = models.WarmupPlan.create(started_on, 100)
    assert plan.daily_limit_on(started_on + timedelta(days=day_number - 1)) == expected


@pytest.mark.parametrize("target", [5, 50, 100])
def test_warmup_plan_is_monotone_bounded_and_stays_warming_for_28_days(
    target: int,
) -> None:
    """浮点插值或短计划会破坏预热上限或把低目标身份提前激活。"""
    started_on = date(2026, 8, 1)
    plan = models.WarmupPlan.create(started_on, target)
    limits = [plan.daily_limit_on(started_on + timedelta(days=offset)) for offset in range(28)]
    assert limits == sorted(limits)
    assert all(limit <= target for limit in limits)
    assert limits[-1] == target
    assert not plan.is_complete_on(started_on + timedelta(days=27))
    assert plan.is_complete_on(started_on + timedelta(days=28))


@pytest.mark.parametrize("target", [True, 4, 101])
def test_warmup_plan_direct_constructor_enforces_factory_invariants(target: int) -> None:
    """反序列化绕过 factory 不得跳过预热目标范围与真整数校验。"""
    with pytest.raises(ValidationError):
        models.WarmupPlan(date(2026, 8, 1), target)


def test_reputation_window_derives_decimal_rates_without_float() -> None:
    """浮点比率会使熔断阈值边界不确定。"""
    window = models.ReputationWindow(
        window_days=7,
        computed_at=datetime(2026, 8, 10, tzinfo=UTC),
        sent_attempts=100,
        delivered=93,
        hard_bounced=5,
        soft_bounced=2,
        complaints=1,
        unsubscribed=0,
    )
    assert window.hard_bounce_rate == Decimal("0.05")
    assert window.complaint_rate == Decimal("0.01")
    assert window.delivery_rate == Decimal("0.93")
    assert type(window.hard_bounce_rate) is Decimal


def test_reputation_window_returns_decimal_zero_for_empty_denominator() -> None:
    """零发送时的除零或 float 零会破坏信誉判定类型。"""
    window = models.ReputationWindow(
        window_days=7,
        computed_at=datetime(2026, 8, 10, tzinfo=UTC),
        sent_attempts=0,
        delivered=0,
        hard_bounced=0,
        soft_bounced=0,
        complaints=0,
        unsubscribed=0,
    )
    assert window.hard_bounce_rate == Decimal(0)
    assert window.complaint_rate == Decimal(0)
    assert window.delivery_rate == Decimal(0)


def test_thresholds_use_exact_decimal_defaults_and_reject_unsafe_values() -> None:
    """float、布尔样本量或倒置阈值会让信誉熔断失真。"""
    thresholds = models.ReputationThresholds()
    assert thresholds.throttle_hard_bounce_rate == Decimal(".03")
    assert thresholds.suspend_hard_bounce_rate == Decimal(".05")
    assert thresholds.throttle_complaint_rate == Decimal(".001")
    assert thresholds.suspend_complaint_rate == Decimal(".003")
    with pytest.raises(ValidationError):
        models.ReputationThresholds(throttle_hard_bounce_rate=0.03)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        models.ReputationThresholds(minimum_sample=True)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        models.ReputationThresholds(minimum_sample=0)
    with pytest.raises(ValidationError):
        models.ReputationThresholds(
            throttle_hard_bounce_rate=Decimal(".05"),
            suspend_hard_bounce_rate=Decimal(".05"),
        )
