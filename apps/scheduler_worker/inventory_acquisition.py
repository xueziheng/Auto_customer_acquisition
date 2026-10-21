"""单轮库存任务驱动；无后台循环，必须由singleton scheduler调用。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from domains.prospecting.collection_result import CollectionResult
from domains.prospecting.inventory import replenishment_target
from domains.prospecting.inventory_access import InventoryAuthority
from domains.prospecting.inventory_fit import (
    MAX_FIT_BATCHES,
    MAX_FIT_CANDIDATES,
    MAX_FIT_EVIDENCE_BYTES,
    InventoryFitAssessment,
    InventoryFitBatch,
    candidate_evidence_version,
    fit_evidence_inputs,
    selected_fit_candidates,
)
from domains.prospecting.projection_diagnostic import (
    ProjectionDiagnosticError,
    SaveSummary,
    SourceDiagnostic,
)
from domains.prospecting.service import (
    AcquisitionJob,
    AcquisitionWorker,
    InventoryService,
    PublicCandidate,
    PublicCatalog,
    SourceState,
    select_sources,
)
from shared.errors import PermissionDenied
from shared.schemas.identifiers import AcquisitionJobId, TenantId
from shared.schemas.model_invocation import ModelGenerationError
from workflows.enterprise_knowledge.confirmed_context import ConfirmedContextReader


class InventoryBackend(AcquisitionWorker, InventoryService, Protocol):
    """同一绑定企业的后台与只读状态端口。"""

    async def save_collected(
        self,
        tenant_id: TenantId,
        job_id: AcquisitionJobId,
        lease_token: str,
        candidates: tuple[PublicCandidate, ...],
    ) -> SaveSummary:
        """必须持job行锁核验fence后才委托公共目录写入。"""
        ...

    async def fit_pool(
        self, tenant_id: TenantId, job_id: AcquisitionJobId, lease_token: str
    ) -> tuple[PublicCandidate, ...]: ...

    async def reserve_fit_batch(
        self,
        tenant_id: TenantId,
        job_id: AcquisitionJobId,
        lease_token: str,
        candidates: tuple[PublicCandidate, ...],
    ) -> InventoryFitBatch: ...

    async def complete_fit_batch(
        self,
        tenant_id: TenantId,
        job_id: AcquisitionJobId,
        lease_token: str,
        sequence: int,
        assessments: tuple[InventoryFitAssessment, ...],
    ) -> None: ...


class InventoryFitModel(Protocol):
    async def evaluate(
        self,
        job: AcquisitionJob,
        batch: InventoryFitBatch,
        candidates: tuple[PublicCandidate, ...],
        guard: Callable[[], Awaitable[None]],
    ) -> tuple[InventoryFitAssessment, ...]: ...


class InventorySourceReader(Protocol):
    async def list_sources(self, tenant_id: TenantId) -> tuple[SourceState, ...]: ...


class InventoryCollector(Protocol):
    async def collect(
        self,
        job: AcquisitionJob,
        source_id: str,
        guard: Callable[[], Awaitable[None]],
        *,
        target_count: int = 50,
    ) -> CollectionResult:
        """每次外部动作前执行guard；仅返回真实页面校验后的公共投影。"""
        ...


class LeaseLost(ValueError):
    """旧任务不再具有任何写权限。"""


class InventoryAcquisitionDriver:
    def __init__(
        self,
        *,
        tenant_id: TenantId,
        backend: InventoryBackend,
        catalog: PublicCatalog,
        authority: InventoryAuthority,
        sources: InventorySourceReader,
        collector: InventoryCollector,
        fit_model: InventoryFitModel | None = None,
        context_reader: ConfirmedContextReader | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.context_reader = context_reader
        self.fit_model = fit_model
        self.tenant_id, self.backend, self.catalog = tenant_id, backend, catalog
        self.authority, self.sources, self.collector, self.now = (
            authority,
            sources,
            collector,
            now,
        )

    async def scan(self) -> int:
        """每天额度耗尽只读持久状态等待重置；崩溃后的未知调用禁止再次派发。"""
        if (await self.backend.status(self.tenant_id)).remaining_today == 0:
            return 0
        job = await self.backend.claim_job(self.tenant_id, timedelta(minutes=10))
        if job is None:
            return 0
        token = job.lease_token
        if token is None:
            raise LeaseLost("采集租约缺失")

        async def lease_guard() -> None:
            current = await self.backend.get_job(self.tenant_id, job.id)
            if (
                current is None
                or current.tenant_id != self.tenant_id
                or current.employee_id != job.employee_id
                or current.query != job.query
                or current.status != "processing"
                or current.lease_token != token
                or current.lease_until is None
                or current.lease_until <= self.now()
            ):
                raise LeaseLost("采集租约已失效")

        async def guard() -> None:
            await lease_guard()
            if job.query.confirmed_refs:
                if self.context_reader is None:
                    raise PermissionDenied("企业资料上下文不可用")
                await self.context_reader.read(
                    self.tenant_id, job.employee_id, job.query.confirmed_refs
                )
            scope = await self.authority.current_scope(self.tenant_id, job.employee_id)
            if (
                scope.tenant_id != self.tenant_id
                or scope.employee_id != job.employee_id
                or (
                    scope.allowed_owners is not None
                    and job.employee_id not in scope.allowed_owners
                )
            ):
                raise PermissionDenied("采集员工权限已失效")

        async def fit_candidates() -> tuple[PublicCandidate, ...]:
            if self.fit_model is None:
                raise PermissionDenied("企业适配模型未配置")
            for _ in range(MAX_FIT_BATCHES):
                await guard()
                current = await self.backend.get_job(self.tenant_id, job.id)
                if current is None:
                    raise LeaseLost("采集任务已失效")
                fresh = await self.backend.fit_pool(self.tenant_id, job.id, token)
                selected = selected_fit_candidates(
                    fresh, current.fit_batches, job.query
                )
                if (
                    len(selected) >= job.remaining_count
                    or len(current.fit_batches) >= MAX_FIT_BATCHES
                ):
                    return selected
                seen = {
                    (c.candidate_id, c.evidence_version)
                    for b in current.fit_batches
                    for c in b.candidates
                }
                batch_candidates: tuple[PublicCandidate, ...] = ()
                for candidate in fresh:
                    if (
                        str(candidate.id),
                        candidate_evidence_version(candidate),
                    ) in seen:
                        continue
                    proposed = (*batch_candidates, candidate)
                    if (
                        len(
                            json.dumps(
                                fit_evidence_inputs(proposed), ensure_ascii=False
                            ).encode()
                        )
                        > MAX_FIT_EVIDENCE_BYTES
                    ):
                        continue
                    batch_candidates = proposed
                    if len(batch_candidates) >= MAX_FIT_CANDIDATES:
                        break
                if not batch_candidates:
                    return selected
                await guard()
                batch = await self.backend.reserve_fit_batch(
                    self.tenant_id, job.id, token, batch_candidates
                )
                decisions = await self.fit_model.evaluate(
                    job, batch, batch_candidates, guard
                )
                await guard()
                await self.backend.complete_fit_batch(
                    self.tenant_id, job.id, token, batch.sequence, decisions
                )
            current = await self.backend.get_job(self.tenant_id, job.id)
            if current is None:
                raise LeaseLost("采集任务已失效")
            fresh = await self.backend.fit_pool(self.tenant_id, job.id, token)
            return selected_fit_candidates(fresh, current.fit_batches, job.query)

        try:
            await guard()
            private = job.query.requires_private_fit
            selected = await fit_candidates() if private else ()
            if private and (len(selected) >= job.remaining_count or job.attempts > 1):
                await guard()
                await self.backend.deliver_job(self.tenant_id, job.id, token)
                return 1
            if job.attempts > 1:
                await self.backend.fail_job(
                    self.tenant_id, job.id, token, "budget_exhausted"
                )
                return 1
            prepare = getattr(self.sources, "prepare", None)
            if prepare is not None:
                await prepare(job, guard)
            plan = select_sources(
                list(await self.sources.list_sources(self.tenant_id)), 3
            )
            if not plan and private and selected:
                await guard()
                await self.backend.deliver_job(self.tenant_id, job.id, token)
                return 1
            if not plan:
                await self.backend.fail_job(
                    self.tenant_id, job.id, token, "sources_unavailable"
                )
                return 1
            async with asyncio.timeout(480):
                target = (
                    job.remaining_count
                    if private
                    else replenishment_target(job.requested_count)
                )
                completed_sources = 0
                for source_id in plan:
                    await guard()
                    fresh_count = (
                        len(selected)
                        if private
                        else len(
                            await self.catalog.fresh(
                                job.query.public_projection(), self.now()
                            )
                        )
                    )
                    if fresh_count >= target and source_id != plan[-1]:
                        continue
                    try:
                        collected = await self.collector.collect(
                            job,
                            source_id,
                            guard,
                            target_count=max(0, target - fresh_count),
                        )
                    except asyncio.CancelledError:
                        raise
                    except (LeaseLost, PermissionDenied):
                        raise
                    except Exception as error:  # noqa: BLE001 -- 一站失败不取消预留探索，但未知调用绝不重试。
                        await guard()
                        await self.backend.record_source_attempt(
                            self.tenant_id,
                            job.id,
                            token,
                            source_id,
                            error.diagnostic.outcome
                            if isinstance(error, ModelGenerationError)
                            and error.diagnostic is not None
                            else "failed",
                            0,
                            diagnostic=SourceDiagnostic(projection=error.summary)
                            if isinstance(error, ProjectionDiagnosticError)
                            else None,
                        )
                        continue
                    completed_sources += 1
                    await guard()
                    saved = await self.backend.save_collected(
                        self.tenant_id, job.id, token, collected.candidates
                    )
                    await guard()
                    if private:
                        selected = await fit_candidates()
                    await self.backend.record_source_attempt(
                        self.tenant_id,
                        job.id,
                        token,
                        source_id,
                        "complete",
                        saved.inserted_fresh,
                        diagnostic=SourceDiagnostic(
                            projection=collected.summary, save=saved
                        ),
                    )
                await guard()
                if completed_sources == 0 and not await self.catalog.fresh(
                    job.query.public_projection(), self.now()
                ):
                    await self.backend.fail_job(
                        self.tenant_id, job.id, token, "collector_failed"
                    )
                    return 1
                await self.backend.deliver_job(self.tenant_id, job.id, token)
        except asyncio.CancelledError:
            raise
        except LeaseLost:
            return 1
        except PermissionDenied:
            await lease_guard()
            await self.backend.fail_job(
                self.tenant_id, job.id, token, "collector_failed"
            )
        except Exception:  # noqa: BLE001 -- 不把外部正文、凭证或异常写任务。
            try:
                await guard()
                await self.backend.fail_job(
                    self.tenant_id, job.id, token, "collector_failed"
                )
            except (LeaseLost, PermissionDenied):
                pass
        return 1
