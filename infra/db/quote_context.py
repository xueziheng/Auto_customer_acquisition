"""同session报价事实lease：员工→机会→Need SHARE锁，禁止外部IO与跨域仓储调用。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from pydantic import ValidationError as SchemaError
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.quotations.errors import QuoteContextError, QuoteContextUnavailableError
from domains.quotations.schemas import QuoteBusinessContext
from domains.quotations.service import (
    QuoteIssuerReader,
    canonical_quote_specification,
    quote_specification,
    quote_specification_hash,
)
from infra.db.tables import (
    EmployeeRow,
    OpportunityRow,
    ProspectAccountRow,
    ValidatedNeedRow,
)
from shared.schemas.identifiers import EmployeeId, OpportunityId, TenantId
from shared.schemas.quote_facts import (
    NeedQuoteFacts,
    QuoteEmployeeFact,
    QuoteRuntimeFacts,
    canonical_fact_hash,
)


def _storage_error(exc: SQLAlchemyError) -> QuoteContextUnavailableError:
    """固定脱敏，不泄露SQL/DSN/原始字段。"""
    code = "lock_timeout" if isinstance(exc, DBAPIError) and getattr(exc.orig, "sqlstate", None) in {
        "55P03", "57014"} else "storage_unknown"
    return QuoteContextUnavailableError(code)


class SqlAlchemyQuoteContextProvider:
    """只做查询、锁与DTO映射，业务权限及单位有效性留给调用域。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession],
                 issuer_reader: QuoteIssuerReader, *, lock_timeout_ms: int,
                 statement_timeout_ms: int) -> None:
        """资源超时显式配置，无宽松默认或隐式重试。"""
        if any(type(value) is not int or value <= 0 for value in (lock_timeout_ms, statement_timeout_ms)):
            raise QuoteContextError("invalid_input")
        self._factory, self._issuer = session_factory, issuer_reader
        self._lock_timeout, self._statement_timeout = lock_timeout_ms, statement_timeout_ms

    @asynccontextmanager
    async def open(self, tenant_id: TenantId, opportunity_id: OpportunityId, actor_id: EmployeeId,
                   *, prepared_by: EmployeeId) -> AsyncIterator[QuoteBusinessContext]:
        """锁持有到调用者提交结束；异常/取消都回滚释放，不升级为FOR UPDATE。"""
        try:
            async with self._factory() as bootstrap:
                candidate = (await bootstrap.execute(select(OpportunityRow.owner, OpportunityRow.need_id,
                    OpportunityRow.account_id).where(OpportunityRow.tenant_id == tenant_id,
                    OpportunityRow.opportunity_id == opportunity_id))).one_or_none()
            if candidate is None:
                raise QuoteContextError("record_not_found")
            if candidate.owner is None:
                raise QuoteContextError("facts_missing")
            try:
                issuer = await self._issuer.get_confirmed(tenant_id)
            except (QuoteContextError, QuoteContextUnavailableError):
                raise
            except Exception:  # noqa: BLE001 -- 外部依赖异常不得泄露原文或连接信息
                raise QuoteContextUnavailableError("dependency_unavailable") from None
            async with self._factory() as session:
                try:
                    await session.execute(text("SELECT set_config('lock_timeout', :value, true)"),
                        {"value": f"{self._lock_timeout}ms"})
                    await session.execute(text("SELECT set_config('statement_timeout', :value, true)"),
                        {"value": f"{self._statement_timeout}ms"})
                    employees: dict[str, QuoteEmployeeFact] = {}
                    for employee_id in sorted({actor_id, candidate.owner, prepared_by}):
                        row = (await session.execute(select(EmployeeRow).where(
                            EmployeeRow.tenant_id == tenant_id, EmployeeRow.employee_id == employee_id)
                            .with_for_update(read=True).execution_options(populate_existing=True))).scalar_one_or_none()
                        if row is not None:
                            employees[employee_id] = QuoteEmployeeFact(**{name: getattr(row, name)
                                for name in QuoteEmployeeFact.model_fields})
                    if actor_id not in employees or candidate.owner not in employees:
                        raise QuoteContextError("facts_missing")
                    opportunity = (await session.execute(select(OpportunityRow).where(
                        OpportunityRow.tenant_id == tenant_id, OpportunityRow.opportunity_id == opportunity_id)
                        .with_for_update(read=True).execution_options(populate_existing=True))).scalar_one_or_none()
                    if opportunity is None or (opportunity.owner, opportunity.need_id, opportunity.account_id) != tuple(candidate):
                        raise QuoteContextError("context_changed")
                    need = (await session.execute(select(ValidatedNeedRow).where(
                        ValidatedNeedRow.tenant_id == tenant_id, ValidatedNeedRow.need_id == opportunity.need_id)
                        .with_for_update(read=True).execution_options(populate_existing=True))).scalar_one_or_none()
                    account = await session.scalar(select(ProspectAccountRow.account_id).where(
                        ProspectAccountRow.tenant_id == tenant_id, ProspectAccountRow.account_id == opportunity.account_id))
                    if need is None or account is None or need.account_id != opportunity.account_id:
                        raise QuoteContextError("facts_corrupt")
                    facts = NeedQuoteFacts.model_validate_json(json.dumps({
                        name: getattr(need, name) for name in NeedQuoteFacts.model_fields}))
                    if facts.quantity is None or facts.destination is None:
                        raise QuoteContextError("facts_missing")
                    if facts.unit is None:
                        raise QuoteContextError("unit_missing")
                    spec = quote_specification(facts)
                    context = QuoteBusinessContext(tenant_id=tenant_id, opportunity_id=opportunity_id,
                        need_id=facts.need_id, account_id=facts.account_id, opportunity_state=opportunity.state,
                        owner_id=EmployeeId(opportunity.owner), prepared_by=prepared_by,
                        account_name=opportunity.account_name, country=opportunity.country,
                        category=spec.product_category, specification=canonical_quote_specification(spec),
                        unit=facts.unit.value, destination=facts.destination.value, quantity=facts.quantity.value,
                        need_facts=facts, need_facts_hash=canonical_fact_hash({"version": "need-quote-facts-v1", "facts": facts}),
                        specification_hash=quote_specification_hash(spec), issuer=issuer,
                        runtime=QuoteRuntimeFacts(current_actor=employees[actor_id], owner=employees[opportunity.owner],
                                                  preparer=employees.get(prepared_by)))
                    yield context
                finally:
                    await session.rollback()
        except SQLAlchemyError as exc:
            raise _storage_error(exc) from None
        except (SchemaError, ValueError, TypeError, KeyError):
            raise QuoteContextError("facts_corrupt") from None
