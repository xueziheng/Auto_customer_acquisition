"""Tavily 单部署账户的持久保守额度；所有读写严格绑定 owner tenant。"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import or_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from connectors.search_contracts import SearchCostStatus, SearchUsage
from infra.db.base import TenantScopedRepository
from infra.db.tables import (
    SearchQuotaAccountRow,
    SearchQuotaReservationRow,
    SearchQuotaRunRow,
)
from shared.errors import ValidationError
from shared.schemas.identifiers import RunId, TenantId
from tool_gateway.errors import ToolErrorCategory, ToolGatewayError
from tool_gateway.free_search_contracts import (
    FreeSearchError,
    FreeSearchStopReason,
    ReservationStatus,
    SearchQuotaRunState,
    SearchQuotaSnapshot,
    SearchReservation,
    verified_free_remaining,
)

_PROVIDER = "tavily"


class PostgresSearchQuotaRepository(TenantScopedRepository):
    """不接受请求账户别名：同数据库只允许一个 Tavily owner，轮换 key 不重置。

    usage 无账期/账户 ID/一致性水位，故上限只降不升，所有本地预留累计扣减。
    可能重复保守扣除已计入 provider usage 的本地消费；这是无可靠对账信息时
    的安全下界，不是精确可用余额，也不是按月刷新额度的钱包。
    """

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        tenant_id: TenantId,
        *,
        now: Callable[[], datetime],
    ) -> None:
        if not tenant_id or len(tenant_id) > 40:
            raise ValidationError("免费搜索租户绑定无效")
        super().__init__(tenant_id)
        self._factory = factory
        self._now = now

    async def _locked_account(self, session: AsyncSession) -> SearchQuotaAccountRow:
        await session.execute(
            insert(SearchQuotaAccountRow)
            .values(
                tenant_id=str(self._tenant_id),
                provider=_PROVIDER,
            )
            .on_conflict_do_nothing()
        )
        row = (
            await session.execute(
                self.scoped_query(SearchQuotaAccountRow)
                .where(
                    SearchQuotaAccountRow.provider == _PROVIDER,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            # 全局唯一约束只用于拒绝，不跨租户读取或暴露 owner。
            raise ToolGatewayError(ToolErrorCategory.PERMISSION_DENIED)
        return row

    async def _is_replay(
        self, session: AsyncSession, run_id: RunId, request_key: str
    ) -> bool:
        row = (
            await session.execute(
                self.scoped_query(SearchQuotaReservationRow)
                .where(
                    SearchQuotaReservationRow.provider == _PROVIDER,
                    SearchQuotaReservationRow.run_id == str(run_id),
                    or_(
                        SearchQuotaReservationRow.request_key == request_key,
                        SearchQuotaReservationRow.status.in_(("reserved", "uncertain")),
                    ),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        return row is not None

    async def _bind_fingerprint_version(
        self, session: AsyncSession, run_id: RunId, fingerprint_version: str
    ) -> bool:
        """账户锁内只创建首次版本；旧 NULL 或版本漂移不能猜测为可重新绑定。"""
        if not isinstance(fingerprint_version, str) or re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}", fingerprint_version
        ) is None:
            raise ValidationError("免费搜索指纹版本无效")
        await session.execute(
            insert(SearchQuotaRunRow).values(
                tenant_id=str(self._tenant_id), run_id=str(run_id),
                fingerprint_version=fingerprint_version,
                updated_at=self._timestamp(),
            ).on_conflict_do_nothing()
        )
        row = (await session.execute(self.scoped_query(SearchQuotaRunRow).where(
            SearchQuotaRunRow.run_id == str(run_id),
        ))).scalar_one()
        return row.fingerprint_version == fingerprint_version

    async def check_available(
        self, run_id: RunId, request_key: str, *, fingerprint_version: str
    ) -> None:
        """凭证/usage 前拒绝同 Run 未决或相同操作；reserve 事务内仍需重新检查。"""
        _validate_operation(run_id, request_key)
        reason: FreeSearchStopReason | None = None
        async with self._factory() as session, session.begin():
            account = await self._locked_account(session)
            version_matches = await self._bind_fingerprint_version(
                session, run_id, fingerprint_version
            )
            if not version_matches or await self._is_replay(session, run_id, request_key):
                reason = FreeSearchStopReason.REQUEST_UNCERTAIN
            elif (
                account.ceiling is not None and account.ceiling <= account.reservations
            ):
                reason = FreeSearchStopReason.QUOTA_EXHAUSTED
            if reason is not None:
                await self._record_run(session, run_id, reason)
        if reason is not None:
            raise FreeSearchError(reason)

    async def reserve(
        self, run_id: RunId, request_key: str, usage: SearchUsage,
        *, fingerprint_version: str,
    ) -> SearchReservation:
        """账户行锁把快照收紧与一单位额度预留原子提交，跨 Run 不共享内存计数。"""
        _validate_operation(run_id, request_key)
        now = self._timestamp()
        available = verified_free_remaining(usage)
        reservation: SearchReservation | None = None
        reason: FreeSearchStopReason | None = None
        async with self._factory() as session, session.begin():
            account = await self._locked_account(session)
            version_matches = await self._bind_fingerprint_version(
                session, run_id, fingerprint_version
            )
            if not version_matches or await self._is_replay(session, run_id, request_key):
                reason = FreeSearchStopReason.REQUEST_UNCERTAIN
            else:
                account.checked_at = now
                account.cost_status = usage.cost_status.value
                account.usage_limit = usage.limit
                account.usage_used = usage.used
                account.paygo_enabled = usage.paygo_enabled
                account.included_credits_free = usage.included_credits_free
                if available is not None:
                    account.ceiling = (
                        available
                        if account.ceiling is None
                        else min(account.ceiling, available)
                    )
                    if account.ceiling > account.reservations:
                        account.reservations += 1
                        row = SearchQuotaReservationRow(
                            tenant_id=str(self._tenant_id),
                            provider=_PROVIDER,
                            run_id=str(run_id),
                            request_key=request_key,
                            status="reserved",
                            created_at=now,
                            updated_at=now,
                        )
                        session.add(row)
                        reservation = _reservation(row)
                    else:
                        reason = FreeSearchStopReason.QUOTA_EXHAUSTED
                else:
                    reason = (
                        FreeSearchStopReason.PAID_ENABLED
                        if usage.paygo_enabled is True
                        or usage.cost_status is SearchCostStatus.PAID
                        else FreeSearchStopReason.USAGE_UNKNOWN
                    )
            await self._record_run(session, run_id, reason)
        # 必须在事务提交后拒绝，保留失败/耗尽的安全快照，而不是 rollback。
        if reservation is None:
            raise FreeSearchError(reason or FreeSearchStopReason.USAGE_UNKNOWN)
        return reservation

    async def record_unavailable(self, run_id: RunId) -> None:
        """只记录未知的安全摘要；失败不释放已有额度，也不泄露异常原文。"""
        async with self._factory() as session, session.begin():
            account = await self._locked_account(session)
            account.cost_status = "unknown"
            account.usage_limit = None
            account.usage_used = None
            account.paygo_enabled = None
            account.included_credits_free = False
            account.checked_at = self._timestamp()
            await self._record_run(session, run_id, FreeSearchStopReason.USAGE_UNKNOWN)

    async def mark_dispatched(self, run_id: RunId, request_key: str) -> None:
        """在调用 connector 前持久标记 uncertain，崩溃/取消无需补偿写入。"""
        await self._transition(run_id, request_key, "reserved", "uncertain")

    async def consume(self, run_id: RunId, request_key: str) -> None:
        """只在 connector 成功后前进到 consumed；不退款、不删除。"""
        await self._transition(run_id, request_key, "uncertain", "consumed")

    async def acknowledge_uncertain_as_consumed(
        self, run_id: RunId, request_key: str
    ) -> None:
        """人工核对只把原 uncertain 键收紧；不退款、不删行、不改累计预留。"""

        _validate_operation(run_id, request_key)
        async with self._factory() as session, session.begin():
            row = (
                await session.execute(
                    self.scoped_query(SearchQuotaReservationRow)
                    .where(
                        SearchQuotaReservationRow.provider == _PROVIDER,
                        SearchQuotaReservationRow.run_id == str(run_id),
                        SearchQuotaReservationRow.request_key == request_key,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None or row.status not in {"uncertain", "consumed"}:
                raise ToolGatewayError(ToolErrorCategory.RECONCILIATION_REQUIRED)
            if row.status == "uncertain":
                row.status = "consumed"
                row.updated_at = self._timestamp()
                await self._record_run(session, run_id, None)

    async def _transition(
        self,
        run_id: RunId,
        request_key: str,
        expected: ReservationStatus,
        target: ReservationStatus,
    ) -> None:
        _validate_operation(run_id, request_key)
        async with self._factory() as session, session.begin():
            row = (
                await session.execute(
                    self.scoped_query(SearchQuotaReservationRow)
                    .where(
                        SearchQuotaReservationRow.provider == _PROVIDER,
                        SearchQuotaReservationRow.run_id == str(run_id),
                        SearchQuotaReservationRow.request_key == request_key,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None or row.status != expected:
                raise ToolGatewayError(ToolErrorCategory.RECONCILIATION_REQUIRED)
            row.status = target
            row.updated_at = self._timestamp()
            await self._record_run(
                session,
                run_id,
                FreeSearchStopReason.REQUEST_UNCERTAIN
                if target == "uncertain"
                else None,
            )

    async def _record_run(
        self, session: AsyncSession, run_id: RunId, reason: FreeSearchStopReason | None
    ) -> None:
        await session.execute(
            insert(SearchQuotaRunRow)
            .values(
                tenant_id=str(self._tenant_id),
                run_id=str(run_id),
                stop_reason=None if reason is None else reason.value,
                updated_at=self._timestamp(),
            )
            .on_conflict_do_update(
                index_elements=["tenant_id", "run_id"],
                set_={
                    "stop_reason": None if reason is None else reason.value,
                    "updated_at": self._timestamp(),
                },
                where=SearchQuotaRunRow.tenant_id == str(self._tenant_id),
            )
        )

    async def run_state(self, run_id: RunId) -> SearchQuotaRunState | None:
        """未决预留优先；进程在 reserved 后崩溃也必须报告不确定而不是无结果。"""
        async with self._factory() as session:
            pending = (
                await session.execute(
                    self.scoped_query(SearchQuotaReservationRow)
                    .where(
                        SearchQuotaReservationRow.provider == _PROVIDER,
                        SearchQuotaReservationRow.run_id == str(run_id),
                        SearchQuotaReservationRow.status.in_(("reserved", "uncertain")),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if pending is not None:
                return SearchQuotaRunState(
                    self._tenant_id,
                    run_id,
                    FreeSearchStopReason.REQUEST_UNCERTAIN,
                    pending.updated_at,
                )
            row = (
                await session.execute(
                    self.scoped_query(SearchQuotaRunRow).where(
                        SearchQuotaRunRow.run_id == str(run_id),
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return SearchQuotaRunState(
                self._tenant_id,
                run_id,
                None
                if row.stop_reason is None
                else FreeSearchStopReason(row.stop_reason),
                row.updated_at,
            )

    async def snapshot(self) -> SearchQuotaSnapshot | None:
        """读取 tenant-scoped 安全状态；未声明账户返回 None，不创建账户。"""
        async with self._factory() as session:
            row = (
                await session.execute(
                    self.scoped_query(SearchQuotaAccountRow).where(
                        SearchQuotaAccountRow.provider == _PROVIDER,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return SearchQuotaSnapshot(
                self._tenant_id,
                "tavily",
                max(0, (row.ceiling or 0) - row.reservations),
                row.reservations,
                SearchCostStatus(row.cost_status),
                row.usage_limit,
                row.usage_used,
                row.paygo_enabled,
                row.checked_at,
                row.included_credits_free,
            )

    async def get(self, run_id: RunId, request_key: str) -> SearchReservation | None:
        """按安全 Run/操作指纹读取预留，供恢复判断；不读取原查询或结果。"""
        _validate_operation(run_id, request_key)
        async with self._factory() as session:
            row = (
                await session.execute(
                    self.scoped_query(SearchQuotaReservationRow).where(
                        SearchQuotaReservationRow.provider == _PROVIDER,
                        SearchQuotaReservationRow.run_id == str(run_id),
                        SearchQuotaReservationRow.request_key == request_key,
                    )
                )
            ).scalar_one_or_none()
            return None if row is None else _reservation(row)

    def _timestamp(self) -> datetime:
        now = self._now()
        if now.tzinfo is None or now.utcoffset() != UTC.utcoffset(now):
            raise ValidationError("免费搜索时钟无效")
        return now


def _validate_operation(run_id: RunId, request_key: str) -> None:
    if (
        not run_id
        or len(run_id) > 40
        or re.fullmatch(r"[a-f0-9]{64}", request_key) is None
    ):
        raise ValidationError("免费搜索操作标识无效")


def _reservation(row: SearchQuotaReservationRow) -> SearchReservation:
    return SearchReservation(
        TenantId(row.tenant_id),
        RunId(row.run_id),
        row.request_key,
        cast(ReservationStatus, row.status),
        row.created_at,
        row.updated_at,
    )
