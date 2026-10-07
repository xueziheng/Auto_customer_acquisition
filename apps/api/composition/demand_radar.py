"""Demand Radar 的跨域展示名适配与第二道员工授权。"""

from __future__ import annotations

from apps.composition_support.outreach_fact_readers import (
    ProspectingDemandAccountNames as ProspectingDemandAccountNames,  # noqa: PLC0414
)
from domains.demand.schemas import (
    DemandSignalView,
    HypothesisView,
    NeedClusterView,
    ValidatedNeedView,
)
from domains.demand.service import DemandService
from domains.employees.permissions import (
    Actor as EmployeeActor,
)
from domains.employees.permissions import EmployeeAction, EmployeeAuthorizer
from shared.errors import ValidationError
from shared.schemas.identifiers import (
    NeedClusterId,
    NeedHypothesisId,
    TenantId,
    ValidatedNeedId,
)


class AuthorizedDemandRadarService:
    """router gate 之外再次按持久员工身份的 typed scope 判权。"""

    def __init__(
        self,
        demand: DemandService,
        authorizer: EmployeeAuthorizer,
    ) -> None:
        if (
            not callable(getattr(demand, "list_signals", None))
            or not callable(getattr(demand, "list_hypotheses", None))
            or not callable(getattr(demand, "get_hypothesis", None))
            or not callable(getattr(demand, "list_needs", None))
            or not callable(getattr(demand, "get_need", None))
            or not callable(getattr(demand, "list_clusters", None))
            or not callable(getattr(demand, "get_cluster", None))
            or not isinstance(authorizer, EmployeeAuthorizer)
        ):
            raise ValidationError("Demand Radar 服务依赖无效")
        self._demand = demand
        self._authorizer = authorizer

    def _require(self, tenant_id: TenantId, actor: EmployeeActor) -> None:
        self._authorizer.require(
            actor,
            EmployeeAction.OWNERSHIP_READ,
            actor.scope,
            tenant_id,
        )

    async def list_signals(
        self,
        tenant_id: TenantId,
        actor: EmployeeActor,
        *,
        signal_type: str | None,
        status: str | None,
        limit: int,
    ) -> list[DemandSignalView]:
        self._require(tenant_id, actor)
        return await self._demand.list_signals(
            tenant_id,
            signal_type=signal_type,
            status=status,
            limit=limit,
        )

    async def list_hypotheses(
        self,
        tenant_id: TenantId,
        actor: EmployeeActor,
        *,
        status: str | None,
        limit: int,
    ) -> list[HypothesisView]:
        self._require(tenant_id, actor)
        return await self._demand.list_hypotheses(
            tenant_id,
            status=status,
            limit=limit,
        )

    async def get_hypothesis(
        self,
        tenant_id: TenantId,
        actor: EmployeeActor,
        hypothesis_id: NeedHypothesisId,
    ) -> HypothesisView:
        self._require(tenant_id, actor)
        return await self._demand.get_hypothesis(tenant_id, hypothesis_id)

    async def list_needs(
        self,
        tenant_id: TenantId,
        actor: EmployeeActor,
        *,
        status: str | None,
        limit: int,
    ) -> list[ValidatedNeedView]:
        self._require(tenant_id, actor)
        return await self._demand.list_needs(
            tenant_id,
            status=status,
            limit=limit,
        )

    async def get_need(
        self,
        tenant_id: TenantId,
        actor: EmployeeActor,
        need_id: ValidatedNeedId,
    ) -> ValidatedNeedView:
        self._require(tenant_id, actor)
        return await self._demand.get_need(tenant_id, need_id)

    async def list_clusters(
        self,
        tenant_id: TenantId,
        actor: EmployeeActor,
        *,
        limit: int,
    ) -> list[NeedClusterView]:
        self._require(tenant_id, actor)
        return await self._demand.list_clusters(tenant_id, limit=limit)

    async def get_cluster(
        self,
        tenant_id: TenantId,
        actor: EmployeeActor,
        cluster_id: NeedClusterId,
    ) -> NeedClusterView:
        self._require(tenant_id, actor)
        return await self._demand.get_cluster(tenant_id, cluster_id)


__all__ = (
    "AuthorizedDemandRadarService",
    "ProspectingDemandAccountNames",
)
