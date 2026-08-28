"""单位用途事实租约：Employee→Opportunity SHARE，Need只读不提前取锁。"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from pydantic import ValidationError as SchemaError
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.demand.service import (
    NeedUnitError,
    NeedUnitPermissionError,
    NeedUnitScopeFacts,
    NeedUnitUnavailableError,
)
from infra.db.tables import EmployeeRow, OpportunityRow, ValidatedNeedRow
from shared.schemas.identifiers import (
    EmployeeId,
    OpportunityId,
    ProspectAccountId,
    TenantId,
    ValidatedNeedId,
)
from shared.schemas.quote_facts import QuoteEmployeeFact, fact_identity


class SqlAlchemyNeedUnitScopeReader:
    """只实施存储结构绑定与锁，不复制成本或机会业务权限。"""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        lock_timeout_ms: int,
        statement_timeout_ms: int,
    ) -> None:
        if any(
            type(v) is not int or not 0 < v <= 2147483647
            for v in (lock_timeout_ms, statement_timeout_ms)
        ):
            raise NeedUnitError("invalid_input")
        self._factory, self._lock_timeout, self._statement_timeout = (
            factory,
            lock_timeout_ms,
            statement_timeout_ms,
        )

    @asynccontextmanager
    async def open(
        self, tenant_id: TenantId, need_id: ValidatedNeedId, actor_id: EmployeeId
    ) -> AsyncIterator[NeedUnitScopeFacts]:
        """独立尽力rollback/close，取消与原业务异常不变成事实缺项。"""
        try:
            for identity in (tenant_id, need_id, actor_id):
                fact_identity(identity)
        except ValueError:
            raise NeedUnitError("invalid_input") from None
        session = self._factory()
        failure: BaseException | None = None
        consumer_error: BaseException | None = None
        try:
            await session.execute(
                text("SELECT set_config('lock_timeout', :value, true)"),
                {"value": f"{self._lock_timeout}ms"},
            )
            await session.execute(
                text("SELECT set_config('statement_timeout', :value, true)"),
                {"value": f"{self._statement_timeout}ms"},
            )
            actor = await session.scalar(
                select(EmployeeRow)
                .where(
                    EmployeeRow.tenant_id == tenant_id,
                    EmployeeRow.employee_id == actor_id,
                )
                .with_for_update(read=True)
            )
            if actor is None:
                raise NeedUnitPermissionError("permission_denied")
            opportunity = await session.scalar(
                select(OpportunityRow)
                .where(
                    OpportunityRow.tenant_id == tenant_id,
                    OpportunityRow.need_id == need_id,
                )
                .with_for_update(read=True)
            )
            account_id = await session.scalar(
                select(ValidatedNeedRow.account_id).where(
                    ValidatedNeedRow.tenant_id == tenant_id,
                    ValidatedNeedRow.need_id == need_id,
                )
            )
            if opportunity is None or account_id is None:
                raise NeedUnitError("need_not_found")
            if account_id != opportunity.account_id:
                raise NeedUnitError("facts_corrupt")
            facts = NeedUnitScopeFacts(
                tenant_id=tenant_id,
                need_id=need_id,
                opportunity_id=OpportunityId(opportunity.opportunity_id),
                account_id=ProspectAccountId(account_id),
                actor=QuoteEmployeeFact(
                    **{
                        name: getattr(actor, name)
                        for name in QuoteEmployeeFact.model_fields
                    }
                ),
            )
            try:
                yield facts
            except BaseException as exc:
                consumer_error = exc
                raise
        except BaseException as exc:  # noqa: BLE001 - 清理后保留原取消/业务错误
            failure = exc
        finally:
            for cleanup in (session.rollback, session.close):
                try:
                    await cleanup()
                except BaseException as exc:  # noqa: BLE001 - rollback失败仍尝试close
                    if (
                        failure is None
                        or (
                            isinstance(failure, Exception)
                            and not isinstance(exc, Exception)
                        )
                        or isinstance(failure, asyncio.CancelledError)
                        and not isinstance(exc, (Exception, asyncio.CancelledError))
                    ):
                        failure = exc
        if failure is None:
            return
        if failure is consumer_error:
            raise failure
        if isinstance(failure, SQLAlchemyError):
            code: Literal["lock_timeout", "storage_unknown"] = (
                "lock_timeout"
                if isinstance(failure, DBAPIError)
                and getattr(failure.orig, "sqlstate", None) in {"55P03", "57014"}
                else "storage_unknown"
            )
            raise NeedUnitUnavailableError(code) from None
        if isinstance(failure, (SchemaError, TypeError, ValueError)):
            raise NeedUnitError("facts_corrupt") from None
        raise failure
