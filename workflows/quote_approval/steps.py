"""报价单轮步骤：只保存元数据，事件或run上下文不能替代批准事实。"""

from typing import Any, Protocol, cast

from domains.quotations.errors import QuoteApprovalError, QuoteApprovalUnavailableError
from domains.quotations.schemas import (
    QuoteApprovalOutcome,
    QuoteWorkflowExecutor,
    QuoteWorkflowRunFact,
)
from shared.errors import TransientError
from shared.schemas.identifiers import EmployeeId, QuoteId, RunId, TenantId
from shared.schemas.quote_facts import fact_identity
from workflows.engine.runner import WorkflowRun
from workflows.quote_approval.application import QuoteApprovalApplication


class QuoteApprovalNotifier(Protocol):
    """受控通知出口仅收元数据，真实通知适配由T8装配。"""

    async def notify(
        self,
        tenant_id: TenantId,
        quote_id: QuoteId,
        *,
        run_id: RunId,
        recipient_id: EmployeeId,
        outcome: QuoteApprovalOutcome,
        idempotency_key: str,
    ) -> None:
        """同key幂等通知，禁止自动发客户价格或正文。"""
        ...


def _identity(run: WorkflowRun) -> tuple[QuoteWorkflowExecutor, EmployeeId, EmployeeId]:
    """固定绑定键只读，不能由任何step patch重写。"""
    from pydantic import ValidationError

    try:
        fact = QuoteWorkflowRunFact.model_validate(
            {
                "tenant_id": run.tenant_id,
                "run_id": run.run_id,
                "workflow_type": run.workflow_type,
                "workflow_version": run.workflow_version,
                "subject_ref": run.subject_ref,
                "quote_version": run.context.get("quote_version"),
                "content_hash": run.context.get("content_hash"),
            }
        )
        if (
            fact.workflow_type != "quote_approval"
            or fact.workflow_version != 1
            or run.context.get("quote_id") != run.subject_ref
        ):
            raise ValueError
        employees = tuple(run.context.get(k) for k in ("prepared_by", "initiated_by"))
        for employee in employees:
            fact_identity(employee)
        return (
            QuoteWorkflowExecutor(
                workflow_type="quote_approval",
                run_id=run.run_id,
                quote_id=QuoteId(run.subject_ref),
            ),
            EmployeeId(cast(str, employees[0])),
            EmployeeId(cast(str, employees[1])),
        )
    except (ValueError, TypeError, ValidationError):
        raise QuoteApprovalError("workflow_binding_invalid") from None


class QuoteApprovalStep:
    """单流程七个固定阶段，不扩展成通用步骤或授权框架。"""

    def __init__(
        self,
        name: str,
        application: QuoteApprovalApplication,
        notifier: QuoteApprovalNotifier,
    ) -> None:
        """构造只由固定handler工厂调用。"""
        self._name, self._application, self._notifier = name, application, notifier

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        """技术不可用留可重试；业务固定阻断不转批准。"""
        try:
            return await self._execute(run)
        except QuoteApprovalUnavailableError as error:
            if error.code != "storage_inconsistent":
                raise TransientError("报价审批依赖暂不可用") from None
            return "fail", "storage_inconsistent", {"outcome": "blocked"}
        except QuoteApprovalError as error:
            return "fail", error.code, {"outcome": "blocked", "error_code": error.code}

    async def _execute(
        self, run: WorkflowRun
    ) -> tuple[str, str | None, dict[str, Any]]:
        """每步重读真实服务；上下文只有业务关联、期限和固定状态码。"""
        executor, prepared_by, initiated_by = _identity(run)
        tenant, quote_id = run.tenant_id, executor.quote_id
        app = self._application
        if self._name == "assemble":
            await app.poll(tenant, quote_id, executor=executor)
            return "advance", "submit", {"approval_run_id": str(run.run_id)}
        if self._name == "submit":
            submission = await app.submit(
                tenant, quote_id, initiated_by=initiated_by, executor=executor
            )
            deadline = min(f.expires_at for f in submission.facts)
            return (
                "advance",
                "wait",
                {
                    "approval_ids": [str(f.approval_id) for f in submission.facts],
                    "approval_types": list(submission.required_types),
                    "approval_deadline": deadline.isoformat(),
                    "approval_timeout_seconds": app.remaining_wait_seconds(deadline),
                },
            )
        if self._name == "wait":
            result = await app.poll(tenant, quote_id, executor=executor)
            patch = {
                "outcome": result.outcome,
                "approval_timeout_seconds": app.remaining_wait_seconds(result.deadline),
            }
            if result.outcome == "waiting":
                return "wait", None, patch
            if result.outcome in {"ready", "already_applied"}:
                return "advance", "apply", patch
            if result.outcome == "blocked":
                return "fail", result.error_code or "approval_fact_invalid", patch
            return "advance", "notify", patch
        if self._name == "apply":
            apply_result = await app.apply(tenant, quote_id, executor=executor)
            apply_patch = {"outcome": apply_result.outcome}
            if apply_result.outcome in {"approved", "already_applied"}:
                return "advance", "mark_applied", apply_patch
            if apply_result.outcome == "blocked":
                return (
                    "fail",
                    apply_result.error_code or "approval_fact_invalid",
                    apply_patch,
                )
            if apply_result.outcome == "waiting":
                return "advance", "wait", apply_patch
            return "advance", "notify", apply_patch
        if self._name == "mark_applied":
            await app.mark_completed(tenant, quote_id, executor=executor)
            return "advance", "notify", {"outcome": "approved"}
        if self._name == "notify":
            outcome = run.context.get("outcome")
            if outcome not in {"approved", "rejected", "expired", "obsolete"}:
                raise QuoteApprovalError("workflow_binding_invalid")
            await self._notifier.notify(
                tenant,
                quote_id,
                run_id=run.run_id,
                recipient_id=prepared_by,
                outcome=outcome,
                idempotency_key=f"quote-approval-notify:{run.run_id}:{outcome}",
            )
            return "advance", "complete", {}
        if self._name == "complete":
            return "complete", None, {}
        raise QuoteApprovalError("workflow_binding_invalid")
