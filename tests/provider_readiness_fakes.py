"""API 测试所需的最小 Provider readiness 只读依赖。"""

from __future__ import annotations

from shared.schemas.identifiers import TenantId
from tool_gateway.provider_readiness import (
    ProviderReadinessActor,
    ProviderReadinessPermission,
)


class UnusedProviderReadiness:
    """非 Settings router 测试的 fail-fast 占位 reader。"""

    async def get_snapshot(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("本测试不应读取 Provider readiness")


def provider_readiness_dependencies(tenant_id: TenantId) -> dict[str, object]:
    """构造 tenant-bound 最小 READ 依赖，避免测试伪造进程组合状态。"""
    return {
        "provider_readiness": UnusedProviderReadiness(),
        "provider_readiness_actor": ProviderReadinessActor(
            actor_id="system:api-provider-readiness",
            tenant_id=tenant_id,
            permissions=frozenset({ProviderReadinessPermission.READ}),
        ),
    }
