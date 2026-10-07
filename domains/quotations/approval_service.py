"""单用途报价审批会话：当前授权、真实run绑定及报价侧原子成功效果。"""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime
from typing import Literal, Protocol, cast

from domains.quotations.approval_rules import (
    APPROVAL_TYPES,
    build_quote_approval_snapshot,
    quote_approval_facts_hash,
    require_quote_approval_access,
    require_quote_approval_bindings,
    require_quote_approval_facts,
    require_quote_approval_receipt,
    require_quote_approved_decisions,
    require_quote_current_basis,
    require_quote_current_deciders,
    require_quote_decision_states,
    require_quote_run_binding,
    required_quote_approvals,
)
from domains.quotations.approval_schemas import (
    QuoteApprovalAccessResult,
    QuoteApprovalApplicationReceipt,
    QuoteApprovalApplyResult,
    QuoteApprovalContext,
    QuoteApprovalDecisionSnapshot,
    QuoteApprovalFact,
    QuoteApprovalOutcome,
    QuoteApprovalSnapshot,
    QuoteApprovalSubject,
    QuoteApprovalSubmission,
    QuoteWorkflowExecutor,
    QuoteWorkflowRunFact,
)
from domains.quotations.basis_schemas import QuotePolicySnapshot
from domains.quotations.context import QuoteBusinessContext, QuoteContextProvider
from domains.quotations.errors import (
    QuotationUnavailableError,
    QuoteApprovalError,
    QuoteApprovalPermissionError,
    QuoteApprovalUnavailableError,
)
from domains.quotations.models import QuoteState
from domains.quotations.version_repository import (
    QuotationUnitOfWork,
    QuotationUowFactory,
)
from domains.quotations.version_schemas import (
    QuotationActor,
    QuoteDetailView,
    QuoteStateEvent,
)
from shared.events.catalog import QuoteApproved
from shared.schemas.identifiers import EmployeeId, QuoteId, RunId, TenantId, new_id
from shared.schemas.quote_facts import QuoteEmployeeFact, fact_utc


class QuotePolicySelection(Protocol):
    """已取得政策集合共享锁，每次current重新取当前时钟。"""

    async def current(self) -> QuotePolicySnapshot:
        """返回本次真实当前政策，不只锁历史政策行。"""
        ...


class QuoteApprovalPolicyReader(Protocol):
    """报价审批编排专用内部租约，不扩大成本HTTP读取角色。"""

    def open(
        self, tenant_id: TenantId, category: str | None
    ) -> AbstractAsyncContextManager[QuotePolicySelection]:
        """保持政策集合选择锁直到报价提交之后。"""
        ...


class QuoteWorkflowRunReader(Protocol):
    """只读取真实已持久run的中立绑定元数据。"""

    async def read(
        self, tenant_id: TenantId, run_id: RunId
    ) -> QuoteWorkflowRunFact | None:
        """缺记录返回None，不以executor自述替代真实run。"""
        ...


class QuoteActorCheck(Protocol):
    """复用服务门面现有当前员工与四成本角色检查，不复制角色矩阵。"""

    async def __call__(
        self,
        tenant_id: TenantId,
        actor: QuotationActor,
        *,
        action: Literal["prepare", "read_internal"],
    ) -> QuoteEmployeeFact:
        """当前在职/tenant/role与声明严格一致。"""
        ...


class QuoteApprovalSession(Protocol):
    """调用方持外层context，内部持报价机会锁且按需取得政策租约。"""

    async def snapshot(self) -> QuoteApprovalSnapshot:
        """读取不可变本轮安全载荷。"""
        ...

    async def submission(self) -> QuoteApprovalSubmission | None:
        """读取真实原始绑定，不拿旧state当批准。"""
        ...

    async def receipt(self) -> QuoteApprovalApplicationReceipt | None:
        """只读真实成功receipt，不依赖最新Need/政策。"""
        ...

    async def prepare_submission(
        self, context: QuoteBusinessContext, *, actor: QuotationActor
    ) -> QuoteApprovalSnapshot:
        """锁内当前政策与四角色提交检查。"""
        ...

    async def bind(
        self,
        submission: QuoteApprovalSubmission,
        context: QuoteBusinessContext,
        *,
        actor: QuotationActor,
    ) -> QuoteDetailView:
        """整组精确绑定与draft→pending同事务，不替换旧包。"""
        ...

    async def recover_submission(
        self, submission: QuoteApprovalSubmission, *, actor: QuotationActor
    ) -> QuoteDetailView:
        """只补完整已存原组并同事务终止，不取fresh context或批准。"""
        ...

    async def apply(
        self, facts: tuple[QuoteApprovalFact, ...], context: QuoteApprovalContext
    ) -> QuoteApprovalApplyResult:
        """全部独立当前决定通过后首次写报价/事件/receipt。"""
        ...

    async def terminate(
        self, facts: tuple[QuoteApprovalFact, ...]
    ) -> QuoteApprovalApplyResult:
        """拒绝/到期只关闭原轮，不读取Need或政策。"""
        ...


def _decisions(
    facts: tuple[QuoteApprovalFact, ...],
) -> tuple[QuoteApprovalDecisionSnapshot, ...]:
    """独立DTO白名单，不运行时删除state字段。"""
    return tuple(
        QuoteApprovalDecisionSnapshot(
            **{
                name: getattr(f, name)
                for name in QuoteApprovalDecisionSnapshot.model_fields
            }
        )
        for f in sorted(facts, key=lambda f: APPROVAL_TYPES.index(f.approval_type))
    )





class QuoteApprovalServiceImpl:
    """门面唯一委托实现；所有技术入口统一核真实run。"""

    def __init__(
        self,
        uows: QuotationUowFactory,
        contexts: QuoteContextProvider,
        policies: QuoteApprovalPolicyReader,
        runs: QuoteWorkflowRunReader,
        actor_check: QuoteActorCheck,
        *,
        now: Callable[[], datetime],
    ) -> None:
        """依赖必填，不提供默认身份或许可。"""
        self._uows, self._contexts, self._policies, self._runs = (
            uows,
            contexts,
            policies,
            runs,
        )
        self._actor, self._now = actor_check, now

    async def _read(self, tenant_id: TenantId, quote_id: QuoteId) -> QuoteDetailView:
        """内部bootstrap仅定位不可变机会身份。"""
        async with self._uows(tenant_id) as uow:
            quote = await uow.quotes.get(tenant_id, quote_id)
        if quote is None or quote.content.tenant_id != tenant_id:
            raise QuoteApprovalError("workflow_binding_invalid")
        return quote

    async def _run(
        self,
        tenant_id: TenantId,
        quote: QuoteDetailView,
        executor: QuoteWorkflowExecutor,
    ) -> None:
        """缺失/跨tenant/错误type/version/subject/content同样拒绝。"""
        if self._runs is None:
            raise QuoteApprovalUnavailableError("dependency_unavailable")
        try:
            run = await self._runs.read(tenant_id, executor.run_id)
        except (QuoteApprovalError, QuoteApprovalUnavailableError):
            raise
        except Exception:  # noqa: BLE001 -- run reader基础设施异常必须脱敏且不能默认许可
            raise QuoteApprovalUnavailableError("dependency_unavailable") from None
        c = quote.content
        require_quote_run_binding(tenant_id, c.quote_id, c.version, c.content_hash, run)
        if (executor.quote_id != c.quote_id or executor.workflow_type != "quote_approval"
            or run is None or run.run_id != executor.run_id):
            raise QuoteApprovalError("workflow_binding_invalid")

    async def _snapshot(
        self, uow: QuotationUnitOfWork, quote: QuoteDetailView
    ) -> QuoteApprovalSnapshot:
        """上一版本按实际version-1查找，终态后无replaces也保留历史。"""
        c = quote.content
        versions = await uow.quotes.list_versions(c.tenant_id, c.opportunity_id)
        previous = next(
            (v for v in versions if v.content.version == c.version - 1), None
        )
        return build_quote_approval_snapshot(quote, previous)

    async def approval_snapshot(
        self, tenant_id: TenantId, quote_id: QuoteId, *, actor: QuotationActor
    ) -> QuoteApprovalSnapshot:
        """仅当前四成本角色可启动；完整内部quote不进HTTP。"""
        await self._actor(tenant_id, actor, action="read_internal")
        quote = await self._read(tenant_id, quote_id)
        async with self._uows(tenant_id) as uow:
            return await self._snapshot(uow, quote)

    async def approval_target(
        self, tenant_id: TenantId, quote_id: QuoteId, *, executor: QuoteWorkflowExecutor
    ) -> QuoteApprovalSnapshot:
        """受信worker必须先证明真实run精确绑定。"""
        quote = await self._read(tenant_id, quote_id)
        await self._run(tenant_id, quote, executor)
        async with self._uows(tenant_id) as uow:
            return await self._snapshot(uow, quote)

    @asynccontextmanager
    async def open_approval_access(
        self,
        tenant_id: TenantId,
        subject: QuoteApprovalSubject,
        *,
        actor_id: EmployeeId,
        action: Literal["read", "decide"],
    ) -> AsyncIterator[QuoteApprovalAccessResult]:
        """历史access只读报价身份，决定必须绑定当前pending报价，不取Need/policy。"""
        quote = await self._read(tenant_id, subject.quote_id)
        c = quote.content
        if (
            subject.tenant_id,
            subject.quote_version,
            subject.content_hash,
            subject.opportunity_id,
            subject.prepared_by,
            subject.submitted_owner_id,
        ) != (
            tenant_id,
            c.version,
            c.content_hash,
            c.opportunity_id,
            c.prepared_by,
            c.owner_id,
        ):
            raise QuoteApprovalError("quote_contract_invalid")
        async with self._contexts.open_approval_access(
            tenant_id,
            c.opportunity_id,
            actor_id,
            prepared_by=c.prepared_by,
            submitted_owner_id=c.owner_id,
        ) as context:
            require_quote_approval_access(subject, context, action=action)
            if action == "decide":
                async with self._uows(tenant_id) as uow:
                    current = await uow.quotes.get(tenant_id, c.quote_id)
                    bindings = await uow.quotes.approval_bindings(tenant_id, c.quote_id)
                if (
                    current is None
                    or current.state is not QuoteState.PENDING_APPROVAL
                    or not any(
                        f.approval_id == subject.approval_id
                        and f.approval_type == subject.approval_type
                        and f.payload.content_hash == c.content_hash
                        for f in bindings
                    )
                ):
                    raise QuoteApprovalError("approval_binding_conflict")
            can_decide = True
            try:
                require_quote_approval_access(subject, context, action="decide")
            except QuoteApprovalPermissionError:
                can_decide = False
            yield QuoteApprovalAccessResult(
                can_decide=can_decide, current_role=context.actor.role
            )

    @asynccontextmanager
    async def open_approval(
        self, tenant_id: TenantId, quote_id: QuoteId, *, executor: QuoteWorkflowExecutor
    ) -> AsyncIterator[QuoteApprovalSession]:
        """报价先commit/rollback，再释放政策租约，最后调用方释放context。"""
        initial = await self._read(tenant_id, quote_id)
        await self._run(tenant_id, initial, executor)
        try:
            async with self._uows(tenant_id) as uow:
                await uow.quotes.lock_opportunity(
                    tenant_id, initial.content.opportunity_id
                )
                quote = await uow.quotes.get(tenant_id, quote_id, for_update=True)
                if quote is None or quote.content != initial.content:
                    raise QuoteApprovalUnavailableError("storage_inconsistent")
                session = QuoteApprovalSessionImpl(self, uow, quote, executor)
                try:
                    yield session
                    await uow.commit()
                except BaseException:
                    await uow.rollback()
                    raise
                finally:
                    await session.close()
        except QuotationUnavailableError as error:
            raise QuoteApprovalUnavailableError(error.code) from None

    async def get_approval_application(
        self, tenant_id: TenantId, quote_id: QuoteId, *, executor: QuoteWorkflowExecutor
    ) -> QuoteApprovalApplicationReceipt | None:
        """历史receipt读取仍核真实run，不追逐当前员工/Need/policy。"""
        async with self.open_approval(
            tenant_id, quote_id, executor=executor
        ) as session:
            return await session.receipt()


class QuoteApprovalSessionImpl:
    """仅服务持报价机会锁后创建；不可在离开lease后重用。"""

    def __init__(
        self,
        service: QuoteApprovalServiceImpl,
        uow: QuotationUnitOfWork,
        quote: QuoteDetailView,
        executor: QuoteWorkflowExecutor,
    ) -> None:
        """只保存此事务依赖，不建立通用事务或授权框架。"""
        self._service, self._uow, self._quote, self._executor = (
            service,
            uow,
            quote,
            executor,
        )
        self._policy_cm: AbstractAsyncContextManager[QuotePolicySelection] | None = None
        self._selection: QuotePolicySelection | None = None
        self._closed = False

    def _check(self) -> None:
        """退出会话不能继续读写旧锁保护的事实。"""
        if self._closed:
            raise QuoteApprovalError("invalid_input")

    async def close(self) -> None:
        """仅在报价commit或rollback之后关闭policy lease。"""
        self._closed = True
        if self._policy_cm is not None:
            await self._policy_cm.__aexit__(None, None, None)

    async def snapshot(self) -> QuoteApprovalSnapshot:
        """同session读取不可变载荷及实际前一版本。"""
        self._check()
        return await self._service._snapshot(self._uow, self._quote)

    async def submission(self) -> QuoteApprovalSubmission | None:
        """绑定原请求集合，不以绑定时state代替最新决定。"""
        self._check()
        c = self._quote.content
        facts = await self._uow.quotes.approval_bindings(c.tenant_id, c.quote_id)
        if not facts:
            return None
        await self._validate(facts)
        return QuoteApprovalSubmission(
            tenant_id=c.tenant_id,
            quote_id=c.quote_id,
            quote_version=c.version,
            content_hash=c.content_hash,
            policy_id=c.basis.policy_id,
            policy_hash=c.basis.policy.content_hash,
            required_types=required_quote_approvals(self._quote),
            facts=facts,
        )

    async def _validate(self, facts: tuple[QuoteApprovalFact, ...]) -> None:
        """全类型恰一包，所有安全载荷/原limit/提议run精确相同。"""
        require_quote_approval_facts(
            await self.snapshot(), facts, self._executor.run_id
        )

    async def _bound(self, facts: tuple[QuoteApprovalFact, ...]) -> None:
        """实时决定必须来自原轮相同包，不接受替换新ID或请求字段。"""
        await self._validate(facts)
        c = self._quote.content
        bindings = await self._uow.quotes.approval_bindings(c.tenant_id, c.quote_id)
        require_quote_approval_bindings(facts, bindings)

    async def receipt(self) -> QuoteApprovalApplicationReceipt | None:
        """真实成功优先，完整决定hash及run绑定仍必须一致。"""
        self._check()
        c = self._quote.content
        receipt = await self._uow.quotes.approval_receipt(c.tenant_id, c.quote_id)
        if receipt is not None:
            bindings = await self._uow.quotes.approval_bindings(c.tenant_id, c.quote_id)
            require_quote_approval_receipt(
                await self.snapshot(), receipt, bindings, self._executor.run_id
            )
        return receipt

    async def _fresh(self, context: QuoteBusinessContext) -> datetime:
        """全部锁取得后重新选择当前政策/时钟并执行T4第二道依据门。"""
        self._check()
        c = self._quote.content
        if self._selection is None:
            if self._service._policies is None:
                raise QuoteApprovalUnavailableError("dependency_unavailable")
            self._policy_cm = self._service._policies.open(
                c.tenant_id, context.category
            )
            self._selection = await self._policy_cm.__aenter__()
        policy = await self._selection.current()
        now = fact_utc(self._service._now())
        require_quote_current_basis(self._quote, context, policy, now=now)
        return now

    async def prepare_submission(
        self, context: QuoteBusinessContext, *, actor: QuotationActor
    ) -> QuoteApprovalSnapshot:
        """当前四成本角色可提交，但不能因此决定自己的包。"""
        fact = await self._service._actor(context.tenant_id, actor, action="prepare")
        if fact != context.runtime.current_actor:
            raise QuoteApprovalPermissionError("permission_denied")
        if self._quote.state is not QuoteState.DRAFT:
            raise QuoteApprovalError("approval_round_closed")
        await self._fresh(context)
        return await self.snapshot()

    async def _validate_submission(self, submission: QuoteApprovalSubmission) -> None:
        """首次绑定与原组恢复使用同一不可变身份/全类型校验。"""
        self._check()
        c = self._quote.content
        if (
            submission.tenant_id,
            submission.quote_id,
            submission.quote_version,
            submission.content_hash,
            submission.policy_id,
            submission.policy_hash,
            submission.required_types,
        ) != (
            c.tenant_id,
            c.quote_id,
            c.version,
            c.content_hash,
            c.basis.policy_id,
            c.basis.policy.content_hash,
            required_quote_approvals(self._quote),
        ):
            raise QuoteApprovalError("approval_binding_conflict")
        await self._validate(submission.facts)

    async def bind(
        self,
        submission: QuoteApprovalSubmission,
        context: QuoteBusinessContext,
        *,
        actor: QuotationActor,
    ) -> QuoteDetailView:
        """全组一次绑定；已绑定原组幂等，其他组或终轮不能替换。"""
        await self._validate_submission(submission)
        c = self._quote.content
        previous = await self.submission()
        if previous is not None:
            await self._bound(submission.facts)
            return self._quote
        await self.prepare_submission(context, actor=actor)
        now = fact_utc(self._service._now())
        await self._uow.quotes.add_approval_bindings(c.tenant_id, submission)
        await self._transition(
            QuoteState.PENDING_APPROVAL, "approval_submitted", now, actor.employee_id
        )
        return self._quote

    async def recover_submission(
        self, submission: QuoteApprovalSubmission, *, actor: QuotationActor
    ) -> QuoteDetailView:
        """已持久完整原组只补历史关联；过期/终态不复活，无批准副作用。"""
        await self._validate_submission(submission)
        c = self._quote.content
        await self._service._actor(c.tenant_id, actor, action="prepare")
        if await self.submission() is not None:
            await self._bound(submission.facts)
        else:
            await self._uow.quotes.add_approval_bindings(c.tenant_id, submission)
            if self._quote.state is QuoteState.DRAFT:
                await self._transition(
                    QuoteState.PENDING_APPROVAL, "approval_submitted",
                    fact_utc(self._service._now()), actor.employee_id,
                )
        await self.terminate(submission.facts)
        return self._quote

    async def _transition(
        self, target: QuoteState,
        reason: Literal["created", "revision", "expiry", "verified_send", "approval_submitted", "approval_approved", "approval_rejected"],
        now: datetime, actor: EmployeeId | None
    ) -> None:
        """CAS与状态事件同UoW，不改不可变内容。"""
        c = self._quote.content
        event = QuoteStateEvent(
            event_id=new_id("qse"),
            quote_id=c.quote_id,
            from_state=self._quote.state,
            to_state=target,
            actor_id=actor,
            reason=reason,
            at=now,
            reference_id=self._executor.run_id,
        )
        if not await self._uow.quotes.transition(
            c.tenant_id, c.quote_id, self._quote.state, target, event
        ):
            raise QuoteApprovalUnavailableError("storage_inconsistent")
        self._quote = self._quote.model_copy(update={"state": target})

    def _result(
        self, outcome: QuoteApprovalOutcome, receipt: QuoteApprovalApplicationReceipt | None = None
    ) -> QuoteApprovalApplyResult:
        """统一返回当前真实报价和可选成功事实。"""
        return QuoteApprovalApplyResult(
            outcome=outcome, quote=self._quote, receipt=receipt, error_code=None
        )

    async def apply(
        self, facts: tuple[QuoteApprovalFact, ...], context: QuoteApprovalContext
    ) -> QuoteApprovalApplyResult:
        """全部决定在当前权限及依据仍成立时首次原子批准。"""
        receipt = await self.receipt()
        if receipt is not None:
            if receipt.facts_hash != quote_approval_facts_hash(facts):
                raise QuoteApprovalError("approval_fact_invalid")
            return self._result("already_applied", receipt)
        await self._bound(facts)
        if any(f.state == "applied" for f in facts):
            raise QuoteApprovalUnavailableError("storage_inconsistent")
        if self._quote.state is not QuoteState.PENDING_APPROVAL:
            raise QuoteApprovalError("quote_inactive")
        require_quote_decision_states(facts, allow_applied=False)
        deciders = {e.employee_id: e for e in context.deciders}
        send = next(f for f in facts if f.approval_type == "quote_send")
        if (
            set(deciders) != {f.decided_by for f in facts}
            or context.business.runtime.current_actor.employee_id != send.decided_by
        ):
            raise QuoteApprovalError("context_changed")
        c = self._quote.content
        require_quote_current_deciders(self._quote, facts, context.business, context.deciders)
        now = await self._fresh(context.business)
        require_quote_approved_decisions(facts, now=now, allow_applied=False)
        receipt = QuoteApprovalApplicationReceipt(
            tenant_id=c.tenant_id,
            quote_id=c.quote_id,
            quote_version=c.version,
            content_hash=c.content_hash,
            facts_hash=quote_approval_facts_hash(facts),
            decisions=_decisions(facts),
            applied_at=now,
            quote_send_decider=send.decided_by,
            approval_run_id=self._executor.run_id,
        )
        await self._transition(
            QuoteState.APPROVED, "approval_approved", now, send.decided_by
        )
        await self._uow.bus.publish(
            QuoteApproved(
                tenant_id=c.tenant_id,
                occurred_at=now,
                quote_id=c.quote_id,
                approved_by=send.decided_by,
            )
        )
        await self._uow.quotes.add_approval_receipt(c.tenant_id, receipt)
        return self._result("approved", receipt)

    async def terminate(
        self, facts: tuple[QuoteApprovalFact, ...]
    ) -> QuoteApprovalApplyResult:
        """无fresh context/policy也能关闭拒绝或过期的一轮。"""
        receipt = await self.receipt()
        if receipt is not None:
            return self._result("already_applied", receipt)
        await self._bound(facts)
        state = self._quote.state
        if state in {QuoteState.REJECTED, QuoteState.EXPIRED}:
            return self._result(cast(QuoteApprovalOutcome, state.value))
        if state not in {QuoteState.DRAFT, QuoteState.PENDING_APPROVAL}:
            return self._result("obsolete")
        now = fact_utc(self._service._now())
        if any(f.state == "rejected" or f.decision == "reject" for f in facts):
            await self._transition(QuoteState.REJECTED, "approval_rejected", now, None)
            return self._result("rejected")
        if self._quote.content.valid_until <= now or any(
            f.state == "expired" or f.expires_at <= now for f in facts
        ):
            await self._transition(QuoteState.EXPIRED, "expiry", now, None)
            return self._result("expired")
        return self._result("waiting")
