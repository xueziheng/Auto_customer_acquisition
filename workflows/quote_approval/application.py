"""可信内部报价准备应用：零锁来源授权→context lease→成本提交；不是HTTP接口。"""

from domains.costing.errors import (
    CostFreezeError,
    CostFreezePermissionError,
    CostFreezeUnavailableError,
)
from domains.costing.service import (
    CalculationSnapshot,
    CostingActor,
    CostingActorReader,
    CostingContext,
    CostingFreezeService,
    CostScopeConfirmationCommand,
    CostScopeConfirmationView,
    FrozenCostBasis,
    PricingOptions,
)
from domains.demand.service import require_current_unit
from domains.quotations.schemas import QuoteBusinessContext
from domains.quotations.service import QuoteContextProvider, QuotePreparationPolicy
from shared.errors import TradeOSError
from shared.schemas.identifiers import CostSheetId, EmployeeId, OpportunityId, TenantId
from shared.schemas.provenance import FactualField
from shared.schemas.quote_creation import QuoteCreationIntent
from shared.schemas.quote_facts import NeedQuoteFacts


class DemandNeedFactsValidator:
    """只适配demand公共单位规则与固定错误，不在workflow复制其算法。"""

    def require_current_unit(self, facts: NeedQuoteFacts) -> FactualField[str]:
        """单位有效性仍由demand唯一判断。"""
        try:
            return require_current_unit(facts)
        except TradeOSError as exc:
            code = getattr(exc, "code", None)
            if code == "quantity_invalid":
                raise CostFreezeError("quantity_mismatch") from None
            if code in {
                "unit_missing",
                "unit_stale",
                "fact_unconfirmed",
                "facts_corrupt",
                "invalid_input",
            }:
                raise CostFreezeError(code) from None
            raise CostFreezeUnavailableError("dependency_unavailable") from None


def costing_context(context: QuoteBusinessContext) -> CostingContext:
    """显式等值投影，排除客户名称与抬头，但保留原context hash。"""
    return CostingContext(
        tenant_id=context.tenant_id,
        opportunity_id=context.opportunity_id,
        need_id=context.need_id,
        account_id=context.account_id,
        opportunity_state=context.opportunity_state,
        owner_id=context.owner_id,
        prepared_by=context.prepared_by,
        category=context.category,
        specification=context.specification,
        unit=context.unit,
        destination=context.destination,
        quantity=context.quantity,
        need_facts=context.need_facts,
        need_facts_hash=context.need_facts_hash,
        specification_hash=context.specification_hash,
        runtime=context.runtime,
        context_hash=context.context_hash,
    )


class QuotePreparationApplication:
    """不创建报价假回执；真实create/修订由T4接续。"""

    def __init__(
        self,
        context_provider: QuoteContextProvider,
        costing: CostingFreezeService,
        policy: QuotePreparationPolicy,
        actors: CostingActorReader,
    ) -> None:
        self._context, self._costing, self._policy, self._actors = (
            context_provider,
            costing,
            policy,
            actors,
        )

    async def _actor(self, tenant_id: TenantId, actor_id: EmployeeId) -> CostingActor:
        """获取可信身份后再调用领域授权，不从HTTP或角色标签自证。"""
        try:
            actor = await self._actors.read_current(tenant_id, actor_id)
        except Exception:  # noqa: BLE001 -- 身份依赖异常不得将原文泄漏给调用者
            raise CostFreezeUnavailableError("dependency_unavailable") from None
        if actor is None:
            raise CostFreezePermissionError("permission_denied")
        return actor

    async def confirm_scope(
        self,
        tenant_id: TenantId,
        opportunity_id: OpportunityId,
        cost_sheet_id: CostSheetId,
        command: CostScopeConfirmationCommand,
        *,
        actor_id: EmployeeId,
        idempotency_key: str,
    ) -> CostScopeConfirmationView:
        """来源权限读取在所有lease/成本锁外，成本提交保持在lease内。"""
        actor = await self._actor(tenant_id, actor_id)
        access = await self._costing.prepare_scope_access(
            tenant_id, cost_sheet_id, command, actor=actor
        )
        async with self._context.open(
            tenant_id, opportunity_id, actor_id, prepared_by=actor_id
        ) as context:
            self._policy.require(
                tenant_id, context.runtime.current_actor, action="prepare"
            )
            return await self._costing.confirm_scope(
                tenant_id,
                cost_sheet_id,
                command,
                costing_context(context),
                actor=actor,
                idempotency_key=idempotency_key,
                source_access=access,
            )

    async def calculate(
        self,
        tenant_id: TenantId,
        opportunity_id: OpportunityId,
        cost_sheet_id: CostSheetId,
        options: PricingOptions,
        *,
        quote_fx_ref: str | None,
        actor_id: EmployeeId,
    ) -> CalculationSnapshot:
        """内部测算在同lease下读取完整当前事实，结果不是未来有效性保证。"""
        actor = await self._actor(tenant_id, actor_id)
        async with self._context.open(
            tenant_id, opportunity_id, actor_id, prepared_by=actor_id
        ) as context:
            self._policy.require(
                tenant_id, context.runtime.current_actor, action="prepare"
            )
            return await self._costing.calculate(
                tenant_id,
                cost_sheet_id,
                options,
                costing_context(context),
                quote_fx_ref=quote_fx_ref,
                actor=actor,
            )

    async def freeze(
        self,
        tenant_id: TenantId,
        intent: QuoteCreationIntent,
        options: PricingOptions,
        *,
        actor_id: EmployeeId,
        idempotency_key: str,
    ) -> FrozenCostBasis:
        """新建必须由本次起草人发起，costing提交成功后才退出事实lease。"""
        if intent.prepared_by != actor_id or intent.tenant_id != tenant_id:
            raise CostFreezePermissionError("permission_denied")
        actor = await self._actor(tenant_id, actor_id)
        async with self._context.open(
            tenant_id, intent.opportunity_id, actor_id, prepared_by=intent.prepared_by
        ) as context:
            self._policy.require(
                tenant_id, context.runtime.current_actor, action="prepare"
            )
            return await self._costing.freeze(
                tenant_id,
                intent.cost_sheet_id,
                options,
                costing_context(context),
                idempotency_key=idempotency_key,
                intent=intent,
                actor=actor,
            )
