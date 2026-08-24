"""合规域权限契约与 Phase 1 tenant-bound 默认拒绝矩阵。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import TenantId


class ComplianceScope(str, Enum):
    SYSTEM = "system"
    TENANT = "tenant"


class ComplianceAction(str, Enum):
    COUNTRY_POLICY_READ = "country_policy:read"
    COUNTRY_POLICY_PROPOSE = "country_policy:propose"
    COUNTRY_POLICY_DECIDE = "country_policy:decide"
    COUNTRY_POLICY_CHANGE_SNAPSHOT_READ = "country_policy_change_snapshot:read"
    COUNTRY_POLICY_ACTIVATE = "country_policy:activate"


def _bounded_text(value: object, message: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > 200
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValidationError(message)
    return value


@dataclass(frozen=True)
class ComplianceActor:
    """由可信身份上下文显式构造；请求体不能生成系统身份。"""

    actor_id: str
    tenant_id: TenantId
    scope: ComplianceScope
    role: str

    def __post_init__(self) -> None:
        _bounded_text(self.actor_id, "合规操作身份无效")
        _bounded_text(self.tenant_id, "合规操作租户无效")
        if not isinstance(self.scope, ComplianceScope):
            raise ValidationError("合规操作范围无效")
        _bounded_text(self.role, "合规操作角色无效")


@runtime_checkable
class ComplianceAuthorizer(Protocol):
    def require(
        self,
        actor: ComplianceActor,
        action: ComplianceAction,
        scope: ComplianceScope,
        tenant_id: TenantId,
    ) -> str: ...


_BOSS_ACTIONS = frozenset(
    {
        ComplianceAction.COUNTRY_POLICY_READ,
        ComplianceAction.COUNTRY_POLICY_PROPOSE,
    }
)
_SYSTEM_ACTIONS = frozenset(
    {
        ComplianceAction.COUNTRY_POLICY_DECIDE,
        ComplianceAction.COUNTRY_POLICY_CHANGE_SNAPSHOT_READ,
        ComplianceAction.COUNTRY_POLICY_ACTIVATE,
    }
)


class Phase1ComplianceAuthorizer:
    """只放行简报规定的 boss/TENANT 与 system/SYSTEM 组合。"""

    def __init__(self, tenant_id: TenantId) -> None:
        _bounded_text(tenant_id, "合规授权租户无效")
        self._tenant_id = tenant_id

    def require(
        self,
        actor: ComplianceActor,
        action: ComplianceAction,
        scope: ComplianceScope,
        tenant_id: TenantId,
    ) -> str:
        allowed = {
            ("boss", ComplianceScope.TENANT): _BOSS_ACTIONS,
            ("system", ComplianceScope.SYSTEM): _SYSTEM_ACTIONS,
        }
        if (
            not isinstance(actor, ComplianceActor)
            or not isinstance(action, ComplianceAction)
            or not isinstance(scope, ComplianceScope)
            or actor.scope is not scope
            or actor.tenant_id != self._tenant_id
            or tenant_id != self._tenant_id
            or action not in allowed.get((actor.role, scope), frozenset())
        ):
            raise PermissionDenied(
                "Phase 1 合规授权拒绝",
                context={
                    "actor_id": getattr(actor, "actor_id", "invalid"),
                    "action": getattr(action, "value", "invalid"),
                    "tenant_id": str(tenant_id),
                },
            )
        return f"phase1:{actor.role}:{scope.value}:{action.value}"


__all__ = (
    "ComplianceAction",
    "ComplianceActor",
    "ComplianceAuthorizer",
    "ComplianceScope",
    "Phase1ComplianceAuthorizer",
)
