"""可信内部报价准备应用：零锁来源授权→context lease→成本提交；不是HTTP接口。"""

from collections.abc import Callable
from datetime import datetime

from pydantic import TypeAdapter
from pydantic import ValidationError as SchemaError

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
    CostingScope,
    CostScopeConfirmationCommand,
    CostScopeConfirmationView,
    FrozenCostBasis,
    PricingOptions,
)
from domains.demand.service import require_current_unit
from domains.quotations.errors import (
    QuotationError,
    QuotationPermissionError,
    QuotationUnavailableError,
)
from domains.quotations.schemas import (
    Hash,
    QuotationActor,
    QuoteBusinessContext,
    QuoteDetailView,
    QuoteDraftCommand,
)
from domains.quotations.service import (
    QuotationActorReader,
    QuotationVersionService,
    QuoteContextProvider,
    QuotePreparationPolicy,
)
from shared.errors import PermissionDenied, TradeOSError
from shared.schemas.identifiers import CostSheetId, EmployeeId, OpportunityId, TenantId
from shared.schemas.provenance import FactualField
from shared.schemas.quote_creation import (
    QuoteCreationIntent,
    QuoteCreationOperationView,
    QuoteKey,
    quote_creation_request_hash,
)
from shared.schemas.quote_facts import NeedQuoteFacts
from workflows.quote_approval.basis_adapter import (
    pricing_options_from_intent,
    to_quote_basis,
)


def creation_intent(
    tenant_id: TenantId,
    command: QuoteDraftCommand,
    *,
    prepared_by: EmployeeId,
    scope_hash: Hash,
) -> QuoteCreationIntent:
    """只逐字段构造完整意图；起草人和scope身份必须从可信当前/持久记录取得。"""
    return QuoteCreationIntent(
        tenant_id=tenant_id,
        prepared_by=prepared_by,
        opportunity_id=command.opportunity_id,
        cost_sheet_id=command.cost_sheet_id,
        expected_context_hash=command.expected_context_hash,
        expected_sheet_hash=command.expected_sheet_hash,
        valid_until=command.valid_until,
        unit_price=command.unit_price,
        rounding=command.rounding,
        quote_fx_ref=command.quote_fx_ref,
        terms=command.terms,
        replaces_quote_id=command.replaces_quote_id,
        expected_quote_version=command.expected_quote_version,
        scope_confirmation_id=command.scope_confirmation_id,
        scope_confirmation_hash=scope_hash,
    )


class QuoteApplicationService:
    """真实创建/恢复编排；不接客户端operation、冻结或完成自证。"""

    def __init__(
        self,
        context_provider: QuoteContextProvider,
        costing: CostingFreezeService,
        quotations: QuotationVersionService,
        actors: QuotationActorReader,
        policy: QuotePreparationPolicy,
        *,
        now: Callable[[], datetime],
    ) -> None:
        """来源和当前员工/数据库均由可信composition注入，不设后备许可。"""
        (
            self._context,
            self._costing,
            self._quotes,
            self._actors,
            self._policy,
            self._now,
        ) = (context_provider, costing, quotations, actors, policy, now)

    async def _actor(
        self, tenant_id: TenantId, actor_id: EmployeeId
    ) -> tuple[QuotationActor, CostingActor]:
        """四角色policy通过后才显式构造成本tenant身份，不给任意员工默认scope。"""
        try:
            fact = await self._actors.read_current(tenant_id, actor_id)
        except Exception:  # noqa: BLE001 -- 当前身份依赖的诊断不得透出应用边界
            raise QuotationUnavailableError("dependency_unavailable") from None
        if (
            fact is None
            or fact.employee_id != actor_id
            or fact.tenant_id != tenant_id
            or not fact.is_active
        ):
            raise QuotationPermissionError("permission_denied")
        try:
            self._policy.require(tenant_id, fact, action="read_internal")
        except PermissionDenied:
            raise QuotationPermissionError("permission_denied") from None
        return QuotationActor(
            employee_id=fact.employee_id, role=fact.role
        ), CostingActor(
            actor_id=fact.employee_id, role=fact.role, scope=CostingScope.TENANT
        )

    def _bound_intent(
        self,
        tenant_id: TenantId,
        command: QuoteDraftCommand,
        operation: QuoteCreationOperationView,
    ) -> QuoteCreationIntent:
        """重放用原prepared_by/scope_hash构造候选；所有None和条款顺序都绑定。"""
        candidate = creation_intent(
            tenant_id,
            command,
            prepared_by=operation.intent.prepared_by,
            scope_hash=operation.intent.scope_confirmation_hash,
        )
        try:
            digest = quote_creation_request_hash(candidate)
        except (ValueError, ArithmeticError, TypeError):
            raise QuotationError("invalid_input") from None
        if (
            operation.tenant_id != tenant_id
            or operation.intent != candidate
            or operation.request_hash != digest
        ):
            raise QuotationError("idempotency_conflict")
        return candidate

    async def _complete(
        self, tenant_id: TenantId, quote: QuoteDetailView, actor: CostingActor
    ) -> QuoteDetailView:
        """必须在context/报价锁外完成；失败保留可重试，不报告完整创建成功。"""
        c = quote.content
        operation = await self._costing.complete_creation(
            tenant_id, c.operation_id, actor=actor
        )
        receipt = operation.completion
        if (
            operation.state != "completed"
            or receipt is None
            or (
                receipt.tenant_id,
                receipt.operation_id,
                receipt.request_hash,
                receipt.basis_id,
                receipt.quote_id,
                receipt.quote_version,
                receipt.quote_content_hash,
                receipt.replaces_quote_id,
                receipt.replaced_quote_version,
            )
            != (
                tenant_id,
                c.operation_id,
                c.request_hash,
                c.basis.basis_id,
                c.quote_id,
                c.version,
                c.content_hash,
                c.replaces_quote_id,
                c.replaced_quote_version,
            )
        ):
            raise QuotationUnavailableError("storage_inconsistent")
        return quote

    def _quote_binding(
        self, operation: QuoteCreationOperationView, quote: QuoteDetailView
    ) -> None:
        """持久成功记录必须来自同一操作/依据/原意图，不能以另一个报价冒充。"""
        c = quote.content
        if (
            c.tenant_id,
            c.operation_id,
            c.request_hash,
            c.basis.basis_id,
            c.intent,
        ) != (
            operation.tenant_id,
            operation.operation_id,
            operation.request_hash,
            operation.basis_id,
            operation.intent,
        ):
            raise QuotationUnavailableError("storage_inconsistent")

    async def create(
        self,
        tenant_id: TenantId,
        command: QuoteDraftCommand,
        *,
        actor_id: EmployeeId,
        idempotency_key: str,
    ) -> QuoteDetailView:
        """先查历史成功，再scope/context/session；成本冻结后报价同会话提交，锁外complete。"""
        try:
            TypeAdapter(QuoteKey).validate_python(idempotency_key, strict=True)
            if not isinstance(command, QuoteDraftCommand):
                raise TypeError("输入类型无效")
            command = QuoteDraftCommand(
                **{n: getattr(command, n) for n in QuoteDraftCommand.model_fields}
            )
        except (SchemaError, ValueError, TypeError):
            raise QuotationError("invalid_input") from None
        actor, cost_actor = await self._actor(tenant_id, actor_id)
        operation = await self._costing.get_creation(
            tenant_id, idempotency_key, actor=cost_actor
        )
        intent = None
        if operation:
            intent = self._bound_intent(tenant_id, command, operation)
            quote = await self._quotes.get_by_operation(
                tenant_id, operation.operation_id, actor=actor
            )
            if quote:
                self._quote_binding(operation, quote)
                return await self._complete(tenant_id, quote, cost_actor)
            if operation.state == "completed":
                raise QuotationUnavailableError("storage_inconsistent")
            if intent.prepared_by != actor_id:
                raise QuotationPermissionError("permission_denied")
        scope = await self._costing.get_scope(
            tenant_id, command.scope_confirmation_id, actor=cost_actor
        )
        if intent is None:
            intent = creation_intent(
                tenant_id, command, prepared_by=actor_id, scope_hash=scope.content_hash
            )
        async with self._context.open(
            tenant_id, command.opportunity_id, actor_id, prepared_by=intent.prepared_by
        ) as context:
            self._policy.require(
                tenant_id, context.runtime.current_actor, action="prepare"
            )
            async with self._quotes.open_creation(
                tenant_id, command.opportunity_id, context, actor=actor
            ) as session:
                operation = await self._costing.get_creation(
                    tenant_id, idempotency_key, actor=cost_actor
                )
                if operation:
                    intent = self._bound_intent(tenant_id, command, operation)
                quote = await session.preflight(
                    intent, operation_id=operation.operation_id if operation else None
                )
                if quote:
                    if operation is None:
                        raise QuotationUnavailableError("storage_inconsistent")
                    self._quote_binding(operation, quote)
                else:
                    if operation and operation.state == "completed":
                        raise QuotationUnavailableError("storage_inconsistent")
                    if intent.prepared_by != actor_id:
                        raise QuotationPermissionError("permission_denied")
                    basis = await self._costing.freeze(
                        tenant_id,
                        command.cost_sheet_id,
                        pricing_options_from_intent(intent),
                        costing_context(context),
                        idempotency_key=idempotency_key,
                        intent=intent,
                        actor=cost_actor,
                    )
                    quote = await session.create_from_basis(
                        intent, to_quote_basis(basis), operation_id=basis.operation_id
                    )
        return await self._complete(tenant_id, quote, cost_actor)


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
