"""文件生成的tenant事件预算；独立短事务，无内存后备或新fencing。"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from infra.db.tables import ToolCallEventRow, ToolCallRow
from shared.errors import ValidationError
from shared.schemas.identifiers import TenantId
from tool_gateway.file_rate_limit import (
    ULID,
    QuoteFileRateDecision,
    QuoteFileRateError,
    QuoteFileRateLimits,
    QuoteFileRateRequest,
)
from tool_gateway.repository import ToolCallEventRecord


def _input(tenant: TenantId, request: QuoteFileRateRequest) -> None:
    if (
        type(tenant) is not str
        or re.fullmatch(rf"tn_{ULID}", tenant) is None
        or not isinstance(request, QuoteFileRateRequest)
    ):
        raise QuoteFileRateError("invalid_input")


def _binding(
    row: ToolCallRow | None,
    tenant: TenantId,
    request: QuoteFileRateRequest,
    *,
    status: str,
) -> ToolCallRow:
    """真实canonical绑定，actor可与最初received员工不同。"""
    if row is None or (
        row.tenant_id,
        row.tool_call_id,
        row.tool_id,
        row.tool_version,
        row.idempotency_key,
        row.request_fingerprint,
        row.fingerprint_version,
        row.status,
    ) != (
        tenant,
        request.canonical_call_id,
        "quotation.file.generate",
        request.tool_version,
        request.idempotency_key,
        request.request_fingerprint,
        request.fingerprint_version,
        status,
    ):
        raise QuoteFileRateError("claim_invalid")
    return row


class PostgresQuoteFileGenerationRateLimiter:
    """同tenant一个短advisory锁，只锁本canonical，不持任何业务锁。"""

    def __init__(
        self,
        factory: Callable[[], AsyncSession],
        *,
        limits: QuoteFileRateLimits,
        lease_owner: str,
        id_generator: Callable[[str], str],
    ) -> None:
        if (
            not isinstance(limits, QuoteFileRateLimits)
            or type(lease_owner) is not str
            or not lease_owner
        ):
            raise QuoteFileRateError("invalid_input")
        self._factory, self._limits, self._owner, self._id = (
            factory,
            limits,
            lease_owner,
            id_generator,
        )

    async def reserve(
        self, tenant_id: TenantId, request: QuoteFileRateRequest
    ) -> QuoteFileRateDecision:
        """commit与close皆确定成功才返回reserved，未知事件不退款也不重试。"""
        _input(tenant_id, request)
        committing = False
        try:
            async with self._factory() as session, session.begin():
                await session.execute(
                    text("SELECT set_config('lock_timeout', :value, true)"),
                    {"value": f"{self._limits.lock_timeout_ms}ms"},
                )
                await session.execute(
                    text("SELECT set_config('statement_timeout', :value, true)"),
                    {"value": f"{self._limits.statement_timeout_ms}ms"},
                )
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": f"quote-file-rate-v1:{tenant_id}"},
                )
                row = await session.scalar(
                    select(ToolCallRow)
                    .where(
                        ToolCallRow.tenant_id == tenant_id,
                        ToolCallRow.tool_call_id == request.canonical_call_id,
                    )
                    .with_for_update()
                )
                now: datetime = await session.scalar(text("SELECT clock_timestamp()"))
                row = _binding(row, tenant_id, request, status="claimed")
                if (
                    row.attempt_count < 1
                    or row.lease_owner != self._owner
                    or row.lease_expires_at is None
                    or row.lease_expires_at <= now
                ):
                    raise QuoteFileRateError("claim_invalid")
                nth = await session.scalar(
                    select(ToolCallEventRow.occurred_at)
                    .join(
                        ToolCallRow,
                        (ToolCallRow.tenant_id == ToolCallEventRow.tenant_id)
                        & (ToolCallRow.tool_call_id == ToolCallEventRow.tool_call_id),
                    )
                    .where(
                        ToolCallRow.tenant_id == tenant_id,
                        ToolCallEventRow.tenant_id == tenant_id,
                        ToolCallRow.tool_id == "quotation.file.generate",
                        ToolCallEventRow.stage == "rate_limit",
                        ToolCallEventRow.outcome == "reserved",
                        ToolCallEventRow.rule == "quote-file-generate-v1",
                        ToolCallEventRow.occurred_at
                        > now - timedelta(seconds=self._limits.window_seconds),
                    )
                    .order_by(
                        ToolCallEventRow.occurred_at.desc(),
                        ToolCallEventRow.event_id.desc(),
                    )
                    .offset(self._limits.maximum_admissions - 1)
                    .limit(1)
                )
                if nth is not None:
                    retry = math.ceil(
                        (
                            max(
                                nth + timedelta(seconds=self._limits.window_seconds),
                                row.lease_expires_at,
                            )
                            - now
                        ).total_seconds()
                    )
                    decision = QuoteFileRateDecision(
                        outcome="limited",
                        reservation_event_id=None,
                        retry_after_seconds=max(1, min(86400, retry)),
                    )
                else:
                    record = ToolCallEventRecord(
                        tenant_id=tenant_id,
                        event_id=self._id("tce"),
                        tool_call_id=request.canonical_call_id,
                        stage="rate_limit",
                        outcome="reserved",
                        rule="quote-file-generate-v1",
                        category=None,
                        actor_id=request.actor_id,
                        run_id=None,
                        campaign_id=None,
                        message_attempt_id=None,
                        occurred_at=now,
                        duration_ms=0,
                        cost_note=f"claim_attempt:{row.attempt_count}",
                    )
                    session.add(ToolCallEventRow(**asdict(record)))
                    await session.flush()
                    decision = QuoteFileRateDecision(
                        outcome="reserved",
                        reservation_event_id=record.event_id,
                        retry_after_seconds=None,
                    )
                committing = True
            return decision
        except QuoteFileRateError:
            raise
        except ValidationError:
            raise QuoteFileRateError(
                "commit_unknown" if committing else "invalid_input"
            ) from None
        except Exception:  # noqa: BLE001 -- 不泄露SQL/连接信息，也不自动重试预留
            raise QuoteFileRateError(
                "commit_unknown" if committing else "storage_unavailable"
            ) from None


class PostgresQuoteFileExecutionHistoryReader:
    """执行事件历史才是禁重生成依据，不读取可被覆盖的error_category。"""

    def __init__(
        self, factory: Callable[[], AsyncSession], *, statement_timeout_ms: int
    ) -> None:
        if type(statement_timeout_ms) is not int or statement_timeout_ms <= 0:
            raise QuoteFileRateError("invalid_input")
        self._factory, self._timeout = factory, statement_timeout_ms

    async def has_prior_execution(
        self, tenant_id: TenantId, request: QuoteFileRateRequest
    ) -> bool:
        """当前mark_executing已提交；恰一条才可能首次，零条一律拒绝。"""
        _input(tenant_id, request)
        try:
            async with self._factory() as session, session.begin():
                await session.execute(
                    text("SELECT set_config('statement_timeout', :value, true)"),
                    {"value": f"{self._timeout}ms"},
                )
                row = await session.scalar(
                    select(ToolCallRow).where(
                        ToolCallRow.tenant_id == tenant_id,
                        ToolCallRow.tool_call_id == request.canonical_call_id,
                    )
                )
                _binding(row, tenant_id, request, status="executing")
                events = (
                    await session.scalars(
                        select(ToolCallEventRow.event_id)
                        .where(
                            ToolCallEventRow.tenant_id == tenant_id,
                            ToolCallEventRow.tool_call_id == request.canonical_call_id,
                            ToolCallEventRow.stage == "ledger",
                            ToolCallEventRow.outcome == "executing",
                        )
                        .limit(2)
                    )
                ).all()
                if not events:
                    raise QuoteFileRateError("claim_invalid")
            return len(events) >= 2
        except QuoteFileRateError:
            raise
        except Exception:  # noqa: BLE001 -- 读取故障不能伪造首次执行
            raise QuoteFileRateError("storage_unavailable") from None
