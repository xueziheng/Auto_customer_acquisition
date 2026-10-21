"""单轮库存任务驱动；无后台循环，必须由singleton scheduler调用。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from domains.prospecting.collection_result import CollectionResult
from domains.prospecting.inventory import replenishment_target
from domains.prospecting.inventory_access import InventoryAuthority
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
from shared.schemas.identifiers import TenantId
from shared.schemas.model_invocation import ModelGenerationError


class InventoryBackend(AcquisitionWorker, InventoryService, Protocol):
    """同一绑定企业的后台与只读状态端口。"""

    async def save_collected(
        self,
        tenant_id: TenantId,
        job_id,
        lease_token: str,
        candidates: tuple[PublicCandidate, ...],
    ) -> SaveSummary:
        """必须持job行锁核验fence后才委托公共目录写入。"""
        ...


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
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
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
                or current.status != "processing"
                or current.lease_token != token
                or current.lease_until is None
                or current.lease_until <= self.now()
            ):
                raise LeaseLost("采集租约已失效")

        async def guard() -> None:
            await lease_guard()
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

        try:
            await guard()
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
            if not plan:
                await self.backend.fail_job(
                    self.tenant_id, job.id, token, "sources_unavailable"
                )
                return 1
            async with asyncio.timeout(480):
                target = replenishment_target(job.requested_count)
                completed_sources = 0
                for source_id in plan:
                    await guard()
                    fresh_count = len(await self.catalog.fresh(job.query.public_projection(), self.now()))
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
                            self.tenant_id, job.id, token, source_id,
                            error.diagnostic.outcome if isinstance(error, ModelGenerationError)
                            and error.diagnostic is not None else "failed", 0,
                            diagnostic=SourceDiagnostic(projection=error.summary) if isinstance(error, ProjectionDiagnosticError) else None,
                        )
                        continue
                    completed_sources += 1
                    await guard()
                    saved = await self.backend.save_collected(
                        self.tenant_id, job.id, token, collected.candidates
                    )
                    await guard()
                    await self.backend.record_source_attempt(
                        self.tenant_id, job.id, token, source_id, "complete", saved.inserted_fresh,
                        diagnostic=SourceDiagnostic(projection=collected.summary, save=saved),
                    )
                await guard()
                if completed_sources == 0 and not await self.catalog.fresh(job.query.public_projection(), self.now()):
                    await self.backend.fail_job(self.tenant_id, job.id, token, "collector_failed")
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
