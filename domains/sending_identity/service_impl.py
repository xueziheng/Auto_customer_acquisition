"""发件身份 lifecycle、认证门禁与安全查询实现。"""
from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal

from domains.sending_identity.errors import (
    AuthenticationNotVerifiedError,
    ColdOutreachDomainViolation,
    DomainRoleConflictError,
    IdentityRetiredError,
    IdentitySuspendedError,
    InvalidDeliveryEventError,
    SendingIdentityNotFoundError,
    WarmupLimitExceededError,
)
from domains.sending_identity.models import (
    DeliveryEventType,
    DomainRole,
    IdentityState,
    ReputationMetric,
    ReputationSeverity,
    ReputationThresholds,
    ReputationWindow,
    SendingIdentity,
    SuspensionCategory,
    WarmupPlan,
    normalize_sending_domain,
)
from domains.sending_identity.permissions import (
    Actor,
    AuditLogger,
    SendingIdentityAction,
    SendingIdentityAuthorizer,
)
from domains.sending_identity.repository import (
    AuthenticationCheckRecord,
    AuthenticationCheckRequestStatus,
    AuthenticationCheckRequestView,
    IdentityActionRecord,
    ReservationOutcome,
    SendingDomain,
    SendingIdentityUnitOfWork,
    SendingIdentityUnitOfWorkFactory,
)
from domains.sending_identity.schemas import (
    AuthenticationResult,
    AuthStatusView,
    DeliveryEventRecord,
    DomainReputationView,
    IdentityRegisterRequest,
    IdentityView,
    ReputationView,
    SendPermission,
    SendReservation,
    WarmupProgressView,
)
from domains.sending_identity.service import SendingIdentityService
from shared.errors import (
    IdempotencyConflict,
    InvalidStateTransition,
    PermissionDenied,
    TenantIsolationViolation,
    ValidationError,
)
from shared.events.catalog import (
    AuthenticationCheckRequested,
    ReputationThresholdBreached,
    SendingIdentityActivated,
    SendingIdentitySuspended,
    SendingIdentityThrottled,
)
from shared.schemas.identifiers import (
    AuthenticationCheckRequestId,
    IdempotencyKey,
    SendingIdentityId,
    TenantId,
    new_id,
)

_tenant_isolation_logger = logging.getLogger("security.tenant_isolation")
_SENDABLE = frozenset({IdentityState.WARMING, IdentityState.ACTIVE})
_REASON_NOT_SENDABLE = "发件身份尚未进入可发送预热"
_REASON_THROTTLED = "发件身份当前已限流"
_REASON_SUSPENDED = "发件身份当前已停用"
_REASON_RETIRED = "发件身份已退役"
_REASON_AUTH = "发件身份认证未全部通过"
_REASON_BEFORE_START = "发件身份预热尚未开始"
_REASON_WARMUP_INCOMPLETE = "发件身份预热尚未完成"
_REASON_CAP = "当日发送额度已用尽"
_REASON_IDENTITY_REPUTATION = "身份信誉窗口已触发发送限制"
_REASON_DOMAIN_REPUTATION = "域名信誉窗口已触发发送限制"
_SECRET_KEY_MARKERS = ("bearer", "token", "secret", "password")
_CANONICAL_ID = re.compile(r"[a-z]{2,8}_[0-7][0-9A-HJKMNP-TV-Z]{25}\Z")
_AUTH_REQUEST_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z")
_AUTH_REQUEST_SECRET_MARKERS = (*_SECRET_KEY_MARKERS, "authorization")
_RESUME_HARD_BOUNCE_RATE = Decimal(".024")
_RESUME_COMPLAINT_RATE = Decimal(".0008")


@dataclass(frozen=True)
class _ReputationDecision:
    target: IdentityState
    metric: ReputationMetric
    value: Decimal
    threshold: Decimal
    severity: ReputationSeverity


_SEVERITY_PRIORITY = {
    ReputationSeverity.THROTTLED: 1,
    ReputationSeverity.SUSPENDED: 2,
}
_METRIC_PRIORITY = {
    ReputationMetric.COMPLAINT_RATE: 1,
    ReputationMetric.HARD_BOUNCE_RATE: 2,
    ReputationMetric.SPAM_TRAP: 3,
    ReputationMetric.BLOCKLISTED: 4,
}


def _is_real_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_utc(value: datetime) -> bool:
    offset = value.utcoffset()
    return value.tzinfo is not None and offset is not None and offset.total_seconds() == 0


def _safe_text(value: str, *, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} 无效")
    cleaned = value.strip()
    if not 1 <= len(cleaned) <= maximum or any(char in cleaned for char in "\r\n"):
        raise ValidationError(f"{field} 无效")
    return cleaned


def _safe_reservation_key(value: IdempotencyKey) -> IdempotencyKey:
    """验证幂等键而不在异常中回显可能的凭证文本。"""
    if not isinstance(value, str):
        raise ValidationError("发送预留幂等键无效")
    cleaned = value.strip()
    lowered = cleaned.lower()
    if (
        not 1 <= len(cleaned) <= 200
        or any(unicodedata.category(character) == "Cc" for character in cleaned)
        or any(marker in lowered for marker in _SECRET_KEY_MARKERS)
    ):
        raise ValidationError("发送预留幂等键无效")
    return IdempotencyKey(cleaned)


def _safe_auth_request_inputs(
    identity_id: SendingIdentityId, request_key: IdempotencyKey
) -> tuple[SendingIdentityId, IdempotencyKey]:
    if (
        not isinstance(identity_id, str)
        or _CANONICAL_ID.fullmatch(identity_id) is None
        or not identity_id.startswith("sid_")
    ):
        raise ValidationError("认证检查请求无效")
    key = _safe_reservation_key(request_key)
    if (
        key != request_key
        or _AUTH_REQUEST_KEY.fullmatch(key) is None
        or any(marker in key.lower() for marker in _AUTH_REQUEST_SECRET_MARKERS)
    ):
        raise ValidationError("认证检查请求无效")
    return identity_id, key


def _safe_auth_request_id(
    request_id: AuthenticationCheckRequestId,
) -> AuthenticationCheckRequestId:
    if (
        not isinstance(request_id, str)
        or _CANONICAL_ID.fullmatch(request_id) is None
        or not request_id.startswith("acr_")
    ):
        raise ValidationError("认证检查请求 ID 无效")
    return request_id


def _reputation_blocks(window: ReputationWindow, identity: SendingIdentity) -> bool:
    thresholds = identity.thresholds
    if thresholds.suspend_on_spam_trap and window.spam_trap_hits > 0:
        return True
    if thresholds.suspend_on_blocklist and window.blocklist_hits > 0:
        return True
    if window.sent_attempts < thresholds.minimum_sample:
        return False
    return (
        window.hard_bounce_rate >= thresholds.throttle_hard_bounce_rate
        or window.complaint_rate >= thresholds.throttle_complaint_rate
    )


def _decision_priority(decision: _ReputationDecision) -> tuple[int, int]:
    return (
        _SEVERITY_PRIORITY[decision.severity],
        _METRIC_PRIORITY[decision.metric],
    )


def _strongest_decision(
    *decisions: _ReputationDecision | None,
) -> _ReputationDecision | None:
    available = [decision for decision in decisions if decision is not None]
    return max(available, key=_decision_priority, default=None)


def _with_delivery_event(
    window: ReputationWindow,
    event_type: DeliveryEventType,
) -> ReputationWindow:
    """将已接受但尚不在 now 窗口上界内的单条事实折入快照。"""
    if event_type is DeliveryEventType.DELIVERED:
        return replace(window, delivered=window.delivered + 1)
    if event_type is DeliveryEventType.HARD_BOUNCED:
        return replace(window, hard_bounced=window.hard_bounced + 1)
    if event_type is DeliveryEventType.SOFT_BOUNCED:
        return replace(window, soft_bounced=window.soft_bounced + 1)
    if event_type is DeliveryEventType.COMPLAINT:
        return replace(window, complaints=window.complaints + 1)
    if event_type is DeliveryEventType.UNSUBSCRIBED:
        return replace(window, unsubscribed=window.unsubscribed + 1)
    if event_type is DeliveryEventType.SPAM_TRAP:
        return replace(window, spam_trap_hits=window.spam_trap_hits + 1)
    if event_type is DeliveryEventType.BLOCKLISTED:
        return replace(window, blocklist_hits=window.blocklist_hits + 1)
    raise InvalidDeliveryEventError("投递事件无效")


def _evaluate_window(
    window: ReputationWindow,
    thresholds: ReputationThresholds,
) -> _ReputationDecision | None:
    """按 severity 后 metric 固定优先级纯函数求出窗口限制。"""
    decisions: list[_ReputationDecision] = []
    if thresholds.suspend_on_blocklist and window.blocklist_hits > 0:
        decisions.append(
            _ReputationDecision(
                IdentityState.SUSPENDED,
                ReputationMetric.BLOCKLISTED,
                Decimal(1),
                Decimal(0),
                ReputationSeverity.SUSPENDED,
            )
        )
    if thresholds.suspend_on_spam_trap and window.spam_trap_hits > 0:
        decisions.append(
            _ReputationDecision(
                IdentityState.SUSPENDED,
                ReputationMetric.SPAM_TRAP,
                Decimal(1),
                Decimal(0),
                ReputationSeverity.SUSPENDED,
            )
        )
    if window.sent_attempts >= thresholds.minimum_sample:
        rate_rules = (
            (
                ReputationMetric.HARD_BOUNCE_RATE,
                window.hard_bounce_rate,
                thresholds.throttle_hard_bounce_rate,
                thresholds.suspend_hard_bounce_rate,
            ),
            (
                ReputationMetric.COMPLAINT_RATE,
                window.complaint_rate,
                thresholds.throttle_complaint_rate,
                thresholds.suspend_complaint_rate,
            ),
        )
        for metric, value, throttle_threshold, suspend_threshold in rate_rules:
            if value >= suspend_threshold:
                decisions.append(
                    _ReputationDecision(
                        IdentityState.SUSPENDED,
                        metric,
                        value,
                        suspend_threshold,
                        ReputationSeverity.SUSPENDED,
                    )
                )
            elif value >= throttle_threshold:
                decisions.append(
                    _ReputationDecision(
                        IdentityState.THROTTLED,
                        metric,
                        value,
                        throttle_threshold,
                        ReputationSeverity.THROTTLED,
                    )
                )
    return _strongest_decision(*decisions)


def _conservative_thresholds(
    identities: list[SendingIdentity],
) -> ReputationThresholds:
    thresholds = [identity.thresholds for identity in identities]
    if not thresholds:
        return ReputationThresholds()
    return ReputationThresholds(
        throttle_hard_bounce_rate=min(
            item.throttle_hard_bounce_rate for item in thresholds
        ),
        suspend_hard_bounce_rate=min(
            item.suspend_hard_bounce_rate for item in thresholds
        ),
        throttle_complaint_rate=min(
            item.throttle_complaint_rate for item in thresholds
        ),
        suspend_complaint_rate=min(
            item.suspend_complaint_rate for item in thresholds
        ),
        suspend_on_spam_trap=any(item.suspend_on_spam_trap for item in thresholds),
        suspend_on_blocklist=any(item.suspend_on_blocklist for item in thresholds),
        minimum_sample=min(item.minimum_sample for item in thresholds),
    )


def _domain_reputation_controller(
    identities: list[SendingIdentity],
) -> SendingIdentityId | None:
    """按持久化限制状态与 canonical ID 确定唯一 controller。"""
    for state in (IdentityState.SUSPENDED, IdentityState.THROTTLED):
        candidates = sorted(
            (identity for identity in identities if identity.state is state),
            key=lambda identity: str(identity.identity_id),
        )
        if candidates:
            return candidates[0].identity_id
    return None


def _resume_window_is_safe(window: ReputationWindow) -> bool:
    return (
        window.hard_bounce_rate < _RESUME_HARD_BOUNCE_RATE
        and window.complaint_rate < _RESUME_COMPLAINT_RATE
        and window.spam_trap_hits == 0
        and window.blocklist_hits == 0
    )


def _permission_result(
    identity: SendingIdentity,
    *,
    allowed: bool,
    daily_limit: int,
    remaining_today: int,
    reason: str | None,
    on_day: date,
) -> SendPermission:
    plan = identity.warmup_plan
    return SendPermission(
        allowed=allowed,
        remaining_today=remaining_today,
        daily_limit=daily_limit,
        identity_state=identity.state,
        reason=reason,
        is_warming=identity.state is IdentityState.WARMING,
        warmup_day=(on_day - plan.started_on).days + 1 if plan is not None else None,
    )


def _decide_send_permission(
    identity: SendingIdentity,
    domain_role: DomainRole,
    latest_auth: AuthenticationCheckRecord | None,
    identity_window: ReputationWindow,
    domain_window: ReputationWindow,
    sent_attempts: int,
    *,
    on_day: date,
    for_cold_outreach: bool,
    include_capacity: bool,
) -> SendPermission:
    """根据同一快照纯函数判定；不写库也不缓存诊断结果。"""
    if for_cold_outreach and (
        identity.role is not DomainRole.COLD_OUTREACH
        or domain_role is not DomainRole.COLD_OUTREACH
    ):
        raise ColdOutreachDomainViolation("非冷开发域名不得用于冷开发")

    plan = identity.warmup_plan
    daily_limit = plan.daily_limit_on(on_day) if plan is not None else 0

    if identity.state is IdentityState.RETIRED:
        return _permission_result(
            identity,
            allowed=False,
            daily_limit=daily_limit,
            remaining_today=0,
            reason=_REASON_RETIRED,
            on_day=on_day,
        )
    if identity.state is IdentityState.SUSPENDED:
        return _permission_result(
            identity,
            allowed=False,
            daily_limit=daily_limit,
            remaining_today=0,
            reason=_REASON_SUSPENDED,
            on_day=on_day,
        )
    if identity.state is IdentityState.THROTTLED:
        return _permission_result(
            identity,
            allowed=False,
            daily_limit=daily_limit,
            remaining_today=0,
            reason=_REASON_THROTTLED,
            on_day=on_day,
        )
    if identity.state not in _SENDABLE or plan is None:
        return _permission_result(
            identity,
            allowed=False,
            daily_limit=daily_limit,
            remaining_today=0,
            reason=_REASON_NOT_SENDABLE,
            on_day=on_day,
        )
    if latest_auth is None or not latest_auth.result.all_passed:
        return _permission_result(
            identity,
            allowed=False,
            daily_limit=daily_limit,
            remaining_today=0,
            reason=_REASON_AUTH,
            on_day=on_day,
        )
    if daily_limit == 0:
        return _permission_result(
            identity,
            allowed=False,
            daily_limit=0,
            remaining_today=0,
            reason=_REASON_BEFORE_START,
            on_day=on_day,
        )
    if identity.state is IdentityState.ACTIVE and not plan.is_complete_on(on_day):
        return _permission_result(
            identity,
            allowed=False,
            daily_limit=daily_limit,
            remaining_today=0,
            reason=_REASON_WARMUP_INCOMPLETE,
            on_day=on_day,
        )
    if _reputation_blocks(identity_window, identity):
        return _permission_result(
            identity,
            allowed=False,
            daily_limit=daily_limit,
            remaining_today=0,
            reason=_REASON_IDENTITY_REPUTATION,
            on_day=on_day,
        )
    if _reputation_blocks(domain_window, identity):
        return _permission_result(
            identity,
            allowed=False,
            daily_limit=daily_limit,
            remaining_today=0,
            reason=_REASON_DOMAIN_REPUTATION,
            on_day=on_day,
        )
    if include_capacity and sent_attempts >= daily_limit:
        return _permission_result(
            identity,
            allowed=False,
            daily_limit=daily_limit,
            remaining_today=0,
            reason=_REASON_CAP,
            on_day=on_day,
        )
    return _permission_result(
        identity,
        allowed=True,
        daily_limit=daily_limit,
        remaining_today=max(0, daily_limit - sent_attempts),
        reason=None,
        on_day=on_day,
    )


class SendingIdentityServiceImpl:
    """Slice 4A 发件身份生命周期、发送门禁与信誉熔断完整实现。"""

    def __init__(
        self,
        uow_factory: SendingIdentityUnitOfWorkFactory,
        authorizer: SendingIdentityAuthorizer,
        audit_logger: AuditLogger,
        *,
        now: Callable[[], datetime],
    ) -> None:
        self._uow_factory = uow_factory
        self._authorizer = authorizer
        self._audit = audit_logger
        self._clock = now

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or not _is_utc(value):
            raise ValidationError("服务时钟必须为 UTC")
        return value

    def _audit_authorization_deny(
        self,
        actor: object,
        action: SendingIdentityAction,
        tenant_id: TenantId,
    ) -> None:
        actor_id = actor.actor_id if isinstance(actor, Actor) else "unknown"
        scope = actor.scope.label if isinstance(actor, Actor) else "none"
        self._audit.log(
            actor=actor_id,
            action=action.value,
            tenant_id=tenant_id,
            scope=scope,
            rule="deny:authorization",
        )

    def _preauthorize(
        self,
        actor: Actor,
        action: SendingIdentityAction,
        tenant_id: TenantId,
    ) -> None:
        if not isinstance(actor, Actor):
            self._audit_authorization_deny(actor, action, tenant_id)
            raise PermissionDenied("Phase 1 发件身份授权拒绝")
        try:
            self._authorizer.preauthorize(actor, action, actor.scope, tenant_id)
        except PermissionDenied:
            self._audit_authorization_deny(actor, action, tenant_id)
            raise

    def _authorize_resource(
        self,
        actor: Actor,
        action: SendingIdentityAction,
        tenant_id: TenantId,
        *,
        identity_id: SendingIdentityId | None = None,
        domain: str | None = None,
    ) -> str:
        try:
            return self._authorizer.require(
                actor,
                action,
                actor.scope,
                tenant_id,
                identity_id=identity_id,
                domain=domain,
            )
        except PermissionDenied:
            self._audit_authorization_deny(actor, action, tenant_id)
            raise

    def _audit_allow(
        self,
        actor: Actor,
        action: SendingIdentityAction,
        tenant_id: TenantId,
        rule: str,
    ) -> None:
        self._audit.log(
            actor=actor.actor_id,
            action=action.value,
            tenant_id=tenant_id,
            scope=actor.scope.label,
            rule=rule,
        )

    def _deny_tenant_isolation(
        self,
        actor: Actor,
        action: SendingIdentityAction,
        tenant_id: TenantId,
    ) -> None:
        self._audit.log(
            actor=actor.actor_id,
            action=action.value,
            tenant_id=tenant_id,
            scope=actor.scope.label,
            rule="deny:tenant_isolation",
        )
        _tenant_isolation_logger.critical(
            "检测到跨租户数据隔离违规",
            extra={
                "actor": actor.actor_id,
                "action": action.value,
                "tenant_id": str(tenant_id),
                "scope": actor.scope.label,
                "rule": "deny:tenant_isolation",
            },
        )
        raise TenantIsolationViolation("检测到跨租户数据隔离违规")

    def _authorize_identity_row(
        self,
        identity: SendingIdentity,
        actor: Actor,
        action: SendingIdentityAction,
        tenant_id: TenantId,
    ) -> str:
        if identity.tenant_id != tenant_id:
            self._deny_tenant_isolation(actor, action, tenant_id)
        return self._authorize_resource(
            actor,
            action,
            tenant_id,
            identity_id=identity.identity_id,
            domain=identity.domain,
        )

    async def _locked_identity(
        self,
        uow: SendingIdentityUnitOfWork,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
        actor: Actor,
        action: SendingIdentityAction,
    ) -> tuple[SendingIdentity, str]:
        identity = await uow.identities.get(tenant_id, identity_id, for_update=True)
        if identity is None:
            raise SendingIdentityNotFoundError("发件身份不存在或不属于当前租户")
        rule = self._authorize_identity_row(identity, actor, action, tenant_id)
        return identity, rule

    async def _record_action(
        self,
        uow: SendingIdentityUnitOfWork,
        identity: SendingIdentity,
        *,
        action: SendingIdentityAction,
        before: IdentityState | None,
        after: IdentityState | None,
        actor: Actor,
        rule: str,
        version: int,
        occurred_at: datetime,
        note: str | None = None,
    ) -> None:
        action_key = f"identity:{identity.identity_id}:v{version}:{action.value}"
        if await uow.actions.exists_by_key(
            identity.tenant_id, identity.identity_id, action_key
        ):
            return
        await uow.actions.add(
            IdentityActionRecord(
                action_id=new_id("act"),
                tenant_id=identity.tenant_id,
                identity_id=identity.identity_id,
                action_key=action_key,
                action=action,
                before_state=before,
                after_state=after,
                actor_id=actor.actor_id,
                scope=actor.scope.label,
                rule=rule,
                note=note,
                occurred_at=occurred_at,
            )
        )

    async def _lock_reputation_domain(
        self,
        uow: SendingIdentityUnitOfWork,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
        actor: Actor,
        action: SendingIdentityAction,
    ) -> tuple[SendingIdentity, list[SendingIdentity], str]:
        """固定按 domain row → identity_id ASC 锁定并复核 source。"""
        hint = await uow.identities.get(tenant_id, identity_id)
        if hint is None:
            raise SendingIdentityNotFoundError("发件身份不存在或不属于当前租户")
        if hint.tenant_id != tenant_id:
            self._deny_tenant_isolation(actor, action, tenant_id)
        domain = await uow.domains.ensure(
            SendingDomain(
                tenant_id=tenant_id,
                domain=hint.domain,
                role=hint.role,
                created_at=hint.created_at,
            )
        )
        if domain.tenant_id != tenant_id:
            self._deny_tenant_isolation(actor, action, tenant_id)
        if domain.domain != hint.domain or domain.role is not hint.role:
            raise DomainRoleConflictError("发件域名角色冲突")
        identities = await uow.identities.list_domain_for_update(
            tenant_id, domain.domain
        )
        if [str(item.identity_id) for item in identities] != sorted(
            str(item.identity_id) for item in identities
        ):
            raise ValidationError("发件身份锁顺序无效")
        source: SendingIdentity | None = None
        for identity in identities:
            if identity.tenant_id != tenant_id:
                self._deny_tenant_isolation(actor, action, tenant_id)
            if identity.domain != domain.domain or identity.role is not domain.role:
                raise DomainRoleConflictError("发件域名角色冲突")
            if identity.identity_id == identity_id:
                source = identity
        if source is None:
            raise SendingIdentityNotFoundError("发件身份不存在或不属于当前租户")
        rule = self._authorize_identity_row(source, actor, action, tenant_id)
        return source, identities, rule

    async def _evaluate_locked(
        self,
        uow: SendingIdentityUnitOfWork,
        tenant_id: TenantId,
        source: SendingIdentity,
        identities: list[SendingIdentity],
        *,
        actor: Actor,
        action: SendingIdentityAction,
        rule: str,
        now: datetime,
        event: DeliveryEventRecord | None = None,
    ) -> ReputationWindow:
        """在持有 domain/identity 锁的当前 UoW 内评估并写完整熔断副作用。"""
        identity_window = await uow.reputation.compute_window(
            tenant_id, source.identity_id, 7, now
        )
        domain_window = await uow.reputation.compute_domain_window(
            tenant_id, source.domain, 7, now
        )
        if event is not None and event.occurred_at > now:
            identity_window = _with_delivery_event(identity_window, event.event_type)
            domain_window = _with_delivery_event(domain_window, event.event_type)
        identity_decision = _evaluate_window(identity_window, source.thresholds)
        domain_decision = _evaluate_window(
            domain_window, _conservative_thresholds(identities)
        )
        state_events: list[SendingIdentityThrottled | SendingIdentitySuspended] = []
        applied: list[_ReputationDecision] = []
        for identity in identities:
            decision = domain_decision
            if identity.identity_id == source.identity_id:
                decision = _strongest_decision(identity_decision, domain_decision)
            if decision is None:
                continue
            if identity.state in _SENDABLE:
                target = decision.target
            elif (
                identity.state is IdentityState.THROTTLED
                and decision.target is IdentityState.SUSPENDED
            ):
                target = IdentityState.SUSPENDED
            else:
                continue
            before = identity.state
            category = (
                SuspensionCategory(decision.metric.value)
                if target is IdentityState.SUSPENDED
                else None
            )
            identity.transition_to(target, suspension_category=category)
            identity.suspended_at = now if target is IdentityState.SUSPENDED else None
            next_version = identity.version + 1
            await uow.identities.update(identity)
            await self._record_action(
                uow,
                identity,
                action=action,
                before=before,
                after=target,
                actor=actor,
                rule=rule,
                version=next_version,
                occurred_at=now,
            )
            if target is IdentityState.THROTTLED:
                state_events.append(
                    SendingIdentityThrottled(
                        tenant_id=tenant_id,
                        occurred_at=now,
                        sending_identity_id=identity.identity_id,
                        new_state=target.value,
                        trigger_metric=decision.metric.value,
                        metric_value=str(decision.value),
                    )
                )
            else:
                state_events.append(
                    SendingIdentitySuspended(
                        tenant_id=tenant_id,
                        occurred_at=now,
                        sending_identity_id=identity.identity_id,
                        reason=category.value if category is not None else "",
                    )
                )
            applied.append(decision)
        trigger = _strongest_decision(*applied)
        if trigger is not None:
            await uow.bus.publish_many(
                [
                    *state_events,
                    ReputationThresholdBreached(
                        tenant_id=tenant_id,
                        occurred_at=now,
                        sending_identity_id=source.identity_id,
                        metric=trigger.metric.value,
                        value=str(trigger.value),
                        threshold=str(trigger.threshold),
                        severity=trigger.severity.value,
                    ),
                ]
            )
        return identity_window

    async def register(
        self,
        tenant_id: TenantId,
        request: IdentityRegisterRequest,
        *,
        actor: Actor,
    ) -> SendingIdentityId:
        action = SendingIdentityAction.IDENTITY_REGISTER
        self._preauthorize(actor, action, tenant_id)
        now = self._now()
        display_name = None
        if request.display_name is not None:
            display_name = _safe_text(request.display_name, field="显示名", maximum=200)
        candidate = SendingIdentity(
            identity_id=SendingIdentityId(new_id("sid")),
            tenant_id=tenant_id,
            address=request.address,
            domain=request.domain,
            role=request.role,
            created_at=now,
            display_name=display_name,
            connector_ref=request.connector_ref,
        )
        rule = self._authorize_resource(
            actor,
            action,
            tenant_id,
            identity_id=candidate.identity_id,
            domain=candidate.domain,
        )
        async with self._uow_factory(tenant_id) as uow:
            domain = await uow.domains.ensure(
                SendingDomain(tenant_id, candidate.domain, candidate.role, now)
            )
            if domain.tenant_id != tenant_id:
                self._deny_tenant_isolation(actor, action, tenant_id)
            if domain.role is not candidate.role:
                raise DomainRoleConflictError("发件域名角色冲突")
            result = await uow.identities.register_if_address_absent(candidate)
            winner = result.winner
            if winner.tenant_id != tenant_id:
                self._deny_tenant_isolation(actor, action, tenant_id)
            registration_fields = (
                "address",
                "domain",
                "role",
                "display_name",
                "connector_ref",
            )
            if any(
                getattr(winner, field) != getattr(candidate, field)
                for field in registration_fields
            ):
                raise ValidationError("发件身份登记冲突")
            if result.created:
                await self._record_action(
                    uow,
                    winner,
                    action=action,
                    before=None,
                    after=IdentityState.CREATED,
                    actor=actor,
                    rule=rule,
                    version=0,
                    occurred_at=now,
                )
            identity_id = winner.identity_id
        self._audit_allow(actor, action, tenant_id, rule)
        return identity_id

    async def begin_authentication(
        self,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
        *,
        actor: Actor,
    ) -> None:
        action = SendingIdentityAction.AUTH_CHECK_BEGIN
        self._preauthorize(actor, action, tenant_id)
        now = self._now()
        async with self._uow_factory(tenant_id) as uow:
            identity, rule = await self._locked_identity(
                uow, tenant_id, identity_id, actor, action
            )
            before = identity.state
            identity.transition_to(IdentityState.AUTH_PENDING)
            next_version = identity.version + 1
            await uow.identities.update(identity)
            await self._record_action(
                uow,
                identity,
                action=action,
                before=before,
                after=identity.state,
                actor=actor,
                rule=rule,
                version=next_version,
                occurred_at=now,
            )
        self._audit_allow(actor, action, tenant_id, rule)

    async def request_authentication_check(
        self,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
        request_key: IdempotencyKey,
        *,
        actor: Actor,
    ) -> AuthenticationCheckRequestView:
        """原子创建认证请求、必要的首次状态迁移和 outbox 事件。"""
        action = SendingIdentityAction.AUTH_CHECK_BEGIN
        self._preauthorize(actor, action, tenant_id)
        identity_id, request_key = _safe_auth_request_inputs(identity_id, request_key)
        now = self._now()
        candidate = AuthenticationCheckRequestView(
            AuthenticationCheckRequestId(new_id("acr")),
            tenant_id,
            identity_id,
            request_key,
            AuthenticationCheckRequestStatus.REQUESTED,
            now,
            None,
        )
        async with self._uow_factory(tenant_id) as uow:
            identity, rule = await self._locked_identity(
                uow, tenant_id, identity_id, actor, action
            )
            if identity.state is IdentityState.RETIRED:
                raise InvalidStateTransition("退休身份不能请求认证检查")
            created = await uow.auth_check_requests.create_or_get(candidate)
            winner = created.winner
            if winner.tenant_id != tenant_id:
                self._deny_tenant_isolation(actor, action, tenant_id)
            if winner.sending_identity_id != identity_id:
                raise IdempotencyConflict("认证检查请求幂等冲突")
            if created.created:
                if identity.state is IdentityState.CREATED:
                    before = identity.state
                    identity.transition_to(IdentityState.AUTH_PENDING)
                    next_version = identity.version + 1
                    await uow.identities.update(identity)
                    await self._record_action(
                        uow,
                        identity,
                        action=action,
                        before=before,
                        after=identity.state,
                        actor=actor,
                        rule=rule,
                        version=next_version,
                        occurred_at=now,
                    )
                await uow.bus.publish(
                    AuthenticationCheckRequested(
                        tenant_id=tenant_id,
                        occurred_at=now,
                        request_id=winner.request_id,
                        sending_identity_id=identity_id,
                    )
                )
        self._audit_allow(actor, action, tenant_id, rule)
        return winner

    async def transition_authentication_check_request(
        self,
        tenant_id: TenantId,
        request_id: AuthenticationCheckRequestId,
        target_status: AuthenticationCheckRequestStatus,
        *,
        actor: Actor,
    ) -> AuthenticationCheckRequestView:
        """由单身份 SYSTEM actor 推动请求的严格状态机。"""
        action = SendingIdentityAction.AUTH_RESULT_RECORD
        self._preauthorize(actor, action, tenant_id)
        request_id = _safe_auth_request_id(request_id)
        if not isinstance(target_status, AuthenticationCheckRequestStatus):
            raise ValidationError("认证检查请求状态无效")
        now = self._now()
        async with self._uow_factory(tenant_id) as uow:
            current = await uow.auth_check_requests.get(
                tenant_id, request_id, for_update=True
            )
            if current is None:
                raise SendingIdentityNotFoundError("认证检查请求不存在或不属于当前租户")
            rule = self._authorize_resource(
                actor,
                action,
                tenant_id,
                identity_id=current.sending_identity_id,
            )
            if (
                current.status is AuthenticationCheckRequestStatus.RUNNING
                and target_status is AuthenticationCheckRequestStatus.RUNNING
            ):
                updated = current
            else:
                completed_at = (
                    now
                    if target_status
                    in {
                        AuthenticationCheckRequestStatus.SUCCEEDED,
                        AuthenticationCheckRequestStatus.FAILED,
                    }
                    else None
                )
                updated = await uow.auth_check_requests.transition(
                    tenant_id, request_id, target_status, completed_at
                )
        self._audit_allow(actor, action, tenant_id, rule)
        return updated

    async def get_authentication_check_request(
        self,
        tenant_id: TenantId,
        request_id: AuthenticationCheckRequestId,
        *,
        actor: Actor,
    ) -> AuthenticationCheckRequestView:
        """读取 tenant-bound 请求状态，供 workflow 在崩溃重放时收敛。"""
        action = SendingIdentityAction.AUTH_RESULT_RECORD
        self._preauthorize(actor, action, tenant_id)
        request_id = _safe_auth_request_id(request_id)
        async with self._uow_factory(tenant_id) as uow:
            current = await uow.auth_check_requests.get(tenant_id, request_id)
            if current is None:
                raise SendingIdentityNotFoundError(
                    "认证检查请求不存在或不属于当前租户"
                )
            rule = self._authorize_resource(
                actor,
                action,
                tenant_id,
                identity_id=current.sending_identity_id,
            )
        self._audit_allow(actor, action, tenant_id, rule)
        return current

    async def record_authentication_result(
        self,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
        result: AuthenticationResult,
        *,
        actor: Actor,
    ) -> None:
        action = SendingIdentityAction.AUTH_RESULT_RECORD
        self._preauthorize(actor, action, tenant_id)
        now = self._now()
        async with self._uow_factory(tenant_id) as uow:
            identity, rule = await self._locked_identity(
                uow, tenant_id, identity_id, actor, action
            )
            if identity.state is IdentityState.RETIRED:
                raise InvalidStateTransition("退休身份不能记录认证结果")
            if result.checked_at < identity.created_at or result.checked_at > now + timedelta(minutes=5):
                raise ValidationError("认证检查时间无效")
            record = AuthenticationCheckRecord(
                auth_check_id=new_id("auth"),
                tenant_id=tenant_id,
                identity_id=identity_id,
                result=result,
                created_at=now,
            )
            appended = await uow.auth_checks.append_if_ref_absent(record)
            if appended.winner.result != result:
                raise ValidationError("认证检查引用冲突")
            latest = await uow.auth_checks.latest_for_identity(tenant_id, identity_id)
            is_latest = latest is not None and latest.auth_check_id == appended.winner.auth_check_id
            was_new_latest = appended.created and is_latest
            if (
                was_new_latest
                and not result.all_passed
                and identity.state
                in {IdentityState.WARMING, IdentityState.ACTIVE, IdentityState.THROTTLED}
            ):
                before = identity.state
                identity.transition_to(
                    IdentityState.SUSPENDED,
                    suspension_category=SuspensionCategory.AUTHENTICATION_REGRESSION,
                )
                identity.suspended_at = now
                next_version = identity.version + 1
                await uow.identities.update(identity)
                await self._record_action(
                    uow,
                    identity,
                    action=action,
                    before=before,
                    after=identity.state,
                    actor=actor,
                    rule=rule,
                    version=next_version,
                    occurred_at=now,
                )
                await uow.bus.publish(
                    SendingIdentitySuspended(
                        tenant_id=tenant_id,
                        occurred_at=now,
                        run_id=None,
                        sending_identity_id=identity_id,
                        reason=SuspensionCategory.AUTHENTICATION_REGRESSION.value,
                    )
                )
        self._audit_allow(actor, action, tenant_id, rule)

    async def start_warmup(
        self,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
        target_daily_volume: int,
        *,
        actor: Actor,
    ) -> None:
        action = SendingIdentityAction.WARMUP_START
        self._preauthorize(actor, action, tenant_id)
        now = self._now()
        async with self._uow_factory(tenant_id) as uow:
            identity, rule = await self._locked_identity(
                uow, tenant_id, identity_id, actor, action
            )
            if identity.state is not IdentityState.AUTH_PENDING:
                raise InvalidStateTransition("只有待认证身份可以开始预热")
            latest = await uow.auth_checks.latest_for_identity(tenant_id, identity_id)
            if latest is None or not latest.result.all_passed:
                raise AuthenticationNotVerifiedError("认证未全部通过")
            plan = WarmupPlan.create(now.date(), target_daily_volume)
            before = identity.state
            identity.warmup_plan = plan
            identity.transition_to(IdentityState.WARMING)
            next_version = identity.version + 1
            await uow.identities.update(identity)
            await self._record_action(
                uow,
                identity,
                action=action,
                before=before,
                after=identity.state,
                actor=actor,
                rule=rule,
                version=next_version,
                occurred_at=now,
            )
        self._audit_allow(actor, action, tenant_id, rule)

    async def advance_warmup(
        self,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
        *,
        actor: Actor,
    ) -> None:
        action = SendingIdentityAction.WARMUP_ADVANCE
        self._preauthorize(actor, action, tenant_id)
        now = self._now()
        async with self._uow_factory(tenant_id) as uow:
            identity, rule = await self._locked_identity(
                uow, tenant_id, identity_id, actor, action
            )
            if identity.state is IdentityState.ACTIVE:
                pass
            elif identity.state is not IdentityState.WARMING or identity.warmup_plan is None:
                raise InvalidStateTransition("只有预热身份可以推进预热")
            elif identity.warmup_plan.is_complete_on(now.date()):
                before = identity.state
                identity.transition_to(IdentityState.ACTIVE)
                identity.activated_at = now
                next_version = identity.version + 1
                await uow.identities.update(identity)
                await self._record_action(
                    uow,
                    identity,
                    action=action,
                    before=before,
                    after=identity.state,
                    actor=actor,
                    rule=rule,
                    version=next_version,
                    occurred_at=now,
                )
                await uow.bus.publish(
                    SendingIdentityActivated(
                        tenant_id=tenant_id,
                        occurred_at=now,
                        run_id=None,
                        sending_identity_id=identity_id,
                    )
                )
        self._audit_allow(actor, action, tenant_id, rule)

    async def check_send_permission(
        self,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
        for_cold_outreach: bool,
        *,
        actor: Actor,
    ) -> SendPermission:
        """返回当前快照诊断；调用方不得将结果当作发送授权。"""
        action = SendingIdentityAction.SEND_PERMISSION_READ
        self._preauthorize(actor, action, tenant_id)
        now = self._now()
        async with self._uow_factory(tenant_id) as uow:
            identity = await uow.identities.get(tenant_id, identity_id)
            if identity is None:
                raise SendingIdentityNotFoundError("发件身份不存在或不属于当前租户")
            rule = self._authorize_identity_row(
                identity, actor, action, tenant_id
            )
            if not isinstance(for_cold_outreach, bool):
                raise ValidationError("冷开发标识无效")
            domain = await uow.domains.get(tenant_id, identity.domain)
            if domain is None:
                raise ValidationError("发件身份持久化数据无效")
            if domain.tenant_id != tenant_id:
                self._deny_tenant_isolation(actor, action, tenant_id)
            if domain.domain != identity.domain or domain.role is not identity.role:
                raise DomainRoleConflictError("发件域名角色冲突")
            latest_auth = await uow.auth_checks.latest_for_identity(
                tenant_id, identity_id
            )
            identity_window = await uow.reputation.compute_window(
                tenant_id, identity_id, 7, now
            )
            domain_window = await uow.reputation.compute_domain_window(
                tenant_id, identity.domain, 7, now
            )
            sent_attempts = await uow.counters.get_count(
                tenant_id, identity_id, now.date()
            )
            permission = _decide_send_permission(
                identity,
                domain.role,
                latest_auth,
                identity_window,
                domain_window,
                sent_attempts,
                on_day=now.date(),
                for_cold_outreach=for_cold_outreach,
                include_capacity=True,
            )
        self._audit_allow(actor, action, tenant_id, rule)
        return permission

    async def reserve_send_slot(
        self,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
        reservation_key: IdempotencyKey,
        for_cold_outreach: bool,
        *,
        actor: Actor,
    ) -> SendReservation:
        """在单一事务中重新判定门禁并原子占用发送名额。"""
        action = SendingIdentityAction.SEND_SLOT_RESERVE
        self._preauthorize(actor, action, tenant_id)
        now = self._now()
        async with self._uow_factory(tenant_id) as uow:
            hint = await uow.identities.get(tenant_id, identity_id)
            if hint is None:
                raise SendingIdentityNotFoundError("发件身份不存在或不属于当前租户")
            rule = self._authorize_identity_row(hint, actor, action, tenant_id)
            if not isinstance(for_cold_outreach, bool):
                raise ValidationError("冷开发标识无效")
            safe_key = _safe_reservation_key(reservation_key)

            hint_domain = hint.domain
            hint_role = hint.role
            domain = await uow.domains.ensure(
                SendingDomain(
                    tenant_id=tenant_id,
                    domain=hint_domain,
                    role=hint_role,
                    created_at=hint.created_at,
                )
            )
            if domain.tenant_id != tenant_id:
                self._deny_tenant_isolation(actor, action, tenant_id)
            if domain.domain != hint_domain or domain.role is not hint_role:
                raise DomainRoleConflictError("发件域名角色冲突")

            identity = await uow.identities.get(
                tenant_id, identity_id, for_update=True
            )
            if identity is None:
                raise SendingIdentityNotFoundError("发件身份不存在或不属于当前租户")
            if identity.tenant_id != tenant_id:
                self._deny_tenant_isolation(actor, action, tenant_id)
            if identity.domain != domain.domain or identity.role is not domain.role:
                raise DomainRoleConflictError("发件域名角色冲突")

            latest_auth = await uow.auth_checks.latest_for_identity(
                tenant_id, identity_id
            )
            identity_window = await uow.reputation.compute_window(
                tenant_id, identity_id, 7, now
            )
            domain_window = await uow.reputation.compute_domain_window(
                tenant_id, identity.domain, 7, now
            )
            sent_attempts = await uow.counters.get_count(
                tenant_id, identity_id, now.date()
            )
            permission = _decide_send_permission(
                identity,
                domain.role,
                latest_auth,
                identity_window,
                domain_window,
                sent_attempts,
                on_day=now.date(),
                for_cold_outreach=for_cold_outreach,
                include_capacity=False,
            )
            if not permission.allowed:
                if permission.identity_state is IdentityState.RETIRED:
                    raise IdentityRetiredError("发件身份已退役")
                if permission.identity_state is IdentityState.SUSPENDED:
                    raise IdentitySuspendedError("发件身份当前已停用")
                if permission.reason in {_REASON_NOT_SENDABLE, _REASON_AUTH}:
                    raise AuthenticationNotVerifiedError("发件身份尚未完成可发送认证与预热")
                raise WarmupLimitExceededError("发件身份当前不可占用发送名额")

            result = await uow.reservations.reserve_if_below(
                tenant_id,
                identity_id,
                safe_key,
                now.date(),
                permission.daily_limit,
                now,
            )
            if result.outcome is ReservationOutcome.CAP_REACHED:
                raise WarmupLimitExceededError("当日发送额度已用尽")
            if result.reservation is None:
                raise RuntimeError("reservation repository 返回无效")
            reservation = result.reservation
            if result.outcome is ReservationOutcome.EXISTING:
                plan = identity.warmup_plan
                if plan is None:
                    raise AuthenticationNotVerifiedError("发件身份尚未完成可发送认证与预热")
                original_limit = plan.daily_limit_on(reservation.on_day)
                reservation = SendReservation(
                    reservation_id=reservation.reservation_id,
                    identity_id=reservation.identity_id,
                    reservation_key=reservation.reservation_key,
                    on_day=reservation.on_day,
                    sequence=reservation.sequence,
                    daily_limit=original_limit,
                    remaining_today=max(0, original_limit - reservation.sequence),
                )
        self._audit_allow(actor, action, tenant_id, rule)
        return reservation

    async def record_delivery_event(
        self,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
        event: DeliveryEventRecord,
        *,
        actor: Actor,
    ) -> bool:
        """幂等追加投递事实，并在同一事务立即执行身份/域名熔断。"""
        action = SendingIdentityAction.DELIVERY_EVENT_RECORD
        self._preauthorize(actor, action, tenant_id)
        if event.tenant_id != tenant_id:
            self._deny_tenant_isolation(actor, action, tenant_id)
        if event.identity_id != identity_id:
            raise InvalidDeliveryEventError("投递事件无效")
        now = self._now()
        async with self._uow_factory(tenant_id) as uow:
            source, identities, rule = await self._lock_reputation_domain(
                uow, tenant_id, identity_id, actor, action
            )
            if (
                event.occurred_at < source.created_at
                or event.occurred_at > now + timedelta(minutes=5)
            ):
                raise InvalidDeliveryEventError("投递事件无效")
            created = await uow.reputation.record_event(event)
            if created:
                await self._evaluate_locked(
                    uow,
                    tenant_id,
                    source,
                    identities,
                    actor=actor,
                    action=action,
                    rule=rule,
                    now=now,
                    event=event,
                )
        self._audit_allow(actor, action, tenant_id, rule)
        return created

    async def evaluate_reputation(
        self,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
        *,
        actor: Actor,
    ) -> ReputationView:
        """显式评估信誉但不隐式恢复，并返回 source identity 窗口。"""
        action = SendingIdentityAction.REPUTATION_EVALUATE
        self._preauthorize(actor, action, tenant_id)
        now = self._now()
        async with self._uow_factory(tenant_id) as uow:
            source, identities, rule = await self._lock_reputation_domain(
                uow, tenant_id, identity_id, actor, action
            )
            window = await self._evaluate_locked(
                uow,
                tenant_id,
                source,
                identities,
                actor=actor,
                action=action,
                rule=rule,
                now=now,
            )
            view = await self._build_reputation_view(window, source.thresholds)
        self._audit_allow(actor, action, tenant_id, rule)
        return view

    async def resume_from_throttle(
        self,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
        *,
        actor: Actor,
    ) -> None:
        """仅在身份和域窗口均严格低于固定 80% 线时恢复 saved state。"""
        action = SendingIdentityAction.THROTTLE_RESUME
        self._preauthorize(actor, action, tenant_id)
        now = self._now()
        async with self._uow_factory(tenant_id) as uow:
            identity, _, rule = await self._lock_reputation_domain(
                uow, tenant_id, identity_id, actor, action
            )
            if identity.state is not IdentityState.THROTTLED:
                raise InvalidStateTransition("只有限流身份可以自动恢复")
            latest = await uow.auth_checks.latest_for_identity(
                tenant_id, identity_id
            )
            if latest is None or not latest.result.all_passed:
                raise AuthenticationNotVerifiedError("认证未全部通过")
            identity_window = await uow.reputation.compute_window(
                tenant_id, identity_id, 7, now
            )
            domain_window = await uow.reputation.compute_domain_window(
                tenant_id, identity.domain, 7, now
            )
            if not (
                _resume_window_is_safe(identity_window)
                and _resume_window_is_safe(domain_window)
            ):
                raise InvalidStateTransition("信誉窗口尚未达到自动恢复条件")
            target = identity.sendable_state_before_restriction
            if target not in _SENDABLE:
                raise InvalidStateTransition("限流身份缺少可恢复状态")
            before = identity.state
            identity.transition_to(target)
            identity.suspended_at = None
            next_version = identity.version + 1
            await uow.identities.update(identity)
            await self._record_action(
                uow,
                identity,
                action=action,
                before=before,
                after=target,
                actor=actor,
                rule=rule,
                version=next_version,
                occurred_at=now,
            )
        self._audit_allow(actor, action, tenant_id, rule)

    async def resume_from_suspension(
        self,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
        investigation_note: str,
        *,
        actor: Actor,
    ) -> None:
        action = SendingIdentityAction.SUSPENSION_RESUME
        self._preauthorize(actor, action, tenant_id)
        note = _safe_text(investigation_note, field="调查记录", maximum=1000)
        now = self._now()
        async with self._uow_factory(tenant_id) as uow:
            identity, rule = await self._locked_identity(
                uow, tenant_id, identity_id, actor, action
            )
            if identity.state is not IdentityState.SUSPENDED:
                raise InvalidStateTransition("只有停用身份可以人工恢复")
            latest = await uow.auth_checks.latest_for_identity(tenant_id, identity_id)
            if latest is None or not latest.result.all_passed:
                raise AuthenticationNotVerifiedError("认证未全部通过")
            target = identity.sendable_state_before_restriction
            if target not in _SENDABLE:
                raise InvalidStateTransition("停用身份缺少可恢复状态")
            before = identity.state
            identity.transition_to(target)
            identity.suspended_at = None
            next_version = identity.version + 1
            await uow.identities.update(identity)
            await self._record_action(
                uow,
                identity,
                action=action,
                before=before,
                after=target,
                actor=actor,
                rule=rule,
                version=next_version,
                occurred_at=now,
                note=note,
            )
        self._audit_allow(actor, action, tenant_id, rule)

    async def retire(
        self,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
        reason: str,
        *,
        actor: Actor,
    ) -> None:
        action = SendingIdentityAction.IDENTITY_RETIRE
        self._preauthorize(actor, action, tenant_id)
        note = _safe_text(reason, field="退役原因", maximum=1000)
        now = self._now()
        async with self._uow_factory(tenant_id) as uow:
            identity, rule = await self._locked_identity(
                uow, tenant_id, identity_id, actor, action
            )
            if identity.state is not IdentityState.RETIRED:
                before = identity.state
                identity.transition_to(IdentityState.RETIRED)
                identity.retired_at = now
                identity.suspended_at = None
                next_version = identity.version + 1
                await uow.identities.update(identity)
                await self._record_action(
                    uow,
                    identity,
                    action=action,
                    before=before,
                    after=identity.state,
                    actor=actor,
                    rule=rule,
                    version=next_version,
                    occurred_at=now,
                    note=note,
                )
        self._audit_allow(actor, action, tenant_id, rule)

    async def _build_reputation_view(
        self,
        window: ReputationWindow,
        thresholds: ReputationThresholds,
    ) -> ReputationView:
        return ReputationView(
            window_days=window.window_days,
            sent_attempts=window.sent_attempts,
            delivered=window.delivered,
            hard_bounce_rate=window.hard_bounce_rate,
            complaint_rate=window.complaint_rate,
            delivery_rate=window.delivery_rate,
            computed_at=window.computed_at,
            spam_trap_hits=window.spam_trap_hits,
            blocklist_hits=window.blocklist_hits,
            sample_sufficient=window.sent_attempts >= thresholds.minimum_sample,
        )

    async def _build_identity_view(
        self,
        uow: SendingIdentityUnitOfWork,
        identity: SendingIdentity,
        now: datetime,
    ) -> IdentityView:
        auth_record = await uow.auth_checks.latest_for_identity(
            identity.tenant_id, identity.identity_id
        )
        auth = None
        if auth_record is not None:
            result = auth_record.result
            auth = AuthStatusView(
                checked_at=result.checked_at,
                spf_passed=result.spf_passed,
                dkim_passed=result.dkim_passed,
                dmarc_passed=result.dmarc_passed,
                failures=result.failures,
            )
        window = await uow.reputation.compute_window(
            identity.tenant_id, identity.identity_id, 7, now
        )
        reputation = await self._build_reputation_view(window, identity.thresholds)
        count = await uow.counters.get_count(
            identity.tenant_id, identity.identity_id, now.date()
        )
        warmup_day = None
        warmup_complete = False
        daily_limit = 0
        if identity.warmup_plan is not None:
            warmup_day = (now.date() - identity.warmup_plan.started_on).days + 1
            warmup_complete = identity.warmup_plan.is_complete_on(now.date())
            daily_limit = identity.warmup_plan.daily_limit_on(now.date())
        can_send = (
            identity.state in _SENDABLE
            and identity.role is DomainRole.COLD_OUTREACH
            and auth is not None
            and auth.all_passed
            and count < daily_limit
        )
        return IdentityView(
            identity_id=identity.identity_id,
            address=identity.address,
            domain=identity.domain,
            role=identity.role,
            state=identity.state,
            created_at=identity.created_at,
            auth=auth,
            reputation=reputation,
            warmup_day=warmup_day,
            warmup_complete=warmup_complete,
            target_daily_volume=(
                identity.warmup_plan.target_daily_volume
                if identity.warmup_plan is not None
                else None
            ),
            can_send_today=can_send,
            remaining_today=max(0, daily_limit - count),
            usable_for_cold_outreach=can_send,
            activated_at=identity.activated_at,
        )

    async def get(
        self,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
        *,
        actor: Actor,
    ) -> IdentityView:
        action = SendingIdentityAction.IDENTITY_READ
        self._preauthorize(actor, action, tenant_id)
        now = self._now()
        async with self._uow_factory(tenant_id) as uow:
            identity = await uow.identities.get(tenant_id, identity_id)
            if identity is None:
                raise SendingIdentityNotFoundError("发件身份不存在或不属于当前租户")
            rule = self._authorize_identity_row(identity, actor, action, tenant_id)
            view = await self._build_identity_view(uow, identity, now)
        self._audit_allow(actor, action, tenant_id, rule)
        return view

    async def list_available_for_campaign(
        self,
        tenant_id: TenantId,
        *,
        limit: int,
        actor: Actor,
    ) -> list[IdentityView]:
        action = SendingIdentityAction.IDENTITY_LIST
        self._preauthorize(actor, action, tenant_id)
        if not _is_real_int(limit) or not 1 <= limit <= 200:
            raise ValidationError("limit 必须为 1 到 200 的整数")
        now = self._now()
        async with self._uow_factory(tenant_id) as uow:
            identities = await uow.identities.list_available_for_campaign(
                tenant_id, actor.scope, limit
            )
            views: list[IdentityView] = []
            rule: str | None = None
            for identity in identities:
                row_rule = self._authorize_identity_row(
                    identity, actor, action, tenant_id
                )
                if rule is None:
                    rule = row_rule
                if identity.role is not DomainRole.COLD_OUTREACH or identity.state not in _SENDABLE:
                    raise ValidationError("发件身份查询结果不符合可用条件")
                latest = await uow.auth_checks.latest_for_identity(
                    tenant_id, identity.identity_id
                )
                if latest is None or not latest.result.all_passed:
                    raise ValidationError("发件身份查询结果不符合认证条件")
                views.append(await self._build_identity_view(uow, identity, now))
            if rule is None:
                rule = self._authorize_resource(actor, action, tenant_id)
        self._audit_allow(actor, action, tenant_id, rule)
        return views

    async def get_warmup_progress(
        self,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
        *,
        actor: Actor,
    ) -> WarmupProgressView:
        action = SendingIdentityAction.IDENTITY_READ
        self._preauthorize(actor, action, tenant_id)
        today = self._now().date()
        async with self._uow_factory(tenant_id) as uow:
            identity = await uow.identities.get(tenant_id, identity_id)
            if identity is None:
                raise SendingIdentityNotFoundError("发件身份不存在或不属于当前租户")
            rule = self._authorize_identity_row(identity, actor, action, tenant_id)
            plan = identity.warmup_plan
            if plan is None:
                raise InvalidStateTransition("发件身份尚未开始预热")
            view = WarmupProgressView(
                started_on=plan.started_on,
                day_number=(today - plan.started_on).days + 1,
                today_limit=plan.daily_limit_on(today),
                target_daily_volume=plan.target_daily_volume,
                schedule=tuple(
                    plan.daily_limit_on(plan.started_on + timedelta(days=offset))
                    for offset in range(28)
                ),
                estimated_complete_on=plan.started_on + timedelta(days=28),
                is_complete=plan.is_complete_on(today),
            )
        self._audit_allow(actor, action, tenant_id, rule)
        return view

    async def get_domain_reputation(
        self,
        tenant_id: TenantId,
        domain: str,
        *,
        actor: Actor,
    ) -> DomainReputationView:
        action = SendingIdentityAction.REPUTATION_READ
        self._preauthorize(actor, action, tenant_id)
        normalized = normalize_sending_domain(domain)
        now = self._now()
        async with self._uow_factory(tenant_id) as uow:
            domain_row = await uow.domains.get(tenant_id, normalized)
            if domain_row is None:
                raise SendingIdentityNotFoundError("发件域名不存在或不属于当前租户")
            if domain_row.tenant_id != tenant_id:
                self._deny_tenant_isolation(actor, action, tenant_id)
            identities = await uow.identities.list_domain_for_update(
                tenant_id, normalized
            )
            rule: str | None = None
            for identity in identities:
                row_rule = self._authorize_identity_row(
                    identity, actor, action, tenant_id
                )
                if rule is None:
                    rule = row_rule
            if rule is None:
                rule = self._authorize_resource(
                    actor,
                    action,
                    tenant_id,
                    domain=normalized,
                )
            window = await uow.reputation.compute_domain_window(
                tenant_id, normalized, 7, now
            )
            reputation = await self._build_reputation_view(
                window, _conservative_thresholds(identities)
            )
            controller = _domain_reputation_controller(identities)
            view = DomainReputationView(
                domain=normalized,
                role=domain_row.role,
                identity_count=len(identities),
                active_count=sum(
                    identity.state is IdentityState.ACTIVE for identity in identities
                ),
                reputation=reputation,
                at_risk=controller is not None,
                worst_identity_id=controller,
            )
        self._audit_allow(actor, action, tenant_id, rule)
        return view


def assert_service_contract(service: SendingIdentityServiceImpl) -> None:
    """让 mypy 在实现模块内证明完整 public Protocol 结构一致。"""
    public_service: SendingIdentityService = service
    assert public_service is service
