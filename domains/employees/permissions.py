"""员工域权限契约与默认拒绝基座。

- ``EmployeeAction``：本域全部操作的 typed action（未知 action 一律默认拒绝）。
- ``EmployeeScope``：ABAC 作用域（SYSTEM/MANAGER/SELF/TENANT）。
- ``Actor``：操作身份（角色由员工域推导，绝不信任 header）。
- ``EmployeeAuthorizer``：判权接口。``require`` 放行返回所用规则标识，
  拒绝抛 ``shared.errors.PermissionDenied``。
- ``AuditLogger``：授权审计接口，仅记录 actor/action/tenant/scope/rule，
  **不记录任何敏感值**（无账户/锁/客户内容）。
- ``DefaultDenyAuthorizer``：默认拒绝基座——没有明确放行规则就拒绝。
  真实 wiring 在子类中逐项放行；这是安全底线，不是完整授权策略。

不 import 其他 domains/*；只依赖 shared.*。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from shared.errors import PermissionDenied
from shared.schemas.identifiers import ProspectAccountId, TenantId


class EmployeeAction(str, Enum):
    """员工域操作（typed）。新增操作必须在此登记，否则一律默认拒绝。"""

    OWNERSHIP_READ = "ownership:read"
    NOTIFICATION_OWNER_READ = "ownership:notification_owner_read"
    OWNERSHIP_LOCK = "ownership:lock"
    OWNERSHIP_TRANSFER = "ownership:transfer"
    TERRITORY_APPLY = "territory:apply"
    EMPLOYEE_READ = "employee:read"
    EMPLOYEE_LIST = "employee:list"
    ASSIGNMENT_LIST = "assignment:list"


class EmployeeScope(str, Enum):
    """ABAC 作用域。最小权限：worker/system 只做系统动作。"""

    SYSTEM = "system"  # worker/system actor：最小权限
    MANAGER = "manager"  # 经理：可分配/转移
    SELF = "self"  # 员工本人：只读
    TENANT = "tenant"  # 租户级：管理员/老板


@dataclass(frozen=True)
class Actor:
    """操作身份。``scope`` **必须显式传**（无默认，避免默认成最高权限）；
    ``role`` 由员工域从员工记录推导，绝不信任请求头。"""

    actor_id: str
    scope: EmployeeScope
    role: str | None = None
    notification_account_id: ProspectAccountId | None = None


@runtime_checkable
class EmployeeAuthorizer(Protocol):
    def require(
        self,
        actor: Actor,
        action: EmployeeAction,
        scope: EmployeeScope,
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
        """授权审计：仅 actor/action/tenant/scope/rule，无敏感值。"""
        ...


class StandardAuditLogger:
    """仅记录授权白名单字段的结构化审计实现。"""

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


_EMPLOYEE_SYSTEM_ACTIONS = frozenset(
    {
        EmployeeAction.EMPLOYEE_READ,
        EmployeeAction.EMPLOYEE_LIST,
        EmployeeAction.NOTIFICATION_OWNER_READ,
    }
)
_EMPLOYEE_BOSS_ACTIONS = frozenset(
    {
        EmployeeAction.OWNERSHIP_READ,
        EmployeeAction.OWNERSHIP_LOCK,
        EmployeeAction.OWNERSHIP_TRANSFER,
        EmployeeAction.TERRITORY_APPLY,
        EmployeeAction.EMPLOYEE_READ,
        EmployeeAction.EMPLOYEE_LIST,
        EmployeeAction.ASSIGNMENT_LIST,
    }
)


class Phase1EmployeeAuthorizer:
    """Phase 1 的 tenant-bound 固定员工授权矩阵。"""

    def __init__(self, tenant_id: TenantId) -> None:
        self._tenant_id = tenant_id

    def require(
        self,
        actor: Actor,
        action: EmployeeAction,
        scope: EmployeeScope,
        tenant_id: TenantId,
    ) -> str:
        allowed: dict[tuple[str | None, EmployeeScope], frozenset[EmployeeAction]] = {
            ("system", EmployeeScope.SYSTEM): _EMPLOYEE_SYSTEM_ACTIONS,
            ("boss", EmployeeScope.TENANT): _EMPLOYEE_BOSS_ACTIONS,
        }
        if (
            not isinstance(action, EmployeeAction)
            or not isinstance(scope, EmployeeScope)
            or tenant_id != self._tenant_id
            or scope is not actor.scope
            or not isinstance(actor.actor_id, str)
            or not actor.actor_id.strip()
            or action not in allowed.get((actor.role, scope), frozenset())
        ):
            raise PermissionDenied("Phase 1 员工授权拒绝")
        if action is EmployeeAction.NOTIFICATION_OWNER_READ and (
            not isinstance(actor.notification_account_id, str)
            or re.fullmatch(
                r"acc_[0-7][0-9A-HJKMNP-TV-Z]{25}", actor.notification_account_id
            )
            is None
        ):
            raise PermissionDenied("通知受众必须限定单一账户")
        return f"phase1:{actor.role}:{scope.value}:{action.value}"


class DefaultDenyAuthorizer:
    """默认拒绝基座：未知 action/scope 一律 ``PermissionDenied``。

    真实 wiring 在此基座上按角色逐项放行。没有明确放行规则 → 拒绝，
    这是安全底线（fail closed）。
    """

    def require(
        self,
        actor: Actor,
        action: EmployeeAction,
        scope: EmployeeScope,
        tenant_id: TenantId,
    ) -> str:
        if not isinstance(action, EmployeeAction):
            raise PermissionDenied(f"未知 action: {action}")
        if not isinstance(scope, EmployeeScope):
            raise PermissionDenied(f"未知 scope: {scope}")
        raise PermissionDenied(f"默认拒绝: {action.value}")
