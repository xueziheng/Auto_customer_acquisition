"""Sourcing V2 公开搜索回执、恢复与草稿的受信持久边界。"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable
from datetime import datetime
from hashlib import sha256
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_runtime.sourcing_agent import SourcingPageCandidateDraft
from connectors.search_contracts import SearchResult
from domains.sourcing.schemas import (
    PublicCandidateDraft,
    PublicCandidateDraftPriceTier,
    PublicCandidateDraftSpec,
)
from domains.sourcing.service import (
    PublicPlanStatus,
    PublicSourcingPlan,
    SourcingSearchExecution,
    SourcingSearchExecutionStatus,
)
from infra.db.repositories.sourcing import (
    PublicSourcingPlanRepositoryImpl,
    SourcingCaseRepositoryImpl,
    SourcingSearchExecutionRepositoryImpl,
)
from infra.db.sourcing_uow import SqlAlchemySourcingUnitOfWork
from infra.db.tables import RawArtifactRow, SourcingPageAttemptRow, WorkflowRunRow
from shared.errors import ValidationError
from shared.schemas.identifiers import (
    RunId,
    SourcingCaseId,
    SourcingPlanId,
    TenantId,
)
from tool_gateway.handlers.web_slots import SearchResultBatch, WebSearchResultSlot
from workflows.engine.runner import StepStatus
from workflows.sourcing_case.steps import sourcing_search_request_key


def _query_hash(query_text: str) -> str:
    return sha256(query_text.encode("utf-8")).hexdigest()


def _safe_locator_url(value: object) -> str:
    if not isinstance(value, str):
        raise ValidationError("公开寻源 locator 回执无效")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValidationError("公开寻源 locator 回执无效") from None
    try:
        address = ipaddress.ip_address(parsed.hostname) if parsed.hostname else None
    except ValueError:
        address = None
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65_535)
        or parsed.hostname.casefold() in {"localhost", "localhost.localdomain"}
        or parsed.hostname.casefold().endswith(".local")
        or address is not None
        and not address.is_global
    ):
        raise ValidationError("公开寻源 locator 回执无效")
    return value


def sourcing_candidate_draft_source_key(
    *,
    tenant_id: TenantId,
    case_id: SourcingCaseId,
    run_id: RunId,
    plan_hash: str,
    query_index: int,
    result_index: int,
    evidence_artifact_ref: str,
) -> str:
    """对页面在已授权 Run 中的精确位置生成稳定源键。"""

    payload = "\0".join(
        (
            "tradeos:sourcing-draft:v1",
            str(tenant_id),
            str(case_id),
            str(run_id),
            plan_hash,
            str(query_index),
            str(result_index),
            evidence_artifact_ref,
        )
    )
    return sha256(payload.encode("utf-8")).hexdigest()


class PostgresSourcingWebPersistence:
    """同一窄适配器完成授权绑定、locator 回执与恢复。"""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        tenant_id: TenantId,
        slot: WebSearchResultSlot,
        *,
        now: Callable[[], datetime],
    ) -> None:
        self._factory = factory
        self._tenant_id = tenant_id
        self._slot = slot
        self._now = now

    async def _load(
        self,
        session: AsyncSession,
        *,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        run_id: RunId,
        plan_id: SourcingPlanId,
        plan_hash: str,
    ) -> PublicSourcingPlan:
        if tenant_id != self._tenant_id:
            raise ValidationError("公开寻源租户绑定无效")
        cases = SourcingCaseRepositoryImpl(session, tenant_id)
        plans = PublicSourcingPlanRepositoryImpl(session, tenant_id)
        case = await cases.get(tenant_id, case_id)
        plan = await plans.get(tenant_id, plan_id)
        run = (
            await session.execute(
                select(WorkflowRunRow).where(
                    WorkflowRunRow.tenant_id == str(tenant_id),
                    WorkflowRunRow.run_id == str(run_id),
                )
            )
        ).scalar_one_or_none()
        if (
            case is None
            or plan is None
            or run is None
            or case.state.value != "verifying"
            or case.active_search_plan_id != plan_id
            or plan.case_id != case_id
            or plan.status is not PublicPlanStatus.RUNNING
            or plan.plan_hash != plan_hash
            or plan.authorized_plan_hash != plan_hash
            or run.workflow_type != "sourcing_case"
            or run.workflow_version != 2
            or run.subject_ref != str(case_id)
            or run.current_step != "public_search"
            or run.status != StepStatus.RUNNING.value
            or run.context.get("sourcing_plan_id") != str(plan_id)
            or run.context.get("sourcing_plan_hash") != plan_hash
        ):
            raise ValidationError("公开寻源计划与 Run 绑定无效")
        return plan

    async def load_authorized(self, **binding: object) -> PublicSourcingPlan:
        async with self._factory() as session, session.begin():
            return await self._load(session, **binding)  # type: ignore[arg-type]

    async def restore(
        self, *, tenant_id: TenantId, run_id: RunId, plan_hash: str, query_index: int
    ) -> SearchResultBatch | None:
        async with self._factory() as session, session.begin():
            execution_repo = SourcingSearchExecutionRepositoryImpl(session, tenant_id)
            request_key = sourcing_search_request_key(plan_hash, query_index)
            execution = await execution_repo.get_by_request_key(tenant_id, request_key)
            if execution is None:
                return None
            plan = await self._load(
                session,
                tenant_id=tenant_id,
                case_id=execution.case_id,
                run_id=run_id,
                plan_id=execution.plan_id,
                plan_hash=plan_hash,
            )
            if (
                execution.case_id != plan.case_id
                or execution.plan_id != plan.plan_id
                or execution.plan_hash != plan_hash
                or execution.run_id != run_id
                or execution.query_index != query_index
                or execution.request_key != request_key
                or query_index >= len(plan.queries)
                or execution.query_hash
                != _query_hash(plan.queries[query_index].query_text)
            ):
                raise ValidationError("公开寻源回执绑定无效")
            if execution.provider_status is SourcingSearchExecutionStatus.UNCERTAIN:
                if execution.locator_results or execution.completed_at is not None:
                    raise ValidationError("公开寻源回执状态无效")
                return None
            if execution.provider_status not in {
                SourcingSearchExecutionStatus.SUCCEEDED,
                SourcingSearchExecutionStatus.NO_RESULTS,
            }:
                raise ValidationError("公开寻源回执状态无效")
            locators: list[SearchResult] = []
            if (
                execution.provider_status is SourcingSearchExecutionStatus.NO_RESULTS
                and execution.locator_results
            ) or (
                execution.provider_status is SourcingSearchExecutionStatus.SUCCEEDED
                and not execution.locator_results
            ):
                raise ValidationError("公开寻源回执状态无效")
            for locator in execution.locator_results:
                if not isinstance(locator, dict) or set(locator) != {
                    "title",
                    "url",
                    "description",
                }:
                    raise ValidationError("公开寻源 locator 回执无效")
                title = locator["title"]
                url = locator["url"]
                description = locator["description"]
                if not all(isinstance(item, str) for item in (title, url, description)):
                    raise ValidationError("公开寻源 locator 回执无效")
                assert isinstance(title, str)
                assert isinstance(url, str)
                assert isinstance(description, str)
                try:
                    locators.append(
                        SearchResult(
                            title,
                            _safe_locator_url(url),
                            description,
                        )
                    )
                except (TypeError, ValueError):
                    raise ValidationError("公开寻源 locator 回执无效") from None
            query = plan.queries[query_index]
            return self._slot.put(
                tenant_id,
                query.target_country,
                plan.product_category,
                tuple(locators),
            )

    async def count_page_attempts(self, **values: object) -> int:
        tenant_id = TenantId(str(values["tenant_id"]))
        case_id = SourcingCaseId(str(values["case_id"]))
        run_id = RunId(str(values["run_id"]))
        plan_id = SourcingPlanId(str(values["plan_id"]))
        plan_hash = str(values["plan_hash"])
        async with self._factory() as session, session.begin():
            plan = await self._load(
                session,
                tenant_id=tenant_id,
                case_id=case_id,
                run_id=run_id,
                plan_id=plan_id,
                plan_hash=plan_hash,
            )
            count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(SourcingPageAttemptRow)
                    .where(
                        SourcingPageAttemptRow.tenant_id == str(tenant_id),
                        SourcingPageAttemptRow.run_id == str(run_id),
                        SourcingPageAttemptRow.plan_hash == plan_hash,
                    )
                )
                or 0
            )
            if count > plan.max_pages_read:
                raise ValidationError("公开寻源页面预算状态无效")
            return count

    async def claim_page_attempt(self, **values: object) -> bool:
        tenant_id = TenantId(str(values["tenant_id"]))
        case_id = SourcingCaseId(str(values["case_id"]))
        run_id = RunId(str(values["run_id"]))
        plan_id = SourcingPlanId(str(values["plan_id"]))
        plan_hash = str(values["plan_hash"])
        raw_query_index = values["query_index"]
        raw_result_index = values["result_index"]
        if (
            isinstance(raw_query_index, bool)
            or not isinstance(raw_query_index, int)
            or raw_query_index < 0
            or isinstance(raw_result_index, bool)
            or not isinstance(raw_result_index, int)
            or raw_result_index < 0
        ):
            raise ValidationError("公开寻源页面位置无效")
        query_index = raw_query_index
        result_index = raw_result_index
        async with self._factory() as session, session.begin():
            plan = await self._load(
                session,
                tenant_id=tenant_id,
                case_id=case_id,
                run_id=run_id,
                plan_id=plan_id,
                plan_hash=plan_hash,
            )
            if query_index >= len(plan.queries):
                raise ValidationError("公开寻源页面位置无效")
            execution = await SourcingSearchExecutionRepositoryImpl(
                session, tenant_id
            ).get_by_request_key(
                tenant_id, sourcing_search_request_key(plan_hash, query_index)
            )
            if (
                execution is None
                or execution.run_id != run_id
                or execution.plan_id != plan_id
                or execution.plan_hash != plan_hash
                or result_index >= len(execution.locator_results)
            ):
                raise ValidationError("公开寻源页面位置无效")
            if (
                int(
                    await session.scalar(
                        select(func.count())
                        .select_from(SourcingPageAttemptRow)
                        .where(
                            SourcingPageAttemptRow.tenant_id == str(tenant_id),
                            SourcingPageAttemptRow.run_id == str(run_id),
                            SourcingPageAttemptRow.plan_hash == plan_hash,
                        )
                    )
                    or 0
                )
                >= plan.max_pages_read
            ):
                return False
            inserted = (
                await session.execute(
                    pg_insert(SourcingPageAttemptRow)
                    .values(
                        tenant_id=str(tenant_id),
                        case_id=str(case_id),
                        plan_id=str(plan_id),
                        run_id=str(run_id),
                        plan_hash=plan_hash,
                        query_index=query_index,
                        result_index=result_index,
                        attempted_at=self._now(),
                    )
                    .on_conflict_do_nothing()
                    .returning(SourcingPageAttemptRow.result_index)
                )
            ).scalar_one_or_none()
            return inserted is not None

    async def commit_locator_receipt(self, **values: object) -> None:
        batch = values.get("batch")
        if not isinstance(batch, SearchResultBatch):
            raise ValidationError("公开寻源回执批次无效")
        await self._write_execution(values, batch=batch, uncertain=False)

    async def record_uncertain(self, **values: object) -> None:
        await self._write_execution(values, batch=None, uncertain=True)

    async def _write_execution(
        self,
        values: dict[str, object],
        *,
        batch: SearchResultBatch | None,
        uncertain: bool,
    ) -> None:
        tenant_id = TenantId(str(values["tenant_id"]))
        case_id = SourcingCaseId(str(values["case_id"]))
        run_id = RunId(str(values["run_id"]))
        plan_id = SourcingPlanId(str(values["plan_id"]))
        plan_hash = str(values["plan_hash"])
        raw_query_index = values["query_index"]
        if isinstance(raw_query_index, bool) or not isinstance(raw_query_index, int):
            raise ValidationError("公开寻源回执查询位置无效")
        query_index = raw_query_index
        request_key = str(values["request_key"])
        query_hash = str(values["query_hash"])
        async with self._factory() as session, session.begin():
            plan = await self._load(
                session,
                tenant_id=tenant_id,
                case_id=case_id,
                run_id=run_id,
                plan_id=plan_id,
                plan_hash=plan_hash,
            )
            if (
                request_key != sourcing_search_request_key(plan_hash, query_index)
                or query_index >= len(plan.queries)
                or query_hash != _query_hash(plan.queries[query_index].query_text)
                or (
                    batch is not None
                    and (
                        batch.tenant_id != tenant_id
                        or batch.country != plan.queries[query_index].target_country
                        or batch.category != plan.product_category
                    )
                )
            ):
                raise ValidationError("公开寻源回执绑定无效")
            locators: tuple[dict[str, object], ...] = tuple(
                {
                    "title": item.title,
                    "url": item.url,
                    "description": item.description,
                }
                for item in (() if batch is None else batch.results)
            )
            status = (
                SourcingSearchExecutionStatus.UNCERTAIN
                if uncertain
                else (
                    SourcingSearchExecutionStatus.SUCCEEDED
                    if locators
                    else SourcingSearchExecutionStatus.NO_RESULTS
                )
            )
            now = self._now()
            execution = SourcingSearchExecution(
                execution_id=f"sse_{request_key[:26]}",
                tenant_id=tenant_id,
                case_id=case_id,
                plan_id=plan_id,
                run_id=run_id,
                plan_hash=plan_hash,
                query_index=query_index,
                request_key=request_key,
                query_hash=query_hash,
                locator_results=locators,
                provider_status=status,
                created_at=now,
                completed_at=None if uncertain else now,
            )
            repo = SourcingSearchExecutionRepositoryImpl(session, tenant_id)
            await repo.get_or_create_canonical(tenant_id, execution)


class PostgresPublicCandidateDraftWriter:
    """Task 9 草稿的受信安全投影；不调用候选核验服务。"""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        tenant_id: TenantId,
        *,
        now: Callable[[], datetime],
    ) -> None:
        self._factory = factory
        self._tenant_id = tenant_id
        self._now = now

    async def _require_verified_artifact(
        self, draft: SourcingPageCandidateDraft
    ) -> None:
        async with self._factory() as session:
            found = (
                await session.execute(
                    select(RawArtifactRow.artifact_id).where(
                        RawArtifactRow.tenant_id == str(self._tenant_id),
                        RawArtifactRow.artifact_id
                        == str(draft.evidence.snapshot_artifact_ref),
                        RawArtifactRow.kind == "web_snapshot",
                        RawArtifactRow.content_hash == draft.evidence.content_hash,
                    )
                )
            ).scalar_one_or_none()
        if found is None:
            raise ValidationError("公开寻源草稿 Artifact 未核验")

    async def save(self, **values: object) -> str:
        draft = values.get("draft")
        tenant_id = values.get("tenant_id")
        if (
            not isinstance(draft, SourcingPageCandidateDraft)
            or tenant_id != self._tenant_id
        ):
            raise ValidationError("公开寻源候选草稿绑定无效")
        await self._require_verified_artifact(draft)
        case_id = SourcingCaseId(str(values["case_id"]))
        run_id = RunId(str(values["run_id"]))
        plan_id = SourcingPlanId(str(values["plan_id"]))
        plan_hash = str(values["plan_hash"])
        raw_query_index = values["query_index"]
        raw_result_index = values["result_index"]
        if (
            isinstance(raw_query_index, bool)
            or not isinstance(raw_query_index, int)
            or isinstance(raw_result_index, bool)
            or not isinstance(raw_result_index, int)
        ):
            raise ValidationError("公开寻源草稿位置无效")
        query_index = raw_query_index
        result_index = raw_result_index
        artifact_ref = str(draft.evidence.snapshot_artifact_ref)
        source_key = sourcing_candidate_draft_source_key(
            tenant_id=self._tenant_id,
            case_id=case_id,
            run_id=run_id,
            plan_hash=plan_hash,
            query_index=query_index,
            result_index=result_index,
            evidence_artifact_ref=artifact_ref,
        )
        rejection_codes: list[Any] = list(draft.rejection_reasons)
        if draft.supplier_name is None:
            rejection_codes.insert(0, "supplier_identity_missing")
        if draft.product_title is None:
            rejection_codes.insert(0, "product_identity_missing")
        safe = PublicCandidateDraft(
            draft_id=f"scd_{source_key[:26]}",
            tenant_id=self._tenant_id,
            case_id=case_id,
            run_id=run_id,
            plan_id=plan_id,
            plan_hash=plan_hash,
            query_index=query_index,
            result_index=result_index,
            source_key=source_key,
            supplier_name=(
                draft.supplier_name.literal if draft.supplier_name is not None else None
            ),
            product_title=(
                draft.product_title.literal if draft.product_title is not None else None
            ),
            specs=tuple(
                PublicCandidateDraftSpec(
                    spec_name=item.spec_name,
                    required=item.required,
                    observed=item.observed.literal
                    if item.observed is not None
                    else None,
                )
                for item in draft.specs
            ),
            moq=draft.moq,
            indicative_price_tiers=tuple(
                PublicCandidateDraftPriceTier(
                    minimum_quantity=item.minimum_quantity,
                    amount=item.amount,
                    currency=item.currency,
                    unit=item.unit,
                )
                for item in draft.price_tiers
                if item.amount is not None
                and item.minimum_quantity is not None
                and item.currency is not None
                and item.unit is not None
                and not item.rejection_reasons
            ),
            rejection_codes=tuple(dict.fromkeys(rejection_codes)),
            evidence_url=draft.evidence.source_url,
            evidence_observed_at=draft.evidence.observed_at,
            evidence_hash=draft.evidence.content_hash,
            evidence_artifact_ref=draft.evidence.snapshot_artifact_ref,
            created_at=draft.evidence.observed_at,
        )
        async with SqlAlchemySourcingUnitOfWork(self._factory, self._tenant_id) as uow:
            canonical = await uow.candidate_drafts.get_or_create_canonical(
                self._tenant_id, safe
            )
        return canonical.draft_id


__all__ = (
    "PostgresPublicCandidateDraftWriter",
    "PostgresSourcingWebPersistence",
    "sourcing_candidate_draft_source_key",
)
