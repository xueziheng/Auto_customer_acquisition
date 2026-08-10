"""机会域权限契约与默认拒绝基座（**独立契约，不 import 员工域**）。

- ``OpportunityAction``：本域全部操作的 typed action（新增操作必须在此登记，
  否则一律默认拒绝）。
- ``ScopeLevel``：作用域级别（SYSTEM/SELF/MANAGER/TENANT）。
- ``OpportunityScope``：ABAC 作用域（不可变、无最高权限默认）。除级别外携带
  ``allowed_owners`` / ``allowed_countries`` / ``allowed_categories`` 三个维度
  限制，供查询层翻译成 tenant-filtered SQL（S3-13）。``None`` = 该维度不限制，
  空 ``frozenset`` = 该维度任何值都不授权。``level`` 默认 ``None`` = 无授权。
- ``Actor``：操作身份。``scope`` **必须显式传**（无默认，避免默认成最高权限）；
  ``role`` 由上层从员工记录推导，绝不信任请求头（本域不 import 员工域）。
- ``OpportunityAuthorizer``：判权接口。``require(actor, action, scope, tenant_id)``
  放行返回所用规则标识，拒绝抛 ``shared.errors.PermissionDenied``。
- ``AuditLogger`` / ``StandardAuditLogger``：授权审计，仅记录 actor/action/tenant_id/
  scope/rule，**不记录任何敏感值**（无账户/锁/客户内容/业务 payload）。

不 import 其他 domains/*；只依赖 shared.*。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import EmployeeId, TenantId


class OpportunityAction(str, Enum):
    """机会域操作（typed）。新增操作必须在此登记，否则一律默认拒绝。"""

    OPPORTUNITY_CREATE = "opportunity:create"
    OPPORTUNITY_ASSIGN = "opportunity:assign"
    OPPORTUNITY_TRANSITION = "opportunity:transition"
    OPPORTUNITY_MARK_LOST = "opportunity:mark_lost"
    OPPORTUNITY_MARK_WON = "opportunity:mark_won"
    OPPORTUNITY_READ = "opportunity:read"
    OPPORTUNITY_LIST = "opportunity:list"
    HANDOFF_REQUEST = "handoff:request"
    HANDOFF_ESCALATION_RECORD = "handoff:escalation_record"
    HANDOFF_ACCEPT = "handoff:accept"
    HANDOFF_READ = "handoff:read"
    HANDOFF_QUEUE_READ = "handoff:queue_read"
    LOSS_REASON_READ = "loss_reason:read"


class ScopeLevel(str, Enum):
    """作用域级别。worker/system 只用 SYSTEM（最小权限）。"""

    SYSTEM = "system"
    SELF = "self"
    MANAGER = "manager"
    TENANT = "tenant"


@dataclass(frozen=True)
class OpportunityScope:
    """ABAC 作用域（不可变、无最高权限默认）。

    ``level`` 默认 ``None`` = 无任何授权（默认拒绝的最低值）。``allowed_*``
    三个维度是 ABAC 限制：``None`` 表示该维度不限制；空 ``frozenset`` 表示该维度
    任何值都不授权（查询级拒绝）。供 S3-13 翻译成 tenant-filtered SQL WHERE。
    所有字段均不可变（frozen dataclass + frozenset），无可变默认值。

    构造校验（fail closed，防"忘了加过滤就变成租户级"）：
    - ``SELF`` 必须带非空 ``allowed_owners``（own-owner 限制）；
    - ``MANAGER`` 必须带至少一个显式 ABAC 维度（owner/country/category），
      即使选中的集合故意为空（= 该维度全拒）；
    - ``TENANT`` 允许显式无限制；``SYSTEM`` 判权仍由 authorizer 控制。
    """

    level: ScopeLevel | None = None
    allowed_owners: frozenset[EmployeeId] | None = None
    allowed_countries: frozenset[str] | None = None
    allowed_categories: frozenset[str] | None = None

    def __post_init__(self) -> None:
        if self.level is ScopeLevel.SELF and not self.allowed_owners:
            raise ValidationError(
                "SELF 作用域必须带非空 allowed_owners（own-owner 限制，fail closed）"
            )
        if self.level is ScopeLevel.MANAGER and (
            self.allowed_owners is None
            and self.allowed_countries is None
            and self.allowed_categories is None
        ):
            raise ValidationError(
                "MANAGER 作用域必须带至少一个显式 ABAC 维度（owner/country/category）"
            )

    @property
    def label(self) -> str:
        """审计用的作用域标签：级别名；无级别时 ``unprivileged``。"""
        return self.level.value if self.level is not None else "unprivileged"


@dataclass(frozen=True)
class Actor:
    """操作身份。``scope`` **必须显式传**（无默认，避免默认成最高权限）；
    ``role`` 由上层从员工记录推导，绝不信任请求头。

    构造校验（fail closed）：``SELF`` actor 的 ``allowed_owners`` 必须**精确等于**
    ``frozenset({EmployeeId(actor_id)})`` 单例——SELF 语义是"只看自己的机会"，
    多带任何其他 owner（即使同时包含自身）都构成越权身份，构造即拒绝。
    """

    actor_id: str
    scope: OpportunityScope
    role: str | None = None

    def __post_init__(self) -> None:
        if (
            self.scope.level is ScopeLevel.SELF
            and self.scope.allowed_owners != frozenset({EmployeeId(self.actor_id)})
        ):
            raise ValidationError(
                "SELF actor 的 allowed_owners 必须精确等于 {actor_id} 单例"
                "（fail closed，杜绝附带他人 owner）"
            )


@runtime_checkable
class OpportunityAuthorizer(Protocol):
    def require(
        self,
        actor: Actor,
        action: OpportunityAction,
        scope: OpportunityScope,
        tenant_id: TenantId,
    ) -> str:
        """判权：放行返回所用规则标识；拒绝抛 ``PermissionDenied``。

        所有公开读写都必须先经过本方法（默认拒绝未知 action）。
        """
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
        """授权审计：仅 actor/action/tenant_id/scope/rule，无敏感值。"""
        ...


class StandardAuditLogger:
    """安全授权审计实现：结构化记录 actor/action/tenant_id/scope/rule。

    用标准 logging（logger 名 ``security.authorization``），字段经 ``extra``
    进 LogRecord，消息体固定为 ``授权审计``（不含任何业务内容）。
    与 ``infra/db/base.py`` 的审计日志同风格。
    """

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


_OPPORTUNITY_STAFF_ACTIONS = frozenset(
    {
        OpportunityAction.OPPORTUNITY_READ,
        OpportunityAction.OPPORTUNITY_LIST,
        OpportunityAction.OPPORTUNITY_TRANSITION,
        OpportunityAction.OPPORTUNITY_MARK_LOST,
        OpportunityAction.HANDOFF_READ,
        OpportunityAction.HANDOFF_QUEUE_READ,
        OpportunityAction.HANDOFF_ACCEPT,
    }
)
_OPPORTUNITY_BOSS_ACTIONS = _OPPORTUNITY_STAFF_ACTIONS | frozenset(
    {
        OpportunityAction.OPPORTUNITY_CREATE,
        OpportunityAction.OPPORTUNITY_ASSIGN,
        OpportunityAction.HANDOFF_REQUEST,
        OpportunityAction.LOSS_REASON_READ,
    }
)


class Phase1OpportunityAuthorizer:
    """Phase 1 的 tenant-bound 固定授权矩阵；未列组合一律拒绝。"""

    def __init__(self, tenant_id: TenantId) -> None:
        self._tenant_id = tenant_id

    def require(
        self,
        actor: Actor,
        action: OpportunityAction,
        scope: OpportunityScope,
        tenant_id: TenantId,
    ) -> str:
        allowed: dict[
            tuple[str | None, ScopeLevel | None], frozenset[OpportunityAction]
        ] = {
            ("sales", ScopeLevel.SELF): _OPPORTUNITY_STAFF_ACTIONS,
            ("manager", ScopeLevel.MANAGER): _OPPORTUNITY_STAFF_ACTIONS,
            ("boss", ScopeLevel.TENANT): _OPPORTUNITY_BOSS_ACTIONS,
            ("system", ScopeLevel.SYSTEM): frozenset(
                {
                    OpportunityAction.HANDOFF_REQUEST,
                    OpportunityAction.HANDOFF_ESCALATION_RECORD,
                }
            ),
        }
        if not isinstance(action, OpportunityAction) or not isinstance(
            scope, OpportunityScope
        ):
            raise PermissionDenied("Phase 1 机会授权拒绝")
        level = scope.level
        key = (actor.role, level)
        if (
            tenant_id != self._tenant_id
            or scope != actor.scope
            or not isinstance(actor.actor_id, str)
            or not actor.actor_id.strip()
            or level is None
            or action not in allowed.get(key, frozenset())
        ):
            raise PermissionDenied("Phase 1 机会授权拒绝")
        return f"phase1:{actor.role}:{level.value}:{action.value}"


class DefaultDenyAuthorizer:
    """默认拒绝基座：未知 action/scope 一律 ``PermissionDenied``。

    真实 wiring 在此基座上按角色逐项放行。没有明确放行规则 → 拒绝，
    这是安全底线（fail closed）。最小权限：worker/system 只做系统动作。
    """

    def require(
        self,
        actor: Actor,
        action: OpportunityAction,
        scope: OpportunityScope,
        tenant_id: TenantId,
    ) -> str:
        if not isinstance(action, OpportunityAction):
            raise PermissionDenied(f"未知 action: {action}")
        if not isinstance(scope, OpportunityScope):
            raise PermissionDenied(f"未知 scope: {scope}")
        raise PermissionDenied(f"默认拒绝: {action.value}")
