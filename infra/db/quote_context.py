"""同session报价事实lease：员工→机会→Need SHARE锁，禁止外部IO与跨域仓储调用。"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from pydantic import ValidationError as SchemaError
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.quotations.errors import (
    QuotationError,
    QuotationUnavailableError,
    QuoteContextError,
    QuoteContextUnavailableError,
)
from domains.quotations.schemas import (
    QuoteApprovalAccessContext,
    QuoteApprovalContext,
    QuoteBusinessContext,
    QuoteFileCurrentFacts,
    QuoteFileScopeFacts,
)
from domains.quotations.service import (
    QuoteIssuerReader,
    QuotePreparationFacts,
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
from shared.errors import ValidationError as DomainValidationError
from shared.schemas.identifiers import EmployeeId, OpportunityId, TenantId
from shared.schemas.quote_facts import (
    NeedQuoteFacts,
    QuoteEmployeeFact,
    QuoteRuntimeFacts,
    canonical_fact_hash,
)

logger = logging.getLogger(__name__)


def _storage_error(exc: SQLAlchemyError) -> QuoteContextUnavailableError:
    """固定脱敏，不泄露SQL/DSN/原始字段。"""
    code: Literal["lock_timeout", "storage_unknown"] = "lock_timeout" if isinstance(exc, DBAPIError) and getattr(exc.orig, "sqlstate", None) in {
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
    async def open_preparation_facts(self, tenant_id: TenantId, opportunity_id: OpportunityId,
        actor_id: EmployeeId, *, prepared_by: EmployeeId) -> AsyncIterator[QuotePreparationFacts]:
        """复用原锁序与清理；只允许真实缺项，不执行任何确认或对象IO。"""
        async with self._open(tenant_id, opportunity_id, actor_id, prepared_by=prepared_by,
            preparation=True) as result:
            assert isinstance(result, QuotePreparationFacts)
            yield result

    @asynccontextmanager
    async def open_file_scope(self, tenant_id: TenantId, opportunity_id: OpportunityId,
        actor_id: EmployeeId) -> AsyncIterator[QuoteFileScopeFacts]:
        """短scope只锁actor/owner与机会；缺失issuer不阻止历史读取。"""
        async with self._open(tenant_id, opportunity_id, actor_id,
            prepared_by=None, file_scope=True) as result:
            assert isinstance(result, QuoteFileScopeFacts)
            yield result

    @asynccontextmanager
    async def open_for_file(self, tenant_id: TenantId, opportunity_id: OpportunityId,
        actor_id: EmployeeId, *, prepared_by: EmployeeId, decider_ids: tuple[EmployeeId, ...]
    ) -> AsyncIterator[QuoteFileCurrentFacts]:
        """独立文件actor不受审批执行人的actor=decider限制。"""
        if not decider_ids:
            raise QuoteContextError("context_changed")
        async with self._open(tenant_id, opportunity_id, actor_id, prepared_by=prepared_by,
            decider_ids=decider_ids, file_current=True) as result:
            assert isinstance(result, QuoteFileCurrentFacts)
            yield result

    @asynccontextmanager
    async def open(self, tenant_id: TenantId, opportunity_id: OpportunityId, actor_id: EmployeeId,
                   *, prepared_by: EmployeeId) -> AsyncIterator[QuoteBusinessContext]:
        """保持原上下文用途与返回形状，不将审批权引入准备路径。"""
        async with self._open(tenant_id, opportunity_id, actor_id, prepared_by=prepared_by) as result:
            assert isinstance(result, QuoteBusinessContext)
            yield result

    @asynccontextmanager
    async def open_approval_access(self, tenant_id: TenantId, opportunity_id: OpportunityId,
        actor_id: EmployeeId, *, prepared_by: EmployeeId, submitted_owner_id: EmployeeId
    ) -> AsyncIterator[QuoteApprovalAccessContext]:
        """历史审批关联独立于当前Need/抬头，无外部读取。"""
        async with self._open(tenant_id, opportunity_id, actor_id, prepared_by=prepared_by,
                              submitted_owner_id=submitted_owner_id) as result:
            assert isinstance(result, QuoteApprovalAccessContext)
            yield result

    @asynccontextmanager
    async def open_for_approval(self, tenant_id: TenantId, opportunity_id: OpportunityId,
        actor_id: EmployeeId, *, prepared_by: EmployeeId, decider_ids: tuple[EmployeeId, ...]
    ) -> AsyncIterator[QuoteApprovalContext]:
        """所有真实决策人与现负责人在取得机会锁前一次锁齐。"""
        if not decider_ids or actor_id not in decider_ids:
            raise QuoteContextError("context_changed")
        async with self._open(tenant_id, opportunity_id, actor_id, prepared_by=prepared_by,
                              decider_ids=decider_ids) as result:
            assert isinstance(result, QuoteApprovalContext)
            yield result

    @asynccontextmanager
    async def _open(self, tenant_id: TenantId, opportunity_id: OpportunityId, actor_id: EmployeeId,
        *, prepared_by: EmployeeId | None, submitted_owner_id: EmployeeId | None = None,
        decider_ids: tuple[EmployeeId, ...] | None = None, file_scope: bool = False,
        file_current: bool = False, preparation: bool = False,
    ) -> AsyncIterator[QuoteBusinessContext | QuoteApprovalAccessContext | QuoteApprovalContext | QuoteFileScopeFacts | QuoteFileCurrentFacts | QuotePreparationFacts]:
        """租约内持锁，异常/取消尽力回滚关闭；不升级为FOR UPDATE。"""
        consumer_error: BaseException | None = None
        primary_cancel: asyncio.CancelledError | None = None
        first_terminal: BaseException | None = None
        try:
            async with self._factory() as bootstrap:
                try:
                    candidate = (await bootstrap.execute(select(OpportunityRow.owner, OpportunityRow.need_id,
                        OpportunityRow.account_id).where(OpportunityRow.tenant_id == tenant_id,
                        OpportunityRow.opportunity_id == opportunity_id))).one_or_none()
                except BaseException as exc:
                    if isinstance(exc, asyncio.CancelledError):
                        primary_cancel = exc
                    elif not isinstance(exc, Exception):
                        first_terminal = exc
                    raise
            if candidate is None:
                raise QuoteContextError("record_not_found")
            if candidate.owner is None:
                raise QuoteContextError("facts_missing")
            try:
                issuer = await self._issuer.get_confirmed(tenant_id) if submitted_owner_id is None and not file_scope else None
            except (QuoteContextError, QuoteContextUnavailableError):
                raise
            except QuotationError as exc:
                if preparation and exc.code == "issuer_not_found":
                    issuer = None
                elif file_current and exc.code == "issuer_not_found":
                    raise QuoteContextError("context_changed") from None
                else:
                    raise QuoteContextUnavailableError("dependency_unavailable") from None
            except QuotationUnavailableError:
                if file_current:
                    raise
                raise QuoteContextUnavailableError("dependency_unavailable") from None
            except Exception:  # noqa: BLE001 -- 外部依赖异常不得泄露原文或连接信息
                raise QuoteContextUnavailableError("dependency_unavailable") from None
            async with self._factory() as session:
                try:
                    await session.execute(text("SELECT set_config('lock_timeout', :value, true)"),
                        {"value": f"{self._lock_timeout}ms"})
                    await session.execute(text("SELECT set_config('statement_timeout', :value, true)"),
                        {"value": f"{self._statement_timeout}ms"})
                    employees: dict[str, QuoteEmployeeFact] = {}
                    for employee_id in sorted({actor_id, candidate.owner, *(decider_ids or ()),
                        *((prepared_by,) if prepared_by is not None else ())}):
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
                    if opportunity is None or opportunity.owner is None or (opportunity.owner, opportunity.need_id, opportunity.account_id) != tuple(candidate):
                        raise QuoteContextError("context_changed")
                    if decider_ids is not None and not set(decider_ids) <= employees.keys():
                        raise QuoteContextError("context_changed")
                    if file_scope:
                        try:
                            yield QuoteFileScopeFacts(tenant_id=tenant_id, opportunity_id=opportunity_id,
                                actor=employees[actor_id], owner=employees[opportunity.owner])
                        except BaseException as exc:
                            consumer_error = exc
                            raise
                        return
                    if prepared_by is None:
                        raise QuoteContextError("facts_corrupt")
                    if submitted_owner_id is not None:
                        try:
                            yield QuoteApprovalAccessContext(tenant_id=tenant_id, opportunity_id=opportunity_id,
                                actor=employees[actor_id], owner=employees[opportunity.owner],
                                prepared_by=prepared_by, submitted_owner_id=submitted_owner_id)
                        except BaseException as exc:
                            consumer_error = exc
                            raise
                        return
                    need = (await session.execute(select(ValidatedNeedRow).where(
                        ValidatedNeedRow.tenant_id == tenant_id, ValidatedNeedRow.need_id == opportunity.need_id)
                        .with_for_update(read=True).execution_options(populate_existing=True))).scalar_one_or_none()
                    account = await session.scalar(select(ProspectAccountRow.account_id).where(
                        ProspectAccountRow.tenant_id == tenant_id, ProspectAccountRow.account_id == opportunity.account_id))
                    if need is None or account is None or need.account_id != opportunity.account_id:
                        raise QuoteContextError("facts_corrupt")
                    facts = NeedQuoteFacts.model_validate_json(json.dumps({
                        name: getattr(need, name) for name in NeedQuoteFacts.model_fields}))
                    if preparation:
                        try:
                            yield QuotePreparationFacts(tenant_id=tenant_id, opportunity_id=opportunity_id,
                                account_id=facts.account_id, owner_id=EmployeeId(opportunity.owner),
                                prepared_by=prepared_by, account_name=opportunity.account_name,
                                country=opportunity.country, opportunity_state=opportunity.state,
                                need_facts=facts, issuer=issuer,
                                runtime=QuoteRuntimeFacts(current_actor=employees[actor_id],
                                    owner=employees[opportunity.owner], preparer=employees.get(prepared_by)))
                        except BaseException as exc:
                            consumer_error = exc
                            raise
                        return
                    if facts.quantity is None or facts.destination is None:
                        raise QuoteContextError("facts_missing")
                    if facts.unit is None:
                        raise QuoteContextError("unit_missing")
                    spec = quote_specification(facts)
                    if issuer is None:
                        raise QuoteContextError("facts_missing")
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
                    try:
                        if file_current and decider_ids is not None:
                            yield QuoteFileCurrentFacts(business=context,
                                deciders=tuple(employees[eid] for eid in sorted(set(decider_ids))))
                        else:
                            yield context if decider_ids is None else QuoteApprovalContext(
                                business=context, deciders=tuple(employees[eid] for eid in sorted(set(decider_ids))))
                    except BaseException as exc:
                        consumer_error = exc
                        raise
                except BaseException as exc:
                    if isinstance(exc, asyncio.CancelledError):
                        primary_cancel = exc
                    elif not isinstance(exc, Exception) and first_terminal is None:
                        first_terminal = exc
                    raise
                finally:
                    try:
                        await session.rollback()
                    except BaseException as exc:
                        if isinstance(exc, asyncio.CancelledError) and primary_cancel is None:
                            primary_cancel = exc
                        elif not isinstance(exc, (Exception, asyncio.CancelledError)) and first_terminal is None:
                            first_terminal = exc
                        raise
        except BaseException as exc:  # 后续close不能覆盖首个终止对象或原取消。
            if first_terminal is not None:
                if exc is not first_terminal:
                    logger.warning("报价上下文终止后的清理失败")
                raise first_terminal from None
            if primary_cancel is not None and isinstance(exc, (Exception, asyncio.CancelledError)):
                if exc is not primary_cancel:
                    logger.warning("报价上下文取消后的清理失败")
                raise primary_cancel from None
            if isinstance(exc, SQLAlchemyError):
                if exc is consumer_error:
                    raise
                raise _storage_error(exc) from None
            if isinstance(exc, (SchemaError, ValueError, TypeError, KeyError, DomainValidationError)):
                if exc is consumer_error or isinstance(exc, QuoteContextError):
                    raise
                raise QuoteContextError("facts_corrupt") from None
            raise
