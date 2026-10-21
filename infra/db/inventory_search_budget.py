"""按任务行锁持久固定搜索身份与总请求预算；未知保留预留。"""

from collections.abc import Awaitable, Callable
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shared.schemas.identifiers import AcquisitionJobId, TenantId
from shared.schemas.search_backend import InventorySearchPolicy, SearchBackendPin

from .prospect_inventory_tables import AcquisitionAttemptRow, AcquisitionJobRow

SEARCH_REQUEST_CNY = Decimal("0.005")


class InventorySearchBudget:
    """maximum_requests 是跨来源任务总上限；后续受信服务计划只能收紧。"""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        tenant: TenantId,
        job_id: AcquisitionJobId,
        lease_token: str,
        pin: SearchBackendPin,
        policy: InventorySearchPolicy,
        *,
        now: Callable[[], datetime],
        trusted_maximum_requests: int | None = None,
        trusted_cap_reader: Callable[[], Awaitable[int | None]] | None = None,
    ) -> None:
        self.factory, self.tenant, self.job_id, self.lease_token = (
            factory,
            tenant,
            job_id,
            lease_token,
        )
        self.pin, self.policy, self.now = pin, policy, now
        if trusted_maximum_requests is not None and (
            type(trusted_maximum_requests) is not int
            or not 0 <= trusted_maximum_requests <= 24
        ):
            raise ValueError("受信任务上限无效")
        self.trusted_maximum_requests = trusted_maximum_requests
        self.trusted_cap_reader = trusted_cap_reader
        self.binding = {
            **pin.model_dump(mode="json"),
            "policy": policy.model_dump(mode="json"),
        }

    async def _row(self, session: AsyncSession) -> AcquisitionJobRow:
        row = await session.scalar(
            select(AcquisitionJobRow)
            .where(
                AcquisitionJobRow.tenant_id == self.tenant,
                AcquisitionJobRow.job_id == self.job_id,
            )
            .with_for_update()
        )
        if (
            row is None
            or row.status != "processing"
            or row.lease_token != self.lease_token
            or row.lease_until is None
            or row.lease_until <= self.now()
        ):
            raise ValueError("搜索任务租约无效")
        if (
            self.pin.policy_version != self.policy.version
            or self.pin.backend_id != self.policy.backend_id
        ):
            raise ValueError("搜索政策绑定无效")
        if (
            row.search_binding is None
            or {
                k: v
                for k, v in row.search_binding.items()
                if k != "effective_request_cap"
            }
            != self.binding
        ):
            raise ValueError("搜索连接或政策已改变")
        cap = min(
            self.policy.maximum_requests,
            row.search_binding.get(
                "effective_request_cap", self.policy.maximum_requests
            ),
            self.trusted_maximum_requests
            if self.trusted_maximum_requests is not None
            else self.policy.maximum_requests,
        )
        row.search_binding = {**row.search_binding, "effective_request_cap": cap}
        return row

    async def bind(self) -> None:
        if self.trusted_cap_reader is not None:
            current = await self.trusted_cap_reader()
            if current is not None:
                self.trusted_maximum_requests = min(self.trusted_maximum_requests, current) if self.trusted_maximum_requests is not None else current
        async with self.factory() as session, session.begin():
            row = await session.scalar(
                select(AcquisitionJobRow)
                .where(
                    AcquisitionJobRow.tenant_id == self.tenant,
                    AcquisitionJobRow.job_id == self.job_id,
                )
                .with_for_update()
            )
            if row is None:
                raise ValueError("搜索任务不存在")
            if row.search_binding is None:
                if row.source_plan or row.attempts > 1:
                    raise ValueError("历史任务不能补造搜索绑定")
                row.search_binding = self.binding
            await self._row(session)

    async def reserve(self, source_id: str, request_key: str) -> None:
        # 收紧必须独立提交，即使随后因已用次数或未知请求拒绝新IO。
        await self.bind()
        async with self.factory() as session, session.begin():
            row = await self._row(session)
            attempt = await session.scalar(
                select(AcquisitionAttemptRow).where(
                    AcquisitionAttemptRow.tenant_id == self.tenant,
                    AcquisitionAttemptRow.job_id == self.job_id,
                    AcquisitionAttemptRow.source_id == source_id,
                )
            )
            if (
                source_id not in row.source_plan
                or row.collection_deadline is None
                or row.collection_deadline <= self.now()
                or attempt is None
                or attempt.outcome != "started"
            ):
                raise ValueError("搜索来源未获当前计划授权")
            entries = list(row.search_reservations)
            if any(
                e["key"] == request_key
                or (e["source"] == source_id and e["state"] == "started")
                for e in entries
            ):
                raise ValueError("搜索已派发或需要核对")
            assert row.search_binding is not None
            if len(entries) >= row.search_binding["effective_request_cap"]:
                raise ValueError("任务搜索次数预算耗尽")
            if self.pin.backend_id == "deepseek" and (
                not self.policy.paid_search_enabled
                or Decimal(len(entries) + 1) * SEARCH_REQUEST_CNY
                > self.policy.maximum_cost_cny
            ):
                raise ValueError("付费搜索未获准或预算耗尽")
            entries.append(
                {"source": source_id, "key": request_key, "state": "started"}
            )
            row.search_reservations = entries

    async def finish(self, request_key: str) -> None:
        """成功空结果仍保守占用本地预算，不推断额度可重新消费。"""
        async with self.factory() as session, session.begin():
            row = await self._row(session)
            entries = [dict(e) for e in row.search_reservations]
            for entry in entries:
                if entry["key"] == request_key:
                    entry["state"] = "completed"
                    row.search_reservations = entries
                    return
            raise ValueError("搜索预留不存在")
