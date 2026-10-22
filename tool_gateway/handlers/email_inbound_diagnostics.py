"""容量一、task-owned 的入站失败诊断与原 Gateway 追加审计适配。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from shared.schemas.email_inbound import InboundError
from shared.schemas.email_inbound_diagnostics import InboundDiagnostic
from shared.schemas.identifiers import TenantId, UserId, new_id
from tool_gateway.errors import ToolCallStatus
from tool_gateway.repository import (
    ToolCallEventRecord,
    ToolCallId,
    ToolGatewayUnitOfWorkFactory,
)


@dataclass(frozen=True, repr=False)
class _Entry:
    owner: asyncio.Task[Any]
    diagnostic: InboundDiagnostic


class InboundDiagnosticSlot:
    def __init__(self) -> None:
        self._entry: ContextVar[_Entry | None] = ContextVar(
            "inbound_diagnostic", default=None
        )

    def take(self) -> InboundDiagnostic | None:
        entry = self._entry.get()
        if entry is None or entry.owner is not asyncio.current_task():
            return None
        self._entry.set(None)
        return entry.diagnostic

    def put(self, diagnostic: InboundDiagnostic) -> None:
        owner = asyncio.current_task()
        if owner is None or type(diagnostic) is not InboundDiagnostic:
            raise InboundError()
        # 重新校验冻结字段，永不保存异常、traceback、输入或provider引用。
        safe = InboundDiagnostic(
            diagnostic.stage,
            diagnostic.kind,
            diagnostic.protocol_status,
            diagnostic.duration_ms,
        )
        self._entry.set(_Entry(owner, safe))


class InboundDiagnosticSink(Protocol):
    async def record(self, call_id: str, diagnostic: InboundDiagnostic) -> None: ...


class GatewayInboundDiagnosticSink:
    """只追加原工具调用下的技术事件；不改调用结果、游标或重试策略。"""

    def __init__(
        self,
        tenant: TenantId,
        actor: UserId,
        factory: ToolGatewayUnitOfWorkFactory,
        *,
        now: Callable[[], datetime],
    ) -> None:
        self._tenant, self._actor, self._factory, self._now = (
            tenant,
            actor,
            factory,
            now,
        )

    async def record(self, call_id: str, diagnostic: InboundDiagnostic) -> None:
        if type(diagnostic) is not InboundDiagnostic:
            raise InboundError()
        safe = InboundDiagnostic(
            diagnostic.stage,
            diagnostic.kind,
            diagnostic.protocol_status,
            diagnostic.duration_ms,
        )
        async with self._factory(self._tenant) as uow:
            call = await uow.calls.get(self._tenant, ToolCallId(call_id))
            if (
                call is None
                or call.tenant_id != self._tenant
                or call.user_id != self._actor
                or call.tool_id != "email.inbound.fetch"
                or call.status
                not in {
                    ToolCallStatus.FAILED_PERMANENT,
                    ToolCallStatus.FAILED_TRANSIENT,
                }
            ):
                raise InboundError()
            event = ToolCallEventRecord(
                tenant_id=self._tenant,
                event_id=new_id("tce"),
                tool_call_id=ToolCallId(call_id),
                stage="inbound." + safe.stage.value,
                outcome="failed",
                rule=safe.kind.value + ":" + safe.protocol_status.value,
                category=call.error_category,
                actor_id=self._actor,
                run_id=None,
                campaign_id=None,
                message_attempt_id=None,
                occurred_at=self._now(),
                duration_ms=safe.duration_ms,
                cost_note="free",
            )
            await uow.calls.append_event(event)
