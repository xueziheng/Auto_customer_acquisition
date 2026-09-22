"""Run 只读摘要：沿现有审计服务当前角色权限，不暴露 workflow context。"""

from agent_runtime.assistant.reads import CurrentAssistantIdentity, fragment
from domains.assistant.schemas import (
    AssistantActor,
    AssistantReadQuery,
    AuthorizedFragment,
    ObjectRef,
)
from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import RunId
from workflows.engine.audit import RunAuditActor, RunAuditService


class AssistantRunReads:
    def __init__(
        self, identity: CurrentAssistantIdentity, service: RunAuditService
    ) -> None:
        self._identity, self._service = identity, service

    async def read(self, actor: AssistantActor, ref: ObjectRef) -> AuthorizedFragment:
        role, _ = await self._identity.resolve(actor)
        if ref.kind != "run":
            raise ValidationError("运行引用类型无效")
        view = await self._service.get_run(
            actor.tenant_id,
            RunId(ref.object_id),
            actor=RunAuditActor(str(actor.employee_id), role),
        )
        if view is None:
            raise PermissionDenied("运行记录不可见")
        return fragment(
            ref,
            {
                "run_id": str(view.summary.run_id),
                "workflow_type": view.summary.workflow_type,
                "status": view.summary.status,
                "current_step": view.summary.current_step,
            },
        )

    async def list(
        self, actor: AssistantActor, query: AssistantReadQuery
    ) -> tuple[AuthorizedFragment, ...]:
        role, _ = await self._identity.resolve(actor)
        if query.kind != "run" or query.cursor is not None:
            raise ValidationError("运行查询无效")
        rows = await self._service.list_runs(
            actor.tenant_id,
            actor=RunAuditActor(str(actor.employee_id), role),
            workflow_type=None,
            status=None,
            limit=query.limit,
        )
        return tuple(
            [
                await self.read(actor, ObjectRef(kind="run", object_id=str(row.run_id)))
                for row in rows
            ]
        )
