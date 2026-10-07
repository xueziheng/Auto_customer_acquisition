"""成本域权限契约与 Phase 1 默认拒绝矩阵。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import EmployeeId, TenantId


class CostingAction(str, Enum):
    """成本域当前开放的安全动作。"""

    SHEET_READ = "cost_sheet:read"
    SHEET_CREATE = "cost_sheet:create"
    ITEM_ADD = "cost_item:add"
    QUOTE_READINESS_ASSESS = "quote_readiness:assess"
    POLICY_CONFIRM = "pricing_policy:confirm"
    EVIDENCE_CONFIRM = "pricing_evidence:confirm"
    COVERAGE_CONFIRM = "cost_coverage:confirm"
    QUOTE_FX_CONFIRM = "quote_fx:confirm"
    SCOPE_CONFIRM = "cost_scope:confirm"
    QUOTE_CALCULATE = "quote:calculate"
    QUOTE_FREEZE = "quote:freeze"
    QUOTE_OPERATION_READ = "quote_operation:read"
    QUOTE_OPERATION_COMPLETE = "quote_operation:complete"
    SOURCING_ESTIMATE_CREATE = "sourcing_estimate:create"


class CostingScope(str, Enum):
    """Phase 1 成本数据只开放显式租户级后台角色。"""

    UNPRIVILEGED = "unprivileged"
    TENANT = "tenant"
    SYSTEM = "system"


@dataclass(frozen=True)
class CostingActor:
    """由员工公共身份确定性派生，绝不从请求体读取。"""

    actor_id: str
    role: str
    scope: CostingScope
    tenant_id: TenantId | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.actor_id, str) or not self.actor_id.strip():
            raise ValidationError("成本操作身份无效")
        if not isinstance(self.role, str) or not self.role.strip():
            raise ValidationError("成本操作角色无效")
        if not isinstance(self.scope, CostingScope):
            raise ValidationError("成本操作范围无效")
        if (self.role == "system") != (self.scope is CostingScope.SYSTEM):
            raise ValidationError("system 角色与 SYSTEM 范围必须成对")
        if self.scope is CostingScope.SYSTEM and (
            not isinstance(self.tenant_id, str) or not self.tenant_id.strip()
        ):
            raise ValidationError("SYSTEM 成本身份必须绑定租户")


@runtime_checkable
class CostingAuthorizer(Protocol):
    def require(
        self,
        actor: CostingActor,
        action: CostingAction,
        tenant_id: TenantId,
    ) -> str: ...


class Phase1CostingAuthorizer:
    """按 Product 内部视图已有角色边界开放人工成本操作。"""

    _ALLOWED_ROLES = frozenset({"boss", "product", "sourcing", "finance"})

    def __init__(self, tenant_id: TenantId) -> None:
        self._tenant_id = tenant_id

    def require(
        self,
        actor: CostingActor,
        action: CostingAction,
        tenant_id: TenantId,
    ) -> str:
        if (
            tenant_id != self._tenant_id
            or not isinstance(action, CostingAction)
            or (
                action is CostingAction.SOURCING_ESTIMATE_CREATE
                and not (
                    actor.scope is CostingScope.SYSTEM
                    and actor.role == "system"
                    and actor.tenant_id == tenant_id
                )
            )
            or (
                action is not CostingAction.SOURCING_ESTIMATE_CREATE
                and (
                    actor.scope is not CostingScope.TENANT
                    or actor.role not in self._ALLOWED_ROLES
                    or (action is CostingAction.POLICY_CONFIRM and actor.role != "boss")
                )
            )
        ):
            raise PermissionDenied("Phase 1 成本授权拒绝")
        return f"phase1:{actor.role}:{actor.scope.value}:{action.value}"


class CostingActorReader(Protocol):
    """由员工公共服务投影当前在职身份；不存在/离职返回 None。"""

    async def read_current(self, tenant_id: TenantId, actor_id: EmployeeId) -> CostingActor | None:
        """按租户读取当前角色与范围，不接受请求体角色自证。"""
        ...


__all__ = (
    "CostingAction",
    "CostingActor",
    "CostingActorReader",
    "CostingAuthorizer",
    "CostingScope",
    "Phase1CostingAuthorizer",
)
