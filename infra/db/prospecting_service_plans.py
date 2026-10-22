"""三档服务计划持久适配；授权由调用方重验，存储保证隔离/CAS/幂等。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.prospecting.inventory import InventoryQuery
from domains.prospecting.service_plan_contracts import (
    PendingDiscoveryCycle,
    ServicePlanReservation,
    ServicePlanState,
)
from domains.prospecting.service_tiers import (
    ScreeningEvidence,
    ServiceTier,
    tier_policy,
)
from infra.db.prospecting_service_tables import (
    ProspectingDiscoveryCycleRow,
    ProspectingScreeningEvidenceRow,
    ProspectingServicePlanRow,
    ProspectingServiceReservationRow,
)
from infra.db.tables import ProspectAccountRow
from shared.errors import InvalidStateTransition, TransientError, ValidationError
from shared.schemas.identifiers import ProspectAccountId, TenantId


def _identifier(value: str, maximum: int = 64) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ValidationError("服务计划标识无效")
    return value


def _now(value: datetime) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValidationError("服务计划时间必须带时区")
    return value.astimezone(UTC)


def _stored_time(value: datetime | None) -> datetime | None:
    # SQLite不保留时区；写入统一UTC，读回只恢复该已知存储语义。
    return (
        value.replace(tzinfo=UTC)
        if value is not None and value.tzinfo is None
        else value
    )


def _state(row: ProspectingServicePlanRow) -> ServicePlanState:
    return ServicePlanState(
        tenant_id=TenantId(row.tenant_id),
        tier=cast(ServiceTier, row.tier),
        version=row.version,
        period_started_at=cast(datetime, _stored_time(row.period_started_at)),
        period_ends_at=cast(datetime, _stored_time(row.period_ends_at)),
        candidates_reserved=row.candidates_reserved,
        searches_reserved=row.searches_reserved,
        contacts_reserved=row.contacts_reserved,
        schedule_enabled=row.schedule_enabled,
        target_kind=cast(Literal["legacy_research", "inventory_request"], row.target_kind),
        query=InventoryQuery.model_validate(row.inventory_query) if row.inventory_query else None,
        employee_id=row.employee_id,
        proposal_id=row.proposal_id,
        acting_user_id=row.acting_user_id,
        next_run_at=_stored_time(row.next_run_at),
        last_run_at=_stored_time(row.last_run_at),
    )


def _cycle(row: ProspectingDiscoveryCycleRow) -> PendingDiscoveryCycle:
    return PendingDiscoveryCycle(
        tenant_id=TenantId(row.tenant_id),
        key=row.cycle_key,
        target_kind=cast(Literal["legacy_research", "inventory_request"], row.target_kind),
        query=InventoryQuery.model_validate(row.inventory_query) if row.inventory_query else None,
        employee_id=row.employee_id,
        proposal_id=row.proposal_id,
        acting_user_id=row.acting_user_id,
    )


def _version(row: ProspectingServicePlanRow | None, expected: int | None) -> None:
    if expected is not None and (type(expected) is not int or expected < 1):
        raise ValidationError("服务计划版本无效")
    if (row is None and expected is not None) or (
        row is not None and row.version != expected
    ):
        raise InvalidStateTransition("服务计划版本已变化，请重新读取")


def _valid_period(row: ProspectingServicePlanRow, now: datetime) -> bool:
    state = _state(row)
    return state.period_started_at <= now < state.period_ends_at


def _content_hash(payload: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


class PostgresProspectingServicePlanStore:
    """每次变更独立提交；Postgres行锁及SQLite写锁串行保护同租户用量。"""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    @asynccontextmanager
    async def _reader(self) -> AsyncIterator[AsyncSession]:
        try:
            async with self._factory() as session:
                yield session
        except SQLAlchemyError:
            raise TransientError("服务计划存储暂不可用") from None

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[AsyncSession]:
        async with self._factory() as session:
            try:
                if session.get_bind().dialect.name == "sqlite":
                    await session.execute(text("BEGIN IMMEDIATE"))
                else:
                    await session.begin()
                yield session
                await session.commit()
            except IntegrityError:
                await session.rollback()
                raise InvalidStateTransition(
                    "服务计划并发变更冲突，请重新读取"
                ) from None
            except SQLAlchemyError:
                await session.rollback()
                raise TransientError("服务计划存储暂不可用") from None
            except BaseException:
                await session.rollback()
                raise

    async def _plan(
        self, session: AsyncSession, tenant_id: TenantId
    ) -> ProspectingServicePlanRow | None:
        return await session.scalar(
            select(ProspectingServicePlanRow)
            .where(
                ProspectingServicePlanRow.tenant_id == tenant_id,
            )
            .with_for_update()
        )

    async def get(self, tenant_id: TenantId) -> ServicePlanState | None:
        """只读当前租户的安全状态，不公开pending内部恢复身份。"""
        _identifier(tenant_id, 40)
        async with self._reader() as session:
            row = await session.get(ProspectingServicePlanRow, tenant_id)
            return None if row is None else _state(row)

    async def assign(
        self,
        tenant_id: TenantId,
        tier: ServiceTier,
        *,
        expected_version: int | None,
        actor_id: str,
        now: datetime,
    ) -> ServicePlanState:
        """管理员赋档/续期；未到期切档保留全部保守用量及未绑定批次。"""
        _identifier(tenant_id, 40)
        _identifier(actor_id)
        now = _now(now)
        try:
            policy = tier_policy(tier)
        except (ValueError, TypeError):
            raise ValidationError("服务档位无效") from None
        async with self._transaction() as session:
            row = await self._plan(session, tenant_id)
            _version(row, expected_version)
            if row is None:
                row = ProspectingServicePlanRow(
                    tenant_id=tenant_id,
                    tier=tier,
                    version=1,
                    period_started_at=now,
                    period_ends_at=now + timedelta(days=policy.period_days),
                    candidates_reserved=0,
                    searches_reserved=0,
                    contacts_reserved=0,
                    schedule_enabled=False,
                    target_kind="legacy_research",
                    proposal_id=None,
                    acting_user_id=None,
                    next_run_at=None,
                    last_run_at=None,
                    pending_cycle_key=None,
                    assigned_by=actor_id,
                    updated_at=now,
                )
                session.add(row)
            else:
                if now < _state(row).period_started_at:
                    raise ValidationError("服务计划时间早于当前服务期")
                if now >= _state(row).period_ends_at:
                    row.period_started_at = now
                    row.period_ends_at = now + timedelta(days=policy.period_days)
                    row.candidates_reserved = row.searches_reserved = (
                        row.contacts_reserved
                    ) = 0
                row.tier, row.version = tier, row.version + 1
                row.assigned_by, row.updated_at = actor_id, now
                if row.last_run_at is not None:
                    row.next_run_at = cast(
                        datetime, _stored_time(row.last_run_at)
                    ) + timedelta(hours=policy.cadence_hours)
            await session.flush()
            return _state(row)

    async def configure_schedule(
        self,
        tenant_id: TenantId,
        *,
        proposal_id: str | None,
        acting_user_id: str | None,
        enabled: bool,
        expected_version: int,
        now: datetime,
        query: InventoryQuery | None = None,
        employee_id: str | None = None,
    ) -> ServicePlanState:
        """未尝试启动的旧批次可留痕废弃；启动结果未知时永远保留原授权身份。"""
        _identifier(tenant_id, 40)
        now = _now(now)
        if type(enabled) is not bool or (
            enabled and not (acting_user_id and (proposal_id or (query and employee_id)))
        ):
            raise ValidationError("周期探索缺少确认提案或操作者")
        target_kind = "inventory_request" if query is not None else "legacy_research"
        payload = query.model_dump(mode="json") if query is not None else None
        if employee_id is not None:
            _identifier(employee_id)
        if proposal_id is not None:
            _identifier(proposal_id)
        if acting_user_id is not None:
            _identifier(acting_user_id)
        async with self._transaction() as session:
            row = await self._plan(session, tenant_id)
            if row is None:
                raise InvalidStateTransition("服务计划未配置")
            _version(row, expected_version)
            if row.pending_cycle_key is not None and (proposal_id, acting_user_id, target_kind, payload, employee_id) != (
                row.proposal_id, row.acting_user_id, row.target_kind, row.inventory_query, row.employee_id,
            ):
                pending = await session.get(
                    ProspectingDiscoveryCycleRow, (tenant_id, row.pending_cycle_key)
                )
                if (
                    pending is None
                    or pending.run_id is not None
                    or pending.request_key_hash is not None
                    or pending.superseded_at is not None
                ):
                    raise InvalidStateTransition("探索批次恢复记录不一致")
                if pending.start_attempted_at is not None:
                    raise InvalidStateTransition(
                        "启动结果待确认的探索批次必须保留原提案和操作者"
                    )
                pending.superseded_at = now
                pending.superseded_by_version = row.version + 1
                row.pending_cycle_key = None
            row.target_kind, row.inventory_query, row.employee_id = target_kind, payload, employee_id
            row.proposal_id, row.acting_user_id = proposal_id, acting_user_id
            row.schedule_enabled, row.updated_at = enabled, now
            row.version += 1
            if row.last_run_at is not None:
                row.next_run_at = cast(
                    datetime, _stored_time(row.last_run_at)
                ) + timedelta(
                    hours=tier_policy(cast(ServiceTier, row.tier)).cadence_hours
                )
            elif row.next_run_at is None and enabled:
                row.next_run_at = now
            await session.flush()
            return _state(row)

    async def configure_inventory_schedule(
        self, tenant_id: TenantId, *, query: InventoryQuery, employee_id: str,
        acting_user_id: str, enabled: bool, expected_version: int, now: datetime,
    ) -> ServicePlanState:
        """只有显式确认的企业产品引用可成为周期目标；旧研究授权不自动升级。"""
        if not query.confirmed_refs:
            raise ValidationError("周期获客需要明确确认的企业产品资料")
        return await self.configure_schedule(tenant_id, proposal_id=None,
            acting_user_id=acting_user_id, enabled=enabled, expected_version=expected_version,
            now=now, query=query, employee_id=employee_id)

    async def reserve(
        self,
        tenant_id: TenantId,
        *,
        key: str,
        candidates: int,
        searches: int,
        contacts: int,
        now: datetime,
    ) -> None:
        """同键同值仅返回原结果；不确定外部调用仍保留已预留用量。"""
        _identifier(tenant_id, 40)
        _identifier(key, 200)
        now = _now(now)
        amounts = (candidates, searches, contacts)
        if any(type(value) is not int or value < 0 for value in amounts) or not any(
            amounts
        ):
            raise ValidationError("服务用量必须是非负整数且总量大于零")
        async with self._transaction() as session:
            row = await self._plan(session, tenant_id)
            if row is None or not _valid_period(row, now):
                raise InvalidStateTransition("服务计划未配置或已到期")
            prior = await session.get(
                ProspectingServiceReservationRow, (tenant_id, key)
            )
            if prior is not None:
                if _stored_time(prior.period_started_at) != _stored_time(
                    row.period_started_at
                ):
                    raise InvalidStateTransition("服务用量幂等键属于旧服务期")
                if (prior.candidates, prior.searches, prior.contacts) != amounts:
                    raise InvalidStateTransition("服务用量幂等键与原请求冲突")
                return
            policy = tier_policy(cast(ServiceTier, row.tier))
            if (
                row.candidates_reserved + candidates > policy.candidate_limit
                or row.searches_reserved + searches > policy.search_limit
                or row.contacts_reserved + contacts > policy.contact_limit
            ):
                raise InvalidStateTransition("服务期处理额度不足")
            session.add(
                ProspectingServiceReservationRow(
                    tenant_id=tenant_id,
                    reservation_key=key,
                    tier=row.tier,
                    candidates=candidates,
                    searches=searches,
                    contacts=contacts,
                    period_started_at=row.period_started_at,
                    created_at=now,
                )
            )
            row.candidates_reserved += candidates
            row.searches_reserved += searches
            row.contacts_reserved += contacts

    async def reservation(
        self, tenant_id: TenantId, *, key: str, now: datetime
    ) -> ServicePlanReservation | None:
        """同一事务重读有效账期与预留；旧Run不能借历史回执启动新的付费动作。"""
        _identifier(tenant_id, 40)
        _identifier(key, 200)
        now = _now(now)
        async with self._transaction() as session:
            row = await self._plan(session, tenant_id)
            if row is None or not _valid_period(row, now):
                return None
            receipt = await session.get(
                ProspectingServiceReservationRow, (tenant_id, key)
            )
            if receipt is None or _stored_time(
                receipt.period_started_at
            ) != _stored_time(row.period_started_at):
                return None
            return ServicePlanReservation(
                key=receipt.reservation_key,
                tier=cast(ServiceTier, receipt.tier),
                created_at=cast(datetime, _stored_time(receipt.created_at)),
                period_started_at=cast(
                    datetime, _stored_time(receipt.period_started_at)
                ),
                candidates=receipt.candidates,
                searches=receipt.searches,
                contacts=receipt.contacts,
            )

    async def begin_cycle(
        self, tenant_id: TenantId, now: datetime
    ) -> PendingDiscoveryCycle | None:
        """有效排程准备批次；已尝试启动的批次在暂停/到期后仍可恢复回执。"""
        _identifier(tenant_id, 40)
        now = _now(now)
        async with self._transaction() as session:
            row = await self._plan(session, tenant_id)
            if row is None:
                return None
            if row.pending_cycle_key is not None:
                pending = await session.get(
                    ProspectingDiscoveryCycleRow, (tenant_id, row.pending_cycle_key)
                )
                if (
                    pending is None
                    or pending.run_id is not None
                    or pending.request_key_hash is not None
                    or pending.superseded_at is not None
                ):
                    raise InvalidStateTransition("探索批次恢复记录不一致")
                if pending.start_attempted_at is not None or (
                    row.schedule_enabled and _valid_period(row, now)
                ):
                    return _cycle(pending)
                return None
            if not row.schedule_enabled or not _valid_period(row, now):
                return None
            if (
                row.next_run_at is None
                or cast(datetime, _stored_time(row.next_run_at)) > now
            ):
                return None
            if not row.acting_user_id or (row.target_kind == "legacy_research" and not row.proposal_id):
                raise InvalidStateTransition("周期探索授权身份缺失")
            due = cast(datetime, _stored_time(row.next_run_at))
            digest = hashlib.sha256(
                json.dumps([tenant_id, row.version, due.isoformat()]).encode()
            ).hexdigest()
            key = "prospecting-cycle:" + digest
            pending = ProspectingDiscoveryCycleRow(
                tenant_id=tenant_id,
                cycle_key=key,
                plan_version=row.version,
                target_kind=cast(Literal["legacy_research", "inventory_request"], row.target_kind),
                inventory_query=row.inventory_query,
                employee_id=row.employee_id,
                proposal_id=row.proposal_id,
                acting_user_id=row.acting_user_id,
                due_at=due,
                created_at=now,
                run_id=None,
                bound_at=None,
            )
            session.add(pending)
            row.pending_cycle_key = key
            await session.flush()
            return _cycle(pending)

    async def claim_cycle_start(
        self, tenant_id: TenantId, *, key: str, expected_version: int, now: datetime
    ) -> bool:
        """配置CAS与pending身份在同一行锁下确认；此后失败不得推断为未启动。"""
        _identifier(tenant_id, 40)
        _identifier(key, 200)
        now = _now(now)
        if type(expected_version) is not int or expected_version < 1:
            raise ValidationError("服务计划版本无效")
        async with self._transaction() as session:
            row = await self._plan(session, tenant_id)
            if (
                row is None
                or row.version != expected_version
                or row.pending_cycle_key != key
                or not row.schedule_enabled
                or not _valid_period(row, now)
            ):
                return False
            cycle = await session.get(ProspectingDiscoveryCycleRow, (tenant_id, key))
            if (
                cycle is None
                or cycle.run_id is not None
                or cycle.request_key_hash is not None
                or cycle.superseded_at is not None
                or (cycle.proposal_id, cycle.acting_user_id, cycle.target_kind, cycle.inventory_query, cycle.employee_id)
                != (row.proposal_id, row.acting_user_id, row.target_kind, row.inventory_query, row.employee_id)
            ):
                raise InvalidStateTransition("探索批次恢复记录不一致")
            if now < cast(datetime, _stored_time(cycle.created_at)):
                raise ValidationError("探索批次启动时间无效")
            if cycle.start_attempted_at is None:
                cycle.start_attempted_at = now
            return True

    async def bind_cycle(
        self, tenant_id: TenantId, *, key: str, run_id: str | None = None, now: datetime,
        request_key_hash: str | None = None,
    ) -> ServicePlanState:
        """接收同一批次的启动回执；即使服务已到期也允许完成已启动Run绑定。"""
        _identifier(tenant_id, 40)
        _identifier(key, 200)
        if (run_id is None) == (request_key_hash is None):
            raise ValidationError("探索批次必须具有唯一类型的回执")
        _identifier(run_id or request_key_hash or "")
        now = _now(now)
        async with self._transaction() as session:
            row = await self._plan(session, tenant_id)
            cycle = await session.get(ProspectingDiscoveryCycleRow, (tenant_id, key))
            if row is None or cycle is None:
                raise InvalidStateTransition("探索批次不存在")
            if cycle.target_kind != ("inventory_request" if request_key_hash else "legacy_research"):
                raise InvalidStateTransition("探索批次回执类型不匹配")
            if cycle.bound_at is not None:
                if (cycle.run_id, cycle.request_key_hash) != (run_id, request_key_hash):
                    raise InvalidStateTransition("探索批次已绑定其他Run")
                return _state(row)
            if (
                cycle.start_attempted_at is None
                or cycle.superseded_at is not None
                or row.pending_cycle_key != key
                or now < cast(datetime, _stored_time(cycle.created_at))
            ):
                raise InvalidStateTransition("探索批次回执与当前待处理批次不一致")
            cycle.run_id, cycle.request_key_hash, cycle.bound_at = run_id, request_key_hash, now
            row.pending_cycle_key, row.last_run_at = None, now
            row.next_run_at = now + timedelta(
                hours=tier_policy(cast(ServiceTier, row.tier)).cadence_hours
            )
            await session.flush()
            return _state(row)

    async def bind_inventory_cycle(
        self, tenant_id: TenantId, *, key: str, request_key_hash: str, now: datetime,
    ) -> ServicePlanState:
        """即时库存命中也绑定持久请求回执，不把 acquisition ID 伪装成 WorkflowRun。"""
        if len(request_key_hash) != 64 or any(c not in "0123456789abcdef" for c in request_key_hash):
            raise ValidationError("库存请求回执无效")
        return await self.bind_cycle(tenant_id, key=key, request_key_hash=request_key_hash, now=now)

    async def get_evidence(
        self, tenant_id: TenantId, account_id: ProspectAccountId
    ) -> tuple[ScreeningEvidence, ...]:
        """逐条重验payload与复合租户/企业键一致，不将损坏记录交给模型。"""
        _identifier(tenant_id, 40)
        _identifier(account_id, 40)
        async with self._reader() as session:
            rows = (
                await session.scalars(
                    select(ProspectingScreeningEvidenceRow)
                    .where(
                        ProspectingScreeningEvidenceRow.tenant_id == tenant_id,
                        ProspectingScreeningEvidenceRow.account_id == account_id,
                    )
                    .order_by(
                        ProspectingScreeningEvidenceRow.recorded_at,
                        ProspectingScreeningEvidenceRow.content_hash,
                    )
                )
            ).all()
            result = []
            for row in rows:
                if _content_hash(row.payload) != row.content_hash:
                    raise ValidationError("筛选证据内容与持久哈希不一致")
                try:
                    evidence = ScreeningEvidence.model_validate(row.payload)
                except PydanticValidationError:
                    raise ValidationError("筛选证据持久记录无效") from None
                if evidence.tenant_id != tenant_id or evidence.account_id != account_id:
                    raise ValidationError("筛选证据租户或企业绑定不一致")
                result.append(evidence)
            return tuple(result)

    async def append_evidence(
        self,
        tenant_id: TenantId,
        account_id: ProspectAccountId,
        evidence: ScreeningEvidence,
    ) -> None:
        """锁企业后按来源事实去重，首次采集/审核时间及完整内容hash保持不变。"""
        _identifier(tenant_id, 40)
        _identifier(account_id, 40)
        try:
            evidence = ScreeningEvidence.model_validate(evidence)
        except PydanticValidationError:
            raise ValidationError("筛选证据无效") from None
        if evidence.tenant_id != tenant_id or evidence.account_id != account_id:
            raise ValidationError("筛选证据租户或企业绑定不一致")
        payload = evidence.model_dump(mode="json")
        digest = _content_hash(payload)
        logical_payload = {
            key: value
            for key, value in payload.items()
            if key not in {"recorded_at", "reviewed_at"}
        }
        async with self._transaction() as session:
            account = await session.scalar(
                select(ProspectAccountRow.account_id)
                .where(
                    ProspectAccountRow.tenant_id == tenant_id,
                    ProspectAccountRow.account_id == account_id,
                )
                .with_for_update()
            )
            if account is None:
                raise ValidationError("筛选证据企业不存在")
            prior_rows = await session.scalars(
                select(ProspectingScreeningEvidenceRow).where(
                    ProspectingScreeningEvidenceRow.tenant_id == tenant_id,
                    ProspectingScreeningEvidenceRow.account_id == account_id,
                )
            )
            for prior in prior_rows:
                if _content_hash(prior.payload) != prior.content_hash:
                    raise ValidationError("筛选证据内容与持久哈希不一致")
                if (
                    prior.payload.get("tenant_id"),
                    prior.payload.get("account_id"),
                ) != (
                    tenant_id,
                    account_id,
                ):
                    raise ValidationError("筛选证据租户或企业绑定不一致")
                if {
                    key: value
                    for key, value in prior.payload.items()
                    if key not in {"recorded_at", "reviewed_at"}
                } == logical_payload:
                    return
            session.add(
                ProspectingScreeningEvidenceRow(
                    tenant_id=tenant_id,
                    account_id=account_id,
                    content_hash=digest,
                    payload=payload,
                    recorded_at=evidence.recorded_at,
                )
            )
