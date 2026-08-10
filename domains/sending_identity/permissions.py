"""发件身份域的 typed permissions 与默认拒绝授权矩阵。

本模块不 import 员工域；上层负责从员工记录推导角色，再将最小 scope 显式
传入。审计只记录固定五字段，避免地址、域名和连接器引用泄漏。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from domains.sending_identity.models import normalize_sending_domain
from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import SendingIdentityId, TenantId


class SendingIdentityAction(str, Enum):
    IDENTITY_REGISTER = "identity:register"
    AUTH_CHECK_BEGIN = "auth:check_begin"
    AUTH_RESULT_RECORD = "auth:result_record"
    WARMUP_START = "warmup:start"
    WARMUP_ADVANCE = "warmup:advance"
    IDENTITY_READ = "identity:read"
    IDENTITY_LIST = "identity:list"
    REPUTATION_READ = "reputation:read"
    SEND_PERMISSION_READ = "send_permission:read"
    SEND_SLOT_RESERVE = "send_slot:reserve"
    DELIVERY_EVENT_RECORD = "delivery_event:record"
    REPUTATION_EVALUATE = "reputation:evaluate"
    THROTTLE_RESUME = "throttle:resume"
    SUSPENSION_RESUME = "suspension:resume"
    IDENTITY_RETIRE = "identity:retire"


class ScopeLevel(str, Enum):
    SYSTEM = "system"
    SELF = "self"
    MANAGER = "manager"
    TENANT = "tenant"


@dataclass(frozen=True)
class SendingIdentityScope:
    """不可变 ABAC scope；``None`` 维度无限制，空集合表示全拒。"""

    level: ScopeLevel | None = None
    allowed_identity_ids: frozenset[SendingIdentityId] | None = None
    allowed_domains: frozenset[str] | None = None

    def __post_init__(self) -> None:
        if self.level is not None and not isinstance(self.level, ScopeLevel):
            raise ValidationError("发件身份作用域级别无效")
        if self.allowed_domains is not None:
            for domain in self.allowed_domains:
                if not isinstance(domain, str) or normalize_sending_domain(domain) != domain:
                    raise ValidationError("allowed_domains 必须为已规范化域名")
        if self.level is ScopeLevel.MANAGER and self.allowed_identity_ids is None and self.allowed_domains is None:
            raise ValidationError("MANAGER 作用域必须显式收窄 identity 或 domain")
        if self.level is ScopeLevel.SYSTEM and not self.allowed_identity_ids:
            raise ValidationError("SYSTEM 作用域必须显式收窄 identity")

    @property
    def label(self) -> str:
        return self.level.value if self.level is not None else "unprivileged"


@dataclass(frozen=True)
class Actor:
    """经上层验证的操作身份；scope 必须显式提供且 actor ID 不允许空白。"""

    actor_id: str
    scope: SendingIdentityScope
    role: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.actor_id, str)
            or not 1 <= len(self.actor_id) <= 64
            or any(character.isspace() for character in self.actor_id)
        ):
            raise ValidationError("actor_id 格式无效")
        if not isinstance(self.scope, SendingIdentityScope):
            raise ValidationError("actor scope 无效")


@runtime_checkable
class SendingIdentityAuthorizer(Protocol):
    def require(
        self,
        actor: Actor,
        action: SendingIdentityAction,
        scope: SendingIdentityScope,
        tenant_id: TenantId,
        *,
        identity_id: SendingIdentityId | None = None,
    ) -> str:
        """放行返回稳定 rule；未列角色/动作/范围一律抛 ``PermissionDenied``。"""
        ...


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
    ) -> None:
        """仅记录 actor/action/tenant_id/scope/rule 五个安全字段。"""
        ...


class StandardAuditLogger:
    """标准 logging 审计实现，消息固定为中文 ``授权审计``。"""

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
        SendingIdentityAction.IDENTITY_REGISTER,
        SendingIdentityAction.AUTH_CHECK_BEGIN,
        SendingIdentityAction.WARMUP_START,
        SendingIdentityAction.IDENTITY_READ,
        SendingIdentityAction.IDENTITY_LIST,
        SendingIdentityAction.REPUTATION_READ,
        SendingIdentityAction.SEND_PERMISSION_READ,
        SendingIdentityAction.SUSPENSION_RESUME,
        SendingIdentityAction.IDENTITY_RETIRE,
    }
)
_MANAGER_ACTIONS = frozenset(
    {
        SendingIdentityAction.IDENTITY_READ,
        SendingIdentityAction.IDENTITY_LIST,
        SendingIdentityAction.REPUTATION_READ,
        SendingIdentityAction.SEND_PERMISSION_READ,
    }
)
_SYSTEM_ACTIONS = frozenset(
    {
        SendingIdentityAction.AUTH_RESULT_RECORD,
        SendingIdentityAction.WARMUP_ADVANCE,
        SendingIdentityAction.IDENTITY_READ,
        SendingIdentityAction.REPUTATION_READ,
        SendingIdentityAction.SEND_PERMISSION_READ,
        SendingIdentityAction.SEND_SLOT_RESERVE,
        SendingIdentityAction.DELIVERY_EVENT_RECORD,
        SendingIdentityAction.REPUTATION_EVALUATE,
        SendingIdentityAction.THROTTLE_RESUME,
    }
)


class Phase1SendingIdentityAuthorizer:
    """Phase 1 tenant-bound allow-list；未知输入和 scope 不一致均 fail closed。"""

    def __init__(self, tenant_id: TenantId) -> None:
        self._tenant_id = tenant_id

    def require(
        self,
        actor: Actor,
        action: SendingIdentityAction,
        scope: SendingIdentityScope,
        tenant_id: TenantId,
        *,
        identity_id: SendingIdentityId | None = None,
    ) -> str:
        allowed: dict[tuple[str | None, ScopeLevel], frozenset[SendingIdentityAction]] = {
            ("boss", ScopeLevel.TENANT): _BOSS_ACTIONS,
            ("manager", ScopeLevel.MANAGER): _MANAGER_ACTIONS,
            ("system", ScopeLevel.SYSTEM): _SYSTEM_ACTIONS,
            ("sales", ScopeLevel.SELF): frozenset(),
        }
        if not isinstance(actor, Actor) or not isinstance(action, SendingIdentityAction) or not isinstance(scope, SendingIdentityScope):
            raise PermissionDenied("Phase 1 发件身份授权拒绝")
        level = scope.level
        if (
            tenant_id != self._tenant_id
            or scope != actor.scope
            or level is None
            or action not in allowed.get((actor.role, level), frozenset())
        ):
            raise PermissionDenied("Phase 1 发件身份授权拒绝")
        if level is ScopeLevel.SYSTEM and (
            identity_id is None or scope.allowed_identity_ids != frozenset({identity_id})
        ):
            raise PermissionDenied("Phase 1 发件身份授权拒绝")
        if (
            level is ScopeLevel.MANAGER
            and identity_id is not None
            and scope.allowed_identity_ids is not None
            and identity_id not in scope.allowed_identity_ids
        ):
            raise PermissionDenied("Phase 1 发件身份授权拒绝")
        return f"phase1:{actor.role}:{level.value}:{action.value}"


class DefaultDenyAuthorizer:
    """未被具体策略接管时的安全默认值。"""

    def require(
        self,
        actor: Actor,
        action: SendingIdentityAction,
        scope: SendingIdentityScope,
        tenant_id: TenantId,
        *,
        identity_id: SendingIdentityId | None = None,
    ) -> str:
        raise PermissionDenied("Phase 1 发件身份授权拒绝")
