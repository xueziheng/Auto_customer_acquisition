"""可信内部报价准备应用：零锁来源授权→context lease→成本提交；不是HTTP接口。"""

from collections.abc import Callable
from datetime import datetime

from pydantic import TypeAdapter
from pydantic import ValidationError as SchemaError

from domains.approvals.service import ApprovalService, ApprovalType, BlastRadius
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
from domains.quotations import schemas as qa
from domains.quotations.errors import (
    QuotationError,
    QuotationPermissionError,
    QuotationUnavailableError,
    QuoteApprovalError,
    QuoteApprovalErrorCode,
    QuoteApprovalPermissionError,
    QuoteApprovalUnavailableError,
    QuoteContextError,
    QuoteContextUnavailableError,
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
    quote_approval_facts_hash,
    quote_change_set_ref,
)
from shared.errors import PermissionDenied, TradeOSError
from shared.schemas.identifiers import (
    ApprovalId,
    CostSheetId,
    EmployeeId,
    OpportunityId,
    QuoteId,
    TenantId,
)
from shared.schemas.provenance import FactualField
from shared.schemas.quote_creation import (
    QuoteCreationIntent,
    QuoteCreationOperationView,
    QuoteKey,
    quote_creation_request_hash,
)
from shared.schemas.quote_facts import NeedQuoteFacts
from workflows.quote_approval.approvals import read_quote_facts
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


_APPROVAL_FAILURES: dict[QuoteApprovalErrorCode,str] = {
    "approval_fact_invalid":"QUOTE_APPROVAL_FACT_INVALID",
    "context_changed":"QUOTE_APPROVAL_CONTEXT_CHANGED",
    "policy_stale":"QUOTE_APPROVAL_POLICY_STALE",
    "evidence_invalid":"QUOTE_APPROVAL_EVIDENCE_INVALID",
    "evidence_expired":"QUOTE_APPROVAL_EVIDENCE_EXPIRED",
    "decider_invalid":"QUOTE_APPROVAL_DECIDER_INVALID",
    "approval_expired":"QUOTE_APPROVAL_EXPIRED",
}


class QuoteApprovalApplication:
    """单轮真实审批编排；成功receipt优先于任何fresh上下文。"""

    def __init__(self, quotations: QuotationVersionService, approvals: ApprovalService,
                 context_provider: QuoteContextProvider, actors: QuotationActorReader,
                 *, now: Callable[[],datetime]) -> None:
        """全部依赖来自可信composition，不暴露客户端executor构造。"""
        self._quotes,self._approvals,self._context,self._actors,self._now = (
            quotations,approvals,context_provider,actors,now)

    def remaining_wait_seconds(self, deadline: datetime | None) -> int:
        """只供本流程调度，真实批准仍以锁后时钟判定。"""
        if deadline is None:
            return 0
        delta = deadline - self._now()
        return max(0,delta.days*86400+delta.seconds+(1 if delta.microseconds else 0))

    async def _actor(self, tenant_id: TenantId, employee_id: EmployeeId) -> QuotationActor:
        """读取真实当前身份；四成本角色仍由报价服务唯一policy判定。"""
        try:
            fact = await self._actors.read_current(tenant_id,employee_id)
        except Exception:  # noqa: BLE001 -- 员工基础设施诊断不越过应用边界
            raise QuoteApprovalUnavailableError("dependency_unavailable") from None
        if fact is None or fact.tenant_id != tenant_id or fact.employee_id != employee_id or not fact.is_active:
            raise QuoteApprovalPermissionError("permission_denied")
        return QuotationActor(employee_id=employee_id,role=fact.role)

    async def submit(self, tenant_id: TenantId, quote_id: QuoteId, *, initiated_by: EmployeeId,
                     executor: qa.QuoteWorkflowExecutor) -> qa.QuoteApprovalSubmission:
        """原namespace恢复逐包提交，全组绑定事务不会启动第二轮。"""
        target = await self._quotes.approval_target(tenant_id,quote_id,executor=executor)
        actor = await self._actor(tenant_id,initiated_by)
        await self._quotes.approval_snapshot(tenant_id,quote_id,actor=actor)
        async with self._quotes.open_approval(tenant_id,quote_id,executor=executor) as session:
            existing = await session.submission()
            if existing is not None:
                return existing
            snapshot = await session.snapshot()
            found: list[ApprovalId] = []
            for payload in snapshot.payloads:
                fact = await self._approvals.find_quote_fact(tenant_id,
                    quote_change_set_ref(quote_id,payload.content_hash,payload.approval_type))
                if fact is not None:
                    found.append(fact.approval_id)
            if len(found) == len(snapshot.payloads):
                submission = await self._submit_group(snapshot,executor=executor,
                    expected_ids=tuple(found))
                await session.recover_submission(submission,actor=actor)
                return submission
        c = target.internal_quote.content
        async with (
            self._context.open(tenant_id, c.opportunity_id, initiated_by, prepared_by=c.prepared_by) as context,
            self._quotes.open_approval(tenant_id, quote_id, executor=executor) as session,
        ):
            existing = await session.submission()
            if existing is not None:
                return existing
            snapshot = await session.prepare_submission(context,actor=actor)
            submission = await self._submit_group(snapshot,executor=executor)
            await session.bind(submission,context,actor=actor)
            return submission

    async def _submit_group(self, snapshot: qa.QuoteApprovalSnapshot, *,
        executor: qa.QuoteWorkflowExecutor,
        expected_ids: tuple[ApprovalId,...] | None = None) -> qa.QuoteApprovalSubmission:
        """首次与重放共享原固定请求；恢复逐包确认原ID，不接受新包替换。"""
        c = snapshot.internal_quote.content
        tenant_id,quote_id = c.tenant_id,c.quote_id
        ids: list[ApprovalId] = []
        for index,payload in enumerate(snapshot.payloads):
            kind = payload.approval_type
            approval_id = await self._approvals.submit(tenant_id,ApprovalType(kind),
                f"报价审批 {quote_id} {kind}",payload.model_dump(mode="json"),
                f"独立确认报价版本 {quote_id} 的 {kind}",
                BlastRadius([str(quote_id)],"允许本报价版本进入批准态","关闭本轮报价审批",False),
                proposed_by_run=executor.run_id,proposed_by_employee=c.prepared_by,
                owner_employee=c.owner_id,
                evidence_refs=[f"quote-evidence:{quote_id}:{e.evidence_id}" for e in payload.evidence],
                change_set_ref=quote_change_set_ref(quote_id,c.content_hash,kind),
                expires_at_limit=snapshot.expires_at_limit)
            if expected_ids is not None and approval_id != expected_ids[index]:
                raise QuoteApprovalError("approval_binding_conflict")
            ids.append(approval_id)
        facts = await read_quote_facts(self._approvals,tenant_id,tuple(ids))
        return qa.QuoteApprovalSubmission(tenant_id=tenant_id,quote_id=quote_id,
            quote_version=c.version,content_hash=c.content_hash,policy_id=c.basis.policy_id,
            policy_hash=c.basis.policy.content_hash,required_types=snapshot.required_types,facts=facts)

    async def poll(self, tenant_id: TenantId, quote_id: QuoteId, *,
                   executor: qa.QuoteWorkflowExecutor) -> qa.QuoteApprovalPollResult:
        """只从真实绑定读决定；ready不是报价批准，timeout也不能批准。"""
        async with self._quotes.open_approval(tenant_id,quote_id,executor=executor) as session:
            receipt = await session.receipt()
            submission = await session.submission()
            ids = tuple(f.approval_id for f in submission.facts) if submission else ()
            facts = await read_quote_facts(self._approvals,tenant_id,ids)
            deadline = min((f.expires_at for f in facts),default=None)
            if receipt is not None:
                if receipt.facts_hash != quote_approval_facts_hash(facts):
                    raise QuoteApprovalError("approval_fact_invalid")
                outcome,error = "already_applied",None
            elif not facts:
                outcome,error = "waiting",None
            else:
                if any(f.state=="applied" for f in facts):
                    raise QuoteApprovalUnavailableError("storage_inconsistent")
                terminated = await session.terminate(facts)
                outcome,error = terminated.outcome,terminated.error_code
                if outcome == "waiting":
                    if any(f.state=="apply_failed" for f in facts):
                        outcome,error = "blocked","approval_fact_invalid"
                    elif all(f.state=="approved" for f in facts):
                        outcome = "ready"
            return qa.QuoteApprovalPollResult(outcome=outcome,approval_ids=ids,deadline=deadline,error_code=error)

    async def mark_completed(self, tenant_id: TenantId, quote_id: QuoteId, *,
                             executor: qa.QuoteWorkflowExecutor) -> qa.QuoteApprovalApplicationReceipt:
        """receipt与决定hash匹配才逐包补记；无需当前员工/Need/policy。"""
        receipt = await self._quotes.get_approval_application(tenant_id,quote_id,executor=executor)
        if receipt is None:
            raise QuoteApprovalUnavailableError("storage_inconsistent")
        facts = await read_quote_facts(self._approvals,tenant_id,tuple(d.approval_id for d in receipt.decisions))
        if quote_approval_facts_hash(facts) != receipt.facts_hash or any(f.state not in {"approved","applied"} for f in facts):
            raise QuoteApprovalUnavailableError("storage_inconsistent")
        for fact in facts:
            await self._approvals.mark_applied(tenant_id,fact.approval_id,
                f"quote-apply:{quote_id}:{receipt.content_hash}:{fact.approval_type}")
        return receipt

    async def _record_failure(self, tenant_id: TenantId, quote_id: QuoteId,
                              executor: qa.QuoteWorkflowExecutor, code: QuoteApprovalErrorCode) -> qa.QuoteApprovalApplyResult:
        """持报价机会锁先查receipt，禁止迟到失败覆盖另一执行者已成功。"""
        receipt = None
        async with self._quotes.open_approval(tenant_id,quote_id,executor=executor) as session:
            receipt = await session.receipt()
            snapshot = await session.snapshot()
            if receipt is None:
                submission = await session.submission()
                if submission is None:
                    raise QuoteApprovalError("approval_binding_conflict")
                facts = await read_quote_facts(self._approvals,tenant_id,tuple(f.approval_id for f in submission.facts))
                terminal = await session.terminate(facts)
                if terminal.outcome not in {"waiting","expired"}:
                    return terminal
                for fact in facts:
                    if fact.state == "approved":
                        await self._approvals.mark_apply_failed(tenant_id,fact.approval_id,_APPROVAL_FAILURES[code])
        if receipt is not None:
            receipt = await self.mark_completed(tenant_id,quote_id,executor=executor)
            return qa.QuoteApprovalApplyResult(outcome="already_applied",quote=snapshot.internal_quote,receipt=receipt,error_code=None)
        return qa.QuoteApprovalApplyResult(outcome="blocked",quote=snapshot.internal_quote,receipt=None,error_code=code)

    async def apply(self, tenant_id: TenantId, quote_id: QuoteId, *,
                    executor: qa.QuoteWorkflowExecutor) -> qa.QuoteApprovalApplyResult:
        """全部真实批准才进入全员工context，提交后退出lease再补记。"""
        receipt = await self._quotes.get_approval_application(tenant_id,quote_id,executor=executor)
        target = await self._quotes.approval_target(tenant_id,quote_id,executor=executor)
        if receipt is not None:
            receipt = await self.mark_completed(tenant_id,quote_id,executor=executor)
            return qa.QuoteApprovalApplyResult(outcome="already_applied",quote=target.internal_quote,receipt=receipt,error_code=None)
        poll = await self.poll(tenant_id,quote_id,executor=executor)
        if poll.outcome != "ready":
            refreshed = await self._quotes.approval_target(tenant_id,quote_id,executor=executor)
            return qa.QuoteApprovalApplyResult(outcome=poll.outcome,quote=refreshed.internal_quote,receipt=None,error_code=poll.error_code)
        facts = await read_quote_facts(self._approvals,tenant_id,poll.approval_ids)
        send = next(f for f in facts if f.approval_type=="quote_send")
        if send.decided_by is None or any(f.decided_by is None for f in facts):
            raise QuoteApprovalError("approval_fact_invalid")
        c = target.internal_quote.content
        try:
            async with (
                self._context.open_for_approval(tenant_id, c.opportunity_id, send.decided_by, prepared_by=c.prepared_by, decider_ids=tuple(sorted({f.decided_by for f in facts}))) as context,
                self._quotes.open_approval(tenant_id, quote_id, executor=executor) as session,
            ):
                current = await read_quote_facts(self._approvals,tenant_id,poll.approval_ids)
                if quote_approval_facts_hash(current) != quote_approval_facts_hash(facts):
                    raise QuoteApprovalError("context_changed")
                result = await session.apply(current,context)
        except QuoteContextUnavailableError as error:
            raise QuoteApprovalUnavailableError(error.code) from None
        except QuoteContextError:
            return await self._record_failure(tenant_id,quote_id,executor,"context_changed")
        except QuoteApprovalError as error:
            if error.code not in _APPROVAL_FAILURES:
                raise
            return await self._record_failure(tenant_id,quote_id,executor,error.code)
        if result.receipt is not None:
            await self.mark_completed(tenant_id,quote_id,executor=executor)
        return result
