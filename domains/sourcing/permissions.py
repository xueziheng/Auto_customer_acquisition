"""Sourcing V2 typed 权限与 tenant-bound 默认拒绝矩阵。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import EmployeeId, TenantId


class SourcingAction(str, Enum):
    """寻源域公开动作；未知字符串不会被隐式接受。"""

    CASE_READ = "sourcing_case:read"
    CASE_LIST = "sourcing_case:list"
    CASE_OPEN = "sourcing_case:open"
    WORKFLOW_PROGRESS = "sourcing_workflow:progress"
    FACT_PUBLISH = "sourcing_fact:publish"
    PLAN_DRAFT = "sourcing_plan:draft"
    PLAN_CONFIRM = "sourcing_plan:confirm"
    PLAN_RUN = "sourcing_plan:run"
    SEARCH_RECONCILE = "sourcing_search:reconcile"
    CANDIDATE_ENTER = "sourcing_candidate:enter"
    REVIEW_SUBMIT = "sourcing_review:submit"
    REVIEW_CONFIRM = "sourcing_review:confirm"
    COSTING_HANDOFF_READ = "sourcing_costing_handoff:read"
    ADMISSION_ENQUEUE = "sourcing_admission:enqueue"
    ADMISSION_REFRESH = "sourcing_admission:refresh"
    ADMISSION_CLAIM = "sourcing_admission:claim"
    ADMISSION_COMPLETE = "sourcing_admission:complete"
    ADMISSION_READ = "sourcing_admission:read"
    ADMISSION_MANUAL_START = "sourcing_admission:manual_start"


class SourcingScope(str, Enum):
    """SYSTEM 只供工作流；TENANT 只供当前在职内部角色。"""

    SYSTEM = "system"
    TENANT = "tenant"


def _bounded_identity(value: object, message: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 200
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValidationError(message)


@dataclass(frozen=True)
class SourcingActor:
    """由可信员工/系统身份解析器构造，绝不从请求体反序列化。"""

    actor_id: str
    tenant_id: TenantId
    scope: SourcingScope
    role: str

    def __post_init__(self) -> None:
        _bounded_identity(self.actor_id, "寻源操作身份无效")
        _bounded_identity(self.tenant_id, "寻源操作租户无效")
        if not isinstance(self.scope, SourcingScope):
            raise ValidationError("寻源操作范围无效")
        _bounded_identity(self.role, "寻源操作角色无效")
        if (self.role == "system") != (self.scope is SourcingScope.SYSTEM):
            raise ValidationError("system 角色与 SYSTEM 范围必须成对")


@runtime_checkable
class SourcingAuthorizer(Protocol):
    def require(
        self,
        actor: SourcingActor,
        action: SourcingAction,
        scope: SourcingScope,
        tenant_id: TenantId,
    ) -> str:
        """公开服务先调用本方法；拒绝后不得读取业务数据。"""

        ...


@runtime_checkable
class SourcingActorReader(Protocol):
    async def read_current(
        self, tenant_id: TenantId, actor_id: EmployeeId
    ) -> SourcingActor | None:
        """从员工公共服务读取当前身份；不存在或离职返回 ``None``。"""

        ...


_READ_ACTIONS = frozenset(
    {
        SourcingAction.CASE_READ,
        SourcingAction.CASE_LIST,
    }
)
_ADMISSION_READ_ACTIONS = frozenset({SourcingAction.ADMISSION_READ})
_BOSS_ACTIONS = _READ_ACTIONS | frozenset(
    {
        SourcingAction.PLAN_DRAFT,
        SourcingAction.PLAN_CONFIRM,
        SourcingAction.PLAN_RUN,
        SourcingAction.SEARCH_RECONCILE,
        SourcingAction.REVIEW_CONFIRM,
        SourcingAction.REVIEW_SUBMIT,
        SourcingAction.ADMISSION_MANUAL_START,
    }
) | _ADMISSION_READ_ACTIONS
_PRODUCT_ACTIONS = _READ_ACTIONS | frozenset(
    {
        SourcingAction.CANDIDATE_ENTER,
        SourcingAction.REVIEW_SUBMIT,
    }
) | _ADMISSION_READ_ACTIONS
_SOURCING_ACTIONS = _PRODUCT_ACTIONS | frozenset(
    {SourcingAction.PLAN_DRAFT, SourcingAction.ADMISSION_MANUAL_START}
)
_FINANCE_ACTIONS = (
    _READ_ACTIONS
    | _ADMISSION_READ_ACTIONS
    | frozenset({SourcingAction.COSTING_HANDOFF_READ})
)
_SYSTEM_ACTIONS = frozenset(
    {
        SourcingAction.CASE_OPEN,
        SourcingAction.WORKFLOW_PROGRESS,
        SourcingAction.FACT_PUBLISH,
        SourcingAction.COSTING_HANDOFF_READ,
        SourcingAction.ADMISSION_ENQUEUE,
        SourcingAction.ADMISSION_REFRESH,
        SourcingAction.ADMISSION_CLAIM,
        SourcingAction.ADMISSION_COMPLETE,
    }
)


class Phase2SourcingAuthorizer:
    """Phase 2 固定矩阵；actor、scope、tenant 或 action 任一异常即拒绝。"""

    def __init__(self, tenant_id: TenantId) -> None:
        _bounded_identity(tenant_id, "寻源授权租户无效")
        self._tenant_id = tenant_id

    def require(
        self,
        actor: SourcingActor,
        action: SourcingAction,
        scope: SourcingScope,
        tenant_id: TenantId,
    ) -> str:
        allowed = {
            ("boss", SourcingScope.TENANT): _BOSS_ACTIONS,
            ("product", SourcingScope.TENANT): _PRODUCT_ACTIONS,
            ("sourcing", SourcingScope.TENANT): _SOURCING_ACTIONS,
            ("finance", SourcingScope.TENANT): _FINANCE_ACTIONS,
            ("system", SourcingScope.SYSTEM): _SYSTEM_ACTIONS,
        }
        if (
            not isinstance(actor, SourcingActor)
            or not isinstance(action, SourcingAction)
            or not isinstance(scope, SourcingScope)
            or tenant_id != self._tenant_id
            or actor.tenant_id != self._tenant_id
            or actor.scope is not scope
            or action not in allowed.get((actor.role, scope), frozenset())
        ):
            raise PermissionDenied(
                "Phase 2 寻源授权拒绝",
                context={
                    "actor_id": getattr(actor, "actor_id", "invalid"),
                    "action": getattr(action, "value", "invalid"),
                    "tenant_id": str(tenant_id),
                },
            )
        return f"phase2:{actor.role}:{scope.value}:{action.value}"


__all__ = (
    "Phase2SourcingAuthorizer",
    "SourcingAction",
    "SourcingActor",
    "SourcingActorReader",
    "SourcingAuthorizer",
    "SourcingScope",
)
