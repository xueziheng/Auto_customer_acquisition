"""当前员工身份与有限业务读取；不接受模型提供的角色或任意检索路径。"""

from __future__ import annotations

import hashlib
import json
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from domains.assistant.schemas import (
    AssistantActor,
    AssistantReadQuery,
    AuthorizedFragment,
    ObjectRef,
)
from domains.demand.service import DemandService
from domains.employees.schemas import EmployeeView
from domains.employees.service import Actor as EmployeeActor
from domains.employees.service import EmployeeService
from domains.opportunities.service import (
    Actor,
    OpportunityScope,
    OpportunityService,
    ScopeLevel,
)
from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import (
    HandoffId,
    OpportunityId,
    TenantId,
    ValidatedNeedId,
)

PRODUCT_REF = ObjectRef(kind="product_doc", object_id="assistant-help", version="v1")
PRODUCT_HELP = "TradeOS 从需求信号到需求假设，经客户明确表达后才是已验证需求。助手能澄清研究范围、读取获授权资料和准备待确认研究提案。具体价格、报价和商业承诺需要人工审批。聊天不是批准；研究信号不是已验证需求。"


class AssistantReadPort(Protocol):
    async def read(
        self, actor: AssistantActor, ref: ObjectRef
    ) -> AuthorizedFragment: ...
    async def list(
        self, actor: AssistantActor, query: AssistantReadQuery
    ) -> tuple[AuthorizedFragment, ...]: ...


class CurrentAssistantIdentity(Protocol):
    async def resolve(self, actor: AssistantActor) -> tuple[str, frozenset[str]]: ...


class EmployeeScopeFactory(Protocol):
    def __call__(
        self, tenant_id: TenantId
    ) -> AbstractAsyncContextManager[EmployeeService]: ...


class OpportunityScopeFactory(Protocol):
    def __call__(
        self, tenant_id: TenantId
    ) -> AbstractAsyncContextManager[OpportunityService]: ...


class DemandScopeFactory(Protocol):
    def __call__(
        self, tenant_id: TenantId
    ) -> AbstractAsyncContextManager[DemandService]: ...


class CurrentEmployeeIdentity:
    def __init__(self, scope: EmployeeScopeFactory, lookup: EmployeeActor) -> None:
        self._scope, self._lookup = scope, lookup

    async def employee(self, actor: AssistantActor) -> EmployeeView:
        async with self._scope(actor.tenant_id) as service:
            employee = await service.get_employee(
                actor.tenant_id, actor.employee_id, actor=self._lookup
            )
        if (
            employee.tenant_id != actor.tenant_id
            or employee.employee_id != actor.employee_id
            or employee.user_id != actor.user_id
            or not employee.is_active
            or employee.role
            not in {
                "boss",
                "manager",
                "sales",
                "sourcing",
                "product",
                "finance",
                "viewer",
            }
        ):
            raise PermissionDenied("员工身份不可用")
        return employee

    async def check(self, actor: AssistantActor) -> None:
        await self.employee(actor)

    async def require_admin(self, actor: AssistantActor) -> None:
        if (await self.employee(actor)).role != "boss":
            raise PermissionDenied("模型配置仅限当前老板")

    async def resolve(self, actor: AssistantActor) -> tuple[str, frozenset[str]]:
        employee = await self.employee(actor)
        capabilities = {"product_help"}
        if employee.role in {"boss", "manager", "sales"}:
            capabilities.update({"business_read", "research_proposal"})
        return employee.role, frozenset(capabilities)

    async def opportunity_actor(self, actor: AssistantActor) -> Actor:
        employee = await self.employee(actor)
        if employee.role == "boss":
            scope = OpportunityScope(level=ScopeLevel.TENANT)
        elif employee.role == "sales":
            scope = OpportunityScope(
                level=ScopeLevel.SELF, allowed_owners=frozenset({actor.employee_id})
            )
        elif employee.role == "manager":
            async with self._scope(actor.tenant_id) as service:
                employees = await service.list_active(
                    actor.tenant_id, actor=self._lookup
                )
            scope = OpportunityScope(
                level=ScopeLevel.MANAGER,
                allowed_owners=frozenset(
                    {
                        actor.employee_id,
                        *(
                            item.employee_id
                            for item in employees
                            if item.tenant_id == actor.tenant_id
                            and item.is_active
                            and item.manager_id == actor.employee_id
                        ),
                    }
                ),
            )
        else:
            raise PermissionDenied("当前角色不能读取业务资料")
        return Actor(actor_id=str(actor.employee_id), scope=scope, role=employee.role)


def fragment(ref: ObjectRef, values: dict[str, object]) -> AuthorizedFragment:
    text = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    version = hashlib.sha256(text.encode()).hexdigest()
    if ref.version is not None and ref.version != version:
        raise PermissionDenied("资料版本已经变化")
    return AuthorizedFragment(
        text=text, dependencies=(ref.model_copy(update={"version": version}),)
    )


class BusinessReads:
    """白名单读出口；Run reader 由上层注入现有审计服务，不反向依赖 workflows。"""

    def __init__(
        self,
        identity: CurrentEmployeeIdentity,
        opportunities: OpportunityScopeFactory,
        demand: DemandScopeFactory,
        runs: AssistantReadPort,
    ) -> None:
        self._identity, self._opportunities, self._demand, self._runs = (
            identity,
            opportunities,
            demand,
            runs,
        )

    async def read(self, actor: AssistantActor, ref: ObjectRef) -> AuthorizedFragment:
        role, _ = await self._identity.resolve(actor)
        if ref.kind == "product_doc":
            if ref.object_id != PRODUCT_REF.object_id or ref.version not in {
                None,
                PRODUCT_REF.version,
            }:
                raise PermissionDenied("产品说明不可用")
            return AuthorizedFragment(text=PRODUCT_HELP, dependencies=(PRODUCT_REF,))
        bound = await self._identity.opportunity_actor(actor)
        if ref.kind == "run":
            if role != "boss":
                raise PermissionDenied("当前角色不能读取运行记录")
            return await self._runs.read(actor, ref)
        async with self._opportunities(actor.tenant_id) as service:
            if ref.kind == "opportunity":
                opportunity = await service.get(
                    actor.tenant_id, OpportunityId(ref.object_id), actor=bound
                )
                return fragment(
                    ref,
                    {
                        "opportunity_id": opportunity.opportunity_id,
                        "state": opportunity.state,
                        "need_id": opportunity.need_id,
                        "owner": opportunity.owner,
                        "country": opportunity.country,
                        "product_category": opportunity.product_category,
                    },
                )
            if ref.kind == "handoff":
                handoff = await service.get_handoff_packet(
                    actor.tenant_id, HandoffId(ref.object_id), actor=bound
                )
                return fragment(
                    ref,
                    {
                        "handoff_id": handoff.handoff_id,
                        "opportunity_id": handoff.opportunity_id,
                        "state": handoff.state,
                        "customer_verbatim": handoff.customer_verbatim,
                    },
                )
            if role != "boss":
                owned = await service.get_by_need(
                    actor.tenant_id, ValidatedNeedId(ref.object_id), actor=bound
                )
                if owned is None:
                    raise PermissionDenied("需求不在当前业务范围")
        async with self._demand(actor.tenant_id) as demand:
            need = await demand.get_need(
                actor.tenant_id, ValidatedNeedId(ref.object_id)
            )
        return fragment(ref, {"need_id": str(need.need_id), "status": need.status})

    async def list(
        self, actor: AssistantActor, query: AssistantReadQuery
    ) -> tuple[AuthorizedFragment, ...]:
        # 既有服务没有稳定分页游标；拒绝未实现游标，不能当作任意查询透传。
        if query.cursor is not None:
            raise ValidationError("此读取暂不支持分页游标")
        bound = await self._identity.opportunity_actor(actor)
        if query.kind == "run":
            if bound.role != "boss":
                raise PermissionDenied("当前角色不能读取运行记录")
            return await self._runs.list(actor, query)
        if query.kind == "need" and bound.role == "boss":
            async with self._demand(actor.tenant_id) as demand:
                needs = await demand.list_needs(actor.tenant_id, limit=query.limit)
            refs = [ObjectRef(kind="need", object_id=str(n.need_id)) for n in needs]
        else:
            async with self._opportunities(actor.tenant_id) as service:
                if query.kind == "handoff":
                    handoffs = await service.list_pending_handoffs(
                        actor.tenant_id, bound, limit=query.limit
                    )
                    refs = [
                        ObjectRef(kind="handoff", object_id=h.handoff_id)
                        for h in handoffs
                    ]
                else:
                    opportunities = await service.list_opportunities(
                        actor.tenant_id, bound, scope=bound.scope, limit=query.limit
                    )
                    refs = [
                        ObjectRef(
                            kind=query.kind,
                            object_id=o.need_id
                            if query.kind == "need"
                            else o.opportunity_id,
                        )
                        for o in opportunities
                    ]
        return tuple([await self.read(actor, ref) for ref in refs])
