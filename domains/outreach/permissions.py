"""触达域 typed permission、两阶段授权与固定审计。"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from domains.outreach.models import SuppressionReason
from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import (
    CampaignId,
    EnrollmentId,
    MessageAttemptId,
    ProspectAccountId,
    SendingIdentityId,
    TenantId,
)

_ULID = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"


class OutreachAction(str, Enum):
    CAMPAIGN_CREATE = "campaign:create"
    CAMPAIGN_SUBMIT = "campaign:submit"
    CAMPAIGN_REVISE = "campaign:revise"
    CAMPAIGN_ACTIVATE = "campaign:activate"
    CAMPAIGN_PAUSE = "campaign:pause"
    CAMPAIGN_CANCEL = "campaign:cancel"
    CAMPAIGN_READ = "campaign:read"
    CAMPAIGN_LIST = "campaign:list"
    ENROLLMENT_CREATE = "enrollment:create"
    ENROLLMENT_PREPARE_SEND = "enrollment:prepare_send"
    ENROLLMENT_RECORD_SENT = "enrollment:record_sent"
    ENROLLMENT_RECORD_FAILURE = "enrollment:record_failure"
    ENROLLMENT_STOP = "enrollment:stop"
    ENROLLMENT_READ = "enrollment:read"
    ENROLLMENT_LIST = "enrollment:list"
    SUPPRESSION_ADD = "suppression:add"
    SUPPRESSION_READ = "suppression:read"
    SUPPRESSION_LIST = "suppression:list"
    MESSAGE_DELIVERY_BIND = "message:delivery_bind"
    DELIVERY_FEEDBACK_RESOLVE = "delivery_feedback:resolve"
    HARD_BOUNCE_APPLY = "hard_bounce:apply"
    COMPLAINT_APPLY = "complaint:apply"


class ScopeLevel(str, Enum):
    SYSTEM = "system"
    SELF = "self"
    MANAGER = "manager"
    TENANT = "tenant"


def _freeze_ids(
    value: object,
    *,
    field: str,
    prefix: str,
) -> frozenset[str] | None:
    if value is None:
        return None
    if not isinstance(value, (set, frozenset)):
        raise ValidationError(f"{field} 必须为集合")
    result = frozenset(value)
    if any(
        not isinstance(item, str)
        or re.fullmatch(rf"{prefix}_{_ULID}", item) is None
        for item in result
    ):
        raise ValidationError(f"{field} 包含无效 ID")
    return result


@dataclass(frozen=True)
class OutreachScope:
    """不可变 ABAC scope；``None`` 不限制，空集合全拒。"""

    level: ScopeLevel | None = None
    allowed_campaign_ids: frozenset[CampaignId] | None = None
    allowed_account_ids: frozenset[ProspectAccountId] | None = None
    allowed_enrollment_ids: frozenset[EnrollmentId] | None = None
    allowed_suppression_targets: frozenset[str] | None = None
    allowed_attempt_ids: frozenset[MessageAttemptId] | None = None
    allowed_sending_identity_ids: frozenset[SendingIdentityId] | None = None

    def __post_init__(self) -> None:
        if self.level is not None and not isinstance(self.level, ScopeLevel):
            raise ValidationError("触达作用域级别无效")
        object.__setattr__(
            self,
            "allowed_campaign_ids",
            _freeze_ids(
                self.allowed_campaign_ids,
                field="allowed_campaign_ids",
                prefix="cmp",
            ),
        )
        object.__setattr__(
            self,
            "allowed_account_ids",
            _freeze_ids(
                self.allowed_account_ids,
                field="allowed_account_ids",
                prefix="acc",
            ),
        )
        object.__setattr__(
            self,
            "allowed_enrollment_ids",
            _freeze_ids(
                self.allowed_enrollment_ids,
                field="allowed_enrollment_ids",
                prefix="enr",
            ),
        )
        object.__setattr__(
            self,
            "allowed_attempt_ids",
            _freeze_ids(
                self.allowed_attempt_ids,
                field="allowed_attempt_ids",
                prefix="mat",
            ),
        )
        object.__setattr__(
            self,
            "allowed_sending_identity_ids",
            _freeze_ids(
                self.allowed_sending_identity_ids,
                field="allowed_sending_identity_ids",
                prefix="sid",
            ),
        )
        targets = self.allowed_suppression_targets
        if targets is not None:
            if not isinstance(targets, (set, frozenset)):
                raise ValidationError("allowed_suppression_targets 必须为集合")
            frozen_targets = frozenset(targets)
            if any(
                not isinstance(target, str)
                or re.fullmatch(rf"(?:cp|acc)_{_ULID}", target) is None
                for target in frozen_targets
            ):
                raise ValidationError("allowed_suppression_targets 包含无效目标")
            object.__setattr__(
                self, "allowed_suppression_targets", frozen_targets
            )
        if self.level is ScopeLevel.MANAGER and (
            self.allowed_campaign_ids is None and self.allowed_account_ids is None
        ):
            raise ValidationError("MANAGER 作用域必须收窄 campaign 或 account")
        if self.level is ScopeLevel.SELF and (
            self.allowed_campaign_ids is None
            and self.allowed_enrollment_ids is None
        ):
            raise ValidationError("SELF 作用域必须携带 ownership")
        if self.level is ScopeLevel.SYSTEM:
            enrollment_count = (
                len(self.allowed_enrollment_ids)
                if self.allowed_enrollment_ids is not None
                else 0
            )
            target_count = (
                len(self.allowed_suppression_targets)
                if self.allowed_suppression_targets is not None
                else 0
            )
            attempt_count = (
                len(self.allowed_attempt_ids)
                if self.allowed_attempt_ids is not None
                else 0
            )
            identity_count = (
                len(self.allowed_sending_identity_ids)
                if self.allowed_sending_identity_ids is not None
                else 0
            )
            if (
                enrollment_count + target_count + attempt_count + identity_count
                != 1
                or self.allowed_campaign_ids is not None
                or self.allowed_account_ids is not None
            ):
                raise ValidationError("SYSTEM 作用域必须收窄精确单一资源")

    @property
    def label(self) -> str:
        return self.level.value if self.level is not None else "unprivileged"


@dataclass(frozen=True)
class Actor:
    actor_id: str
    scope: OutreachScope
    role: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.actor_id, str)
            or not 1 <= len(self.actor_id) <= 64
            or any(character.isspace() for character in self.actor_id)
        ):
            raise ValidationError("actor_id 格式无效")
        if not isinstance(self.scope, OutreachScope):
            raise ValidationError("actor scope 无效")


@runtime_checkable
class OutreachAuthorizer(Protocol):
    def preauthorize(
        self,
        actor: Actor,
        action: OutreachAction,
        scope: OutreachScope,
        tenant_id: TenantId,
    ) -> str: ...

    def require(
        self,
        actor: Actor,
        action: OutreachAction,
        scope: OutreachScope,
        tenant_id: TenantId,
        *,
        campaign_id: CampaignId | None = None,
        account_id: ProspectAccountId | None = None,
        enrollment_id: EnrollmentId | None = None,
        suppression_target: str | None = None,
        suppression_reason: SuppressionReason | None = None,
        attempt_id: MessageAttemptId | None = None,
        sending_identity_id: SendingIdentityId | None = None,
    ) -> str: ...


@runtime_checkable
class AuditLogger(Protocol):
    def log(
        self,
        *,
        actor: str,
        action: str,
        tenant_id: TenantId,
        scope: str,
        rule: str,
    ) -> None: ...


class StandardAuditLogger:
    def __init__(self, logger_name: str = "security.authorization") -> None:
        self._logger = logging.getLogger(logger_name)

    def log(
        self,
        *,
        actor: str,
        action: str,
        tenant_id: TenantId,
        scope: str,
        rule: str,
    ) -> None:
        self._logger.info(
            "授权审计",
            extra={
                "actor": actor,
                "action": action,
                "tenant_id": str(tenant_id),
                "scope": scope,
                "rule": rule,
            },
        )


_BOSS_ACTIONS = frozenset(
    {
        OutreachAction.CAMPAIGN_CREATE,
        OutreachAction.CAMPAIGN_SUBMIT,
        OutreachAction.CAMPAIGN_REVISE,
        OutreachAction.CAMPAIGN_ACTIVATE,
        OutreachAction.CAMPAIGN_PAUSE,
        OutreachAction.CAMPAIGN_CANCEL,
        OutreachAction.CAMPAIGN_READ,
        OutreachAction.CAMPAIGN_LIST,
        OutreachAction.ENROLLMENT_CREATE,
        OutreachAction.ENROLLMENT_STOP,
        OutreachAction.ENROLLMENT_READ,
        OutreachAction.ENROLLMENT_LIST,
        OutreachAction.SUPPRESSION_ADD,
        OutreachAction.SUPPRESSION_READ,
        OutreachAction.SUPPRESSION_LIST,
    }
)
_MANAGER_ACTIONS = frozenset(
    {
        OutreachAction.CAMPAIGN_SUBMIT,
        OutreachAction.CAMPAIGN_REVISE,
        OutreachAction.CAMPAIGN_PAUSE,
        OutreachAction.CAMPAIGN_CANCEL,
        OutreachAction.CAMPAIGN_READ,
        OutreachAction.CAMPAIGN_LIST,
        OutreachAction.ENROLLMENT_CREATE,
        OutreachAction.ENROLLMENT_STOP,
        OutreachAction.ENROLLMENT_READ,
        OutreachAction.ENROLLMENT_LIST,
        OutreachAction.SUPPRESSION_READ,
        OutreachAction.SUPPRESSION_LIST,
    }
)
_SYSTEM_ACTIONS = frozenset(
    {
        OutreachAction.ENROLLMENT_PREPARE_SEND,
        OutreachAction.ENROLLMENT_RECORD_SENT,
        OutreachAction.ENROLLMENT_RECORD_FAILURE,
        OutreachAction.ENROLLMENT_STOP,
        OutreachAction.ENROLLMENT_READ,
        OutreachAction.SUPPRESSION_ADD,
        OutreachAction.SUPPRESSION_READ,
        OutreachAction.MESSAGE_DELIVERY_BIND,
        OutreachAction.DELIVERY_FEEDBACK_RESOLVE,
        OutreachAction.HARD_BOUNCE_APPLY,
        OutreachAction.COMPLAINT_APPLY,
    }
)
_SALES_ACTIONS = frozenset(
    {
        OutreachAction.CAMPAIGN_READ,
        OutreachAction.CAMPAIGN_LIST,
        OutreachAction.ENROLLMENT_READ,
        OutreachAction.ENROLLMENT_LIST,
    }
)
_AUTOMATIC_SUPPRESSION_REASONS = frozenset(
    {
        SuppressionReason.UNSUBSCRIBE,
        SuppressionReason.COMPLAINT,
        SuppressionReason.HARD_BOUNCE,
    }
)


class Phase1OutreachAuthorizer:
    """Phase 1 tenant-bound allow-list；未知输入一律拒绝。"""

    def __init__(self, tenant_id: TenantId) -> None:
        self._tenant_id = tenant_id

    def _eligible_level(
        self,
        actor: Actor,
        action: OutreachAction,
        scope: OutreachScope,
        tenant_id: TenantId,
    ) -> ScopeLevel:
        matrix = {
            ("boss", ScopeLevel.TENANT): _BOSS_ACTIONS,
            ("manager", ScopeLevel.MANAGER): _MANAGER_ACTIONS,
            ("system", ScopeLevel.SYSTEM): _SYSTEM_ACTIONS,
            ("sales", ScopeLevel.SELF): _SALES_ACTIONS,
        }
        if (
            not isinstance(actor, Actor)
            or not isinstance(action, OutreachAction)
            or not isinstance(scope, OutreachScope)
            or not isinstance(actor.role, str)
        ):
            raise PermissionDenied("Phase 1 触达授权拒绝")
        level = scope.level
        configured_sets = (
            scope.allowed_campaign_ids,
            scope.allowed_account_ids,
            scope.allowed_enrollment_ids,
            scope.allowed_suppression_targets,
            scope.allowed_attempt_ids,
            scope.allowed_sending_identity_ids,
        )
        if (
            tenant_id != self._tenant_id
            or scope is not actor.scope
            or level is None
            or action not in matrix.get((actor.role, level), frozenset())
            or any(value == frozenset() for value in configured_sets)
        ):
            raise PermissionDenied("Phase 1 触达授权拒绝")
        if level is ScopeLevel.SYSTEM:
            expected_resource = {
                OutreachAction.SUPPRESSION_ADD: "suppression",
                OutreachAction.SUPPRESSION_READ: "suppression",
                OutreachAction.MESSAGE_DELIVERY_BIND: "attempt",
                OutreachAction.DELIVERY_FEEDBACK_RESOLVE: "identity",
                OutreachAction.HARD_BOUNCE_APPLY: "identity",
                OutreachAction.COMPLAINT_APPLY: "identity",
            }.get(action, "enrollment")
            actual_resource = (
                "suppression"
                if scope.allowed_suppression_targets is not None
                else "attempt"
                if scope.allowed_attempt_ids is not None
                else "identity"
                if scope.allowed_sending_identity_ids is not None
                else "enrollment"
            )
            if expected_resource != actual_resource:
                raise PermissionDenied("Phase 1 触达授权拒绝")
        return level

    def preauthorize(
        self,
        actor: Actor,
        action: OutreachAction,
        scope: OutreachScope,
        tenant_id: TenantId,
    ) -> str:
        level = self._eligible_level(actor, action, scope, tenant_id)
        return f"phase1:preauthorize:{actor.role}:{level.value}:{action.value}"

    @staticmethod
    def _require_member(
        allowed: frozenset[str] | None,
        actual: str | None,
    ) -> None:
        if allowed is not None and (actual is None or actual not in allowed):
            raise PermissionDenied("Phase 1 触达授权拒绝")

    def require(
        self,
        actor: Actor,
        action: OutreachAction,
        scope: OutreachScope,
        tenant_id: TenantId,
        *,
        campaign_id: CampaignId | None = None,
        account_id: ProspectAccountId | None = None,
        enrollment_id: EnrollmentId | None = None,
        suppression_target: str | None = None,
        suppression_reason: SuppressionReason | None = None,
        attempt_id: MessageAttemptId | None = None,
        sending_identity_id: SendingIdentityId | None = None,
    ) -> str:
        level = self._eligible_level(actor, action, scope, tenant_id)
        if level in {ScopeLevel.MANAGER, ScopeLevel.SELF}:
            if action.value.startswith("campaign:"):
                self._require_member(scope.allowed_campaign_ids, campaign_id)
            elif action.value.startswith("enrollment:"):
                self._require_member(scope.allowed_campaign_ids, campaign_id)
                self._require_member(scope.allowed_account_ids, account_id)
                self._require_member(scope.allowed_enrollment_ids, enrollment_id)
            elif action.value.startswith("suppression:"):
                self._require_member(
                    scope.allowed_suppression_targets, suppression_target
                )
        if level is ScopeLevel.SYSTEM:
            if scope.allowed_enrollment_ids is not None:
                self._require_member(scope.allowed_enrollment_ids, enrollment_id)
            if scope.allowed_suppression_targets is not None:
                self._require_member(
                    scope.allowed_suppression_targets, suppression_target
                )
            if scope.allowed_attempt_ids is not None:
                self._require_member(scope.allowed_attempt_ids, attempt_id)
            if scope.allowed_sending_identity_ids is not None:
                self._require_member(
                    scope.allowed_sending_identity_ids, sending_identity_id
                )
            if action is OutreachAction.SUPPRESSION_ADD and (
                not isinstance(suppression_reason, SuppressionReason)
                or suppression_reason not in _AUTOMATIC_SUPPRESSION_REASONS
            ):
                raise PermissionDenied("Phase 1 触达授权拒绝")
        return f"phase1:{actor.role}:{level.value}:{action.value}"


class DefaultDenyAuthorizer:
    def preauthorize(
        self,
        actor: Actor,
        action: OutreachAction,
        scope: OutreachScope,
        tenant_id: TenantId,
    ) -> str:
        raise PermissionDenied("Phase 1 触达授权拒绝")

    def require(
        self,
        actor: Actor,
        action: OutreachAction,
        scope: OutreachScope,
        tenant_id: TenantId,
        *,
        campaign_id: CampaignId | None = None,
        account_id: ProspectAccountId | None = None,
        enrollment_id: EnrollmentId | None = None,
        suppression_target: str | None = None,
        suppression_reason: SuppressionReason | None = None,
        attempt_id: MessageAttemptId | None = None,
        sending_identity_id: SendingIdentityId | None = None,
    ) -> str:
        raise PermissionDenied("Phase 1 触达授权拒绝")
