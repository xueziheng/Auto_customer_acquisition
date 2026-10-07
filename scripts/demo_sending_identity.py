"""用真实 PostgreSQL 演示 Slice 4A 发件身份服务链路。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from domains.sending_identity.errors import WarmupLimitExceededError
from domains.sending_identity.permissions import (
    Actor,
    Phase1SendingIdentityAuthorizer,
    ScopeLevel,
    SendingIdentityScope,
    StandardAuditLogger,
)
from domains.sending_identity.schemas import (
    AuthenticationResult,
    DeliveryEventRecord,
    DeliveryEventType,
    DomainRole,
    IdentityRegisterRequest,
    SendReservation,
)
from domains.sending_identity.service_impl import SendingIdentityServiceImpl
from infra.db.sending_identity_uow import SqlAlchemySendingIdentityUnitOfWork
from infra.db.session import create_engine_from
from infra.db.tables import (
    AuthenticationCheckRow,
    IdentityActionRow,
    OutboxEventRow,
    ReputationEventRow,
    SendingIdentityRow,
)
from shared.schemas.identifiers import (
    IdempotencyKey,
    SendingIdentityId,
    TenantId,
    new_id,
)

_AUDIT_LOGGER_NAME = "security.authorization.sending_identity.demo"
_FAILURE_MESSAGE = "发件身份演示运行失败"


@dataclass
class _Clock:
    """演示专用可推进 UTC 时钟。"""

    current: datetime

    def now(self) -> datetime:
        return self.current

    def advance(self, *, days: int) -> None:
        self.current += timedelta(days=days)


class _SafeAuditFormatter(logging.Formatter):
    """只渲染授权契约允许的固定字段。"""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "action": getattr(record, "action", ""),
            "actor": getattr(record, "actor", ""),
            "message": "授权审计",
            "rule": getattr(record, "rule", ""),
            "scope": getattr(record, "scope", ""),
            "tenant_id": getattr(record, "tenant_id", ""),
        }
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


def _configure_safe_audit_logging() -> None:
    """隔离 root logger，只保留固定字段的授权审计。"""
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(logging.NullHandler())
    root.setLevel(logging.CRITICAL)

    logger = logging.getLogger(_AUDIT_LOGGER_NAME)
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_SafeAuditFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def _boss_actor() -> Actor:
    return Actor(
        actor_id="boss:sending-identity-demo",
        role="boss",
        scope=SendingIdentityScope(level=ScopeLevel.TENANT),
    )


def _system_actor(identity_id: SendingIdentityId) -> Actor:
    return Actor(
        actor_id="system:sending-identity-demo",
        role="system",
        scope=SendingIdentityScope(
            level=ScopeLevel.SYSTEM,
            allowed_identity_ids=frozenset({identity_id}),
        ),
    )


class _UowFactory:
    """把 session factory 与唯一服务时钟装配为 tenant-scoped UoW。"""

    def __init__(
        self,
        factory: async_sessionmaker,
        now: Callable[[], datetime],
    ) -> None:
        self._factory = factory
        self._now = now

    def __call__(
        self,
        tenant_id: TenantId,
    ) -> Any:
        # 具体 UoW 在 __aenter__ 动态绑定七个 repository，静态结构无法提前展开。
        return SqlAlchemySendingIdentityUnitOfWork(
            self._factory,
            tenant_id,
            now=self._now,
        )


async def _reserve_if_available(
    service: SendingIdentityServiceImpl,
    tenant_id: TenantId,
    identity_id: SendingIdentityId,
    actor: Actor,
    reservation_key: IdempotencyKey,
) -> SendReservation | None:
    """只把并发达到当日上限视为预期结果。"""
    try:
        return await service.reserve_send_slot(
            tenant_id,
            identity_id,
            reservation_key,
            True,
            actor=actor,
        )
    except WarmupLimitExceededError:
        return None


async def _read_summary(
    factory: async_sessionmaker,
    tenant_id: TenantId,
    identity_ids: tuple[SendingIdentityId, SendingIdentityId],
    reservation_success_count: int,
) -> dict[str, object]:
    """以只读查询形成安全摘要，不输出地址、域名或外部引用。"""
    async with factory() as session:
        identities = (
            await session.execute(
                select(SendingIdentityRow).where(
                    SendingIdentityRow.tenant_id == tenant_id
                )
            )
        ).scalars().all()
        authentication_checks = (
            await session.execute(
                select(AuthenticationCheckRow).where(
                    AuthenticationCheckRow.tenant_id == tenant_id
                )
            )
        ).scalars().all()
        identity_actions = (
            await session.execute(
                select(IdentityActionRow).where(
                    IdentityActionRow.tenant_id == tenant_id
                )
            )
        ).scalars().all()
        reputation_events = (
            await session.execute(
                select(ReputationEventRow).where(
                    ReputationEventRow.tenant_id == tenant_id
                )
            )
        ).scalars().all()
        outbox_events = (
            await session.execute(
                select(OutboxEventRow).where(OutboxEventRow.tenant_id == tenant_id)
            )
        ).scalars().all()

    state_by_id = {row.identity_id: row.state for row in identities}
    final_states = [state_by_id[str(identity_id)] for identity_id in identity_ids]
    history_counts = {
        "authentication_checks": len(authentication_checks),
        "identity_actions": len(identity_actions),
        "reputation_events": len(reputation_events),
    }
    if (
        final_states != ["suspended", "suspended"]
        or reservation_success_count != 5
        or history_counts
        != {
            "authentication_checks": 2,
            "identity_actions": 10,
            "reputation_events": 3,
        }
        or len(outbox_events) != 5
    ):
        raise RuntimeError("演示持久化结果不符合预期")
    return {
        "final_states": final_states,
        "history_counts": history_counts,
        "identity_ids": [str(identity_id) for identity_id in identity_ids],
        "outbox_count": len(outbox_events),
        "reservation_success_count": reservation_success_count,
        "tenant_id": str(tenant_id),
    }


async def _exercise_service(
    service: SendingIdentityServiceImpl,
    tenant_id: TenantId,
    clock: _Clock,
    factory: async_sessionmaker,
) -> dict[str, object]:
    """经公共服务完成认证、预热、预留、聚合信誉与域级熔断。"""
    suffix = str(tenant_id).split("_", maxsplit=1)[-1].lower()
    domain = f"cold-{suffix}.example"
    boss = _boss_actor()
    identity_ids: list[SendingIdentityId] = []
    system_actors: list[Actor] = []

    for index in range(2):
        identity_id = await service.register(
            tenant_id,
            IdentityRegisterRequest(
                address=f"sender-{index + 1}@{domain}",
                domain=domain,
                role=DomainRole.COLD_OUTREACH,
                connector_ref=f"demo_connector_{index + 1}",
            ),
            actor=boss,
        )
        system = _system_actor(identity_id)
        await service.begin_authentication(
            tenant_id,
            identity_id,
            actor=boss,
        )
        await service.record_authentication_result(
            tenant_id,
            identity_id,
            AuthenticationResult(
                checked_at=clock.now(),
                spf_passed=True,
                dkim_passed=True,
                dmarc_passed=True,
                failures=(),
                check_ref=f"demo_auth_{index + 1}",
            ),
            actor=system,
        )
        await service.start_warmup(
            tenant_id,
            identity_id,
            100,
            actor=boss,
        )
        identity_ids.append(identity_id)
        system_actors.append(system)

    day1_results = await asyncio.gather(
        *(
            _reserve_if_available(
                service,
                tenant_id,
                identity_ids[0],
                system_actors[0],
                IdempotencyKey(f"demo-day1-{index}"),
            )
            for index in range(20)
        )
    )
    successful_day1 = [result for result in day1_results if result is not None]
    if len(successful_day1) != 5:
        raise RuntimeError("预热首日并发额度结果不符合预期")
    first_reservation = successful_day1[0]
    retried = await service.reserve_send_slot(
        tenant_id,
        identity_ids[0],
        first_reservation.reservation_key,
        True,
        actor=system_actors[0],
    )
    if retried.reservation_id != first_reservation.reservation_id:
        raise RuntimeError("发送预留幂等结果不符合预期")

    clock.advance(days=28)
    await asyncio.gather(
        *(
            service.advance_warmup(
                tenant_id,
                identity_id,
                actor=system_actor,
            )
            for identity_id, system_actor in zip(
                identity_ids,
                system_actors,
                strict=True,
            )
        )
    )
    day29_initial_results = await asyncio.gather(
        *(
            service.reserve_send_slot(
                tenant_id,
                identity_id,
                IdempotencyKey(f"demo-day29-{identity_index}-{slot}"),
                True,
                actor=system_actor,
            )
            for identity_index, (identity_id, system_actor) in enumerate(
                zip(identity_ids, system_actors, strict=True),
                start=1,
            )
            for slot in range(24)
        )
    )
    if len(day29_initial_results) != 48:
        raise RuntimeError("激活日首批发送预留结果不符合预期")

    events = (
        DeliveryEventRecord(
            tenant_id=tenant_id,
            identity_id=identity_ids[0],
            event_type=DeliveryEventType.HARD_BOUNCED,
            occurred_at=clock.now(),
            dedup_key=IdempotencyKey("demo-hard-bounce-1"),
            source_ref="demo_delivery_1",
        ),
        DeliveryEventRecord(
            tenant_id=tenant_id,
            identity_id=identity_ids[1],
            event_type=DeliveryEventType.HARD_BOUNCED,
            occurred_at=clock.now(),
            dedup_key=IdempotencyKey("demo-hard-bounce-2"),
            source_ref="demo_delivery_2",
        ),
        DeliveryEventRecord(
            tenant_id=tenant_id,
            identity_id=identity_ids[0],
            event_type=DeliveryEventType.HARD_BOUNCED,
            occurred_at=clock.now(),
            dedup_key=IdempotencyKey("demo-hard-bounce-3"),
            source_ref="demo_delivery_3",
        ),
    )
    for event in events[:2]:
        created = await service.record_delivery_event(
            tenant_id,
            event.identity_id,
            event,
            actor=system_actors[identity_ids.index(event.identity_id)],
        )
        if not created:
            raise RuntimeError("投递事件首次记录失败")

    day29_final_results = await asyncio.gather(
        *(
            service.reserve_send_slot(
                tenant_id,
                identity_id,
                IdempotencyKey(f"demo-day29-{identity_index}-24"),
                True,
                actor=system_actor,
            )
            for identity_index, (identity_id, system_actor) in enumerate(
                zip(identity_ids, system_actors, strict=True),
                start=1,
            )
        )
    )
    if len(day29_final_results) != 2:
        raise RuntimeError("激活日末批发送预留结果不符合预期")

    created = await service.record_delivery_event(
        tenant_id,
        events[2].identity_id,
        events[2],
        actor=system_actors[0],
    )
    if not created:
        raise RuntimeError("投递事件首次记录失败")
    duplicate_created = await service.record_delivery_event(
        tenant_id,
        events[0].identity_id,
        events[0],
        actor=system_actors[0],
    )
    if duplicate_created:
        raise RuntimeError("投递事件幂等结果不符合预期")

    return await _read_summary(
        factory,
        tenant_id,
        (identity_ids[0], identity_ids[1]),
        len(successful_day1),
    )


async def main() -> None:
    """只从最小环境读取数据库连接并释放装配层持有的 engine。"""
    database_url = os.environ["DATABASE_URL"]
    _configure_safe_audit_logging()
    clock = _Clock(datetime(2026, 8, 11, 12, tzinfo=UTC))
    tenant_id = TenantId(new_id("tn"))
    engine = create_engine_from(database_url)
    try:
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        service = SendingIdentityServiceImpl(
            _UowFactory(factory, clock.now),
            Phase1SendingIdentityAuthorizer(tenant_id),
            StandardAuditLogger(_AUDIT_LOGGER_NAME),
            now=clock.now,
        )
        summary = await _exercise_service(service, tenant_id, clock, factory)
    finally:
        await engine.dispose()
    print(
        json.dumps(
            summary,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _run(entrypoint: Callable[[], Coroutine[Any, Any, None]]) -> int:
    """在进程边界固定失败输出，不回显 DSN、driver 或 traceback。"""
    try:
        asyncio.run(entrypoint())
    except Exception:  # noqa: BLE001 - 进程安全边界必须收敛所有失败
        print(_FAILURE_MESSAGE, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_run(main))
