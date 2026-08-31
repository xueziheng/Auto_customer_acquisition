"""报价单轮流程注册、启动及真实审批事件关联；不是生产worker装配。"""

from collections.abc import Mapping
from typing import Protocol, cast

from domains.approvals.service import ApprovalService
from domains.quotations.errors import QuoteApprovalError
from domains.quotations.schemas import QuotationActor, QuoteWorkflowExecutor
from domains.quotations.service import QuotationVersionService
from shared.errors import TransientError
from shared.events.bus import EventHandler
from shared.events.catalog import ApprovalDecided, DomainEvent
from shared.schemas.identifiers import ApprovalId, QuoteId, RunId, TenantId
from workflows.engine.runner import (
    StepDefinition,
    StepHandler,
    StepStatus,
    WorkflowDefinition,
    WorkflowEngine,
)
from workflows.quote_approval.application import QuoteApprovalApplication
from workflows.quote_approval.approvals import read_quote_facts
from workflows.quote_approval.run_reader import WorkflowQuoteRunReader
from workflows.quote_approval.steps import QuoteApprovalNotifier, QuoteApprovalStep


class OutboxHandlerRegistry(Protocol):
    """复用既有注册结构，不依赖其他workflow内部实现。"""

    def register_handler(
        self,
        event_type: type[DomainEvent],
        handler_name: str,
        handler: EventHandler[DomainEvent],
    ) -> None:
        """由受信composition将metadata事件交给固定handler。"""
        ...


def build_quote_approval_definition() -> WorkflowDefinition:
    """wait进入即poll；到期经apply的poll/terminate关闭，绝无默认批准。"""
    names = (
        "assemble",
        "submit",
        "wait",
        "apply",
        "mark_applied",
        "notify",
        "complete",
    )
    return WorkflowDefinition(
        "quote_approval",
        1,
        tuple(
            StepDefinition(
                name,
                f"quote_approval.{name}",
                timeout_context_key="approval_timeout_seconds",
                on_timeout="apply",
                wait_event_type="ApprovalDecided",
                run_on_entry=True,
            )
            if name == "wait"
            else StepDefinition(name, f"quote_approval.{name}")
            for name in names
        ),
        {
            "assemble": ("submit",),
            "submit": ("wait",),
            "wait": ("apply", "notify"),
            "apply": ("mark_applied", "notify", "wait"),
            "mark_applied": ("notify",),
            "notify": ("complete",),
            "complete": (),
        },
    )


def build_quote_approval_handlers(
    application: QuoteApprovalApplication, notifier: QuoteApprovalNotifier
) -> Mapping[str, StepHandler]:
    """只绑定七个本流程步骤，不建立生产默认依赖。"""
    return {
        f"quote_approval.{name}": QuoteApprovalStep(name, application, notifier)
        for name in (
            "assemble",
            "submit",
            "wait",
            "apply",
            "mark_applied",
            "notify",
            "complete",
        )
    }


async def start_quote_approval(
    engine: WorkflowEngine,
    quotations: QuotationVersionService,
    tenant_id: TenantId,
    quote_id: QuoteId,
    *,
    actor: QuotationActor,
) -> RunId:
    """先当前四角色快照；start同key返回的真实run也再次统一核验。"""
    snapshot = await quotations.approval_snapshot(tenant_id, quote_id, actor=actor)
    c = snapshot.internal_quote.content
    run_id = await engine.start(
        tenant_id,
        "quote_approval",
        str(quote_id),
        {
            "quote_id": str(quote_id),
            "quote_version": c.version,
            "content_hash": c.content_hash,
            "prepared_by": str(c.prepared_by),
            "initiated_by": str(actor.employee_id),
        },
        f"quote-approval:{tenant_id}:{quote_id}:{c.content_hash}",
    )
    await quotations.approval_target(
        tenant_id,
        quote_id,
        executor=QuoteWorkflowExecutor(
            workflow_type="quote_approval", run_id=run_id, quote_id=quote_id
        ),
    )
    return run_id


class ApprovalDecidedHandler:
    """event只有ID唤醒意义，决定与run归属来自真实持久包。"""

    def __init__(self, engine: WorkflowEngine, approvals: ApprovalService) -> None:
        """只注入实际公共服务，不接受event授权自证。"""
        self._engine, self._approvals = engine, approvals

    async def handle(self, event: ApprovalDecided) -> None:
        """旧包忽略，新包精确绑定原run，旧版本绝不唤醒新run。"""
        approval_id = ApprovalId(event.approval_id)
        raw = await self._approvals.read_fact(event.tenant_id, approval_id)
        if raw.contract_namespace is None:
            return
        (fact,) = await read_quote_facts(
            self._approvals, event.tenant_id, (approval_id,)
        )
        if fact.proposed_by_run is None:
            raise QuoteApprovalError("workflow_binding_invalid")
        binding = await WorkflowQuoteRunReader(lambda: self._engine).read(
            event.tenant_id, fact.proposed_by_run
        )
        p = fact.payload
        if binding is None or (
            binding.tenant_id,
            binding.run_id,
            binding.workflow_type,
            binding.workflow_version,
            binding.subject_ref,
            binding.quote_version,
            binding.content_hash,
        ) != (
            p.tenant_id,
            fact.proposed_by_run,
            "quote_approval",
            1,
            str(p.quote_id),
            p.quote_version,
            p.content_hash,
        ):
            raise QuoteApprovalError("workflow_binding_invalid")
        run = await self._engine.get_run(event.tenant_id, fact.proposed_by_run)
        if run is None:
            raise QuoteApprovalError("workflow_binding_invalid")
        if run.status in {
            StepStatus.COMPLETED,
            StepStatus.FAILED,
            StepStatus.CANCELLED,
        } or run.current_step in {"apply", "mark_applied", "notify", "complete"}:
            return
        accepted = await self._engine.deliver_event(
            event.tenant_id,
            fact.proposed_by_run,
            "ApprovalDecided",
            {"approval_id": str(fact.approval_id)},
        )
        if not accepted:
            raise TransientError("报价审批流程尚未进入等待")


def register_quote_approval(
    engine: WorkflowEngine, registry: OutboxHandlerRegistry, approvals: ApprovalService
) -> None:
    """流程与metadata事件注册；生产composition由T8调用。"""
    engine.register(build_quote_approval_definition())
    registry.register_handler(
        ApprovalDecided,
        "quote_approval.approval_decided",
        cast(EventHandler[DomainEvent], ApprovalDecidedHandler(engine, approvals)),
    )


__all__ = (
    "QuoteApprovalNotifier",
    "build_quote_approval_definition",
    "build_quote_approval_handlers",
    "register_quote_approval",
    "start_quote_approval",
)
