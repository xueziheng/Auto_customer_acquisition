"""真实workflow公开读口的中立绑定投影；闭包仅由可信composition发布。"""

from collections.abc import Callable

from pydantic import ValidationError

from domains.quotations.errors import QuoteApprovalError, QuoteApprovalUnavailableError
from domains.quotations.schemas import QuoteWorkflowRunFact
from shared.schemas.identifiers import RunId, TenantId
from workflows.engine.runner import WorkflowEngine


class WorkflowQuoteRunReader:
    """只读取持久initial_context的固定绑定键，不透传run/context。"""

    def __init__(self, engine: Callable[[], WorkflowEngine | None]) -> None:
        """延迟发布engine解决构造环，未发布时不启动worker。"""
        self._engine = engine

    async def read(
        self, tenant_id: TenantId, run_id: RunId
    ) -> QuoteWorkflowRunFact | None:
        """独立MVCC读取不等待handler自身行锁，终态同样核验。"""
        engine = self._engine()
        if engine is None:
            raise QuoteApprovalUnavailableError("dependency_unavailable")
        try:
            run = await engine.get_run(tenant_id, run_id)
        except Exception:  # noqa: BLE001 -- 引擎异常不得泄漏SQL或其他租户元数据
            raise QuoteApprovalUnavailableError("storage_unknown") from None
        if run is None:
            return None
        try:
            return QuoteWorkflowRunFact.model_validate(
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
        except (ValidationError, ValueError, TypeError, AttributeError):
            raise QuoteApprovalError("workflow_binding_invalid") from None
