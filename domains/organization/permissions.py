"""组织域权限契约与 Phase 1 默认拒绝矩阵。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import TenantId


class OrganizationAction(str, Enum):
    """组织域开放的动作；快照读取与激活只供系统工作流使用。"""

    PLAYBOOK_READ = "playbook:read"
    PLAYBOOK_PROPOSE = "playbook:propose"
    PLAYBOOK_CHANGE_SNAPSHOT_READ = "playbook_change_snapshot:read"
    PLAYBOOK_ACTIVATE = "playbook:activate"


class OrganizationScopeLevel(str, Enum):
    SYSTEM = "system"
    TENANT = "tenant"


def _require_bounded_text(value: object, message: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 200
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValidationError(message)
    return value.strip()


@dataclass(frozen=True)
class OrganizationScope:
    level: OrganizationScopeLevel
    tenant_id: TenantId

    def __post_init__(self) -> None:
        if not isinstance(self.level, OrganizationScopeLevel):
            raise ValidationError("组织操作范围级别无效")
        _require_bounded_text(self.tenant_id, "组织操作租户无效")


@dataclass(frozen=True)
class OrganizationActor:
    """由可信身份上下文构造，绝不从请求体读取。"""

    actor_id: str
    scope: OrganizationScope
    role: str

    def __post_init__(self) -> None:
        _require_bounded_text(self.actor_id, "组织操作身份无效")
        if not isinstance(self.scope, OrganizationScope):
            raise ValidationError("组织操作范围无效")
        _require_bounded_text(self.role, "组织操作角色无效")


@runtime_checkable
class OrganizationAuthorizer(Protocol):
    def require(
        self,
        actor: OrganizationActor,
        action: OrganizationAction,
        tenant_id: TenantId,
    ) -> str: ...


class Phase1OrganizationAuthorizer:
    """Phase 1 只允许老板读/提交，系统工作流读快照/激活。"""

    def __init__(self, tenant_id: TenantId) -> None:
        _require_bounded_text(tenant_id, "组织授权租户无效")
        self._tenant_id = tenant_id

    def require(
        self,
        actor: OrganizationActor,
        action: OrganizationAction,
        tenant_id: TenantId,
    ) -> str:
        allowed = {
            ("boss", OrganizationScopeLevel.TENANT): frozenset(
                {
                    OrganizationAction.PLAYBOOK_READ,
                    OrganizationAction.PLAYBOOK_PROPOSE,
                }
            ),
            ("system", OrganizationScopeLevel.SYSTEM): frozenset(
                {
                    OrganizationAction.PLAYBOOK_CHANGE_SNAPSHOT_READ,
                    OrganizationAction.PLAYBOOK_ACTIVATE,
                }
            ),
        }
        if (
            tenant_id != self._tenant_id
            or actor.scope.tenant_id != self._tenant_id
            or not isinstance(action, OrganizationAction)
            or action not in allowed.get((actor.role, actor.scope.level), frozenset())
        ):
            raise PermissionDenied(
                "Phase 1 组织授权拒绝",
                context={
                    "actor_id": actor.actor_id,
                    "action": getattr(action, "value", "invalid"),
                    "tenant_id": str(tenant_id),
                },
            )
        return f"phase1:{actor.role}:{actor.scope.level.value}:{action.value}"


__all__ = (
    "OrganizationAction",
    "OrganizationActor",
    "OrganizationAuthorizer",
    "OrganizationScope",
    "OrganizationScopeLevel",
    "Phase1OrganizationAuthorizer",
)
