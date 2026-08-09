"""``NotificationDedupStore`` 的 Postgres 实现（infra 层）。

设计要点（对齐 test_notification_dedup 行为契约）：
- **租户过滤失败关闭**：每个方法显式带 ``tenant_id``，全部 SQL 都按其过滤
  （硬边界 8）；A 租户的操作不影响 B，B 的 delivered 也不掩盖 A 的待投递状态。
- **逐渠道去重**：``UNIQUE(tenant_id, dedup_key, channel_name)``——同一通知
  不同渠道各自一行；``INSERT ... ON CONFLICT DO NOTHING`` 幂等建行，重复
  ``should_dispatch`` 不建重复行；``record_success`` 后 delivered 抑制重投。
- **原子认领（有限租约）**：``should_dispatch`` 幂等建行后以一条**条件 UPDATE
  RETURNING** 抢占到期行——``WHERE tenant_id=:t AND dedup_key=:k AND channel_name=:c
  AND status='pending' AND (next_attempt_at IS NULL OR next_attempt_at <= now)``；
  并发同 (tenant, dedup, channel) 仅一个返回 True（check-then-act 会被行锁+条件
  串行化）；认领**不改 status**（保持 pending），以 ``next_attempt_at = now + lease``
  作有限租约——进程崩溃后租约到期可被重新认领重试。
- **失败重试**：``record_failure`` 递增 ``attempts``、置确定性正退避
  （``next_attempt_at`` 随 attempts 严格增长）、写脱敏 ``last_error``（只留异常
  类型名，不含异常消息/payload/凭证文本）；``record_success`` 标记 delivered、
  写 ``delivered_at``、清空 ``next_attempt_at``/``last_error``。
- **独立事务**：每个方法一个会话/事务并 commit，投递状态 durable 可见。
- ``now`` 可注入（测试假时钟确定化），默认 UTC 当前时间。
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infra.db.tables import NotificationDeliveryRow
from shared.schemas.identifiers import TenantId, new_id


class PostgresNotificationDedupStore:
    """``NotificationDedupStore`` 的 SQLAlchemy/Postgres 实现。

    构造不触数据库；``now`` 为可选时间源（默认 ``datetime.now(UTC)``），测试
    注入假时钟使退避调度确定可测。方法签名与 Protocol 一致。
    """

    # 有限租约时长：认领后到期前他人不可重投；进程崩溃后租约到期可重新认领。
    _LEASE = timedelta(seconds=300)

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._factory = factory
        self._now = now if now is not None else lambda: datetime.now(UTC)

    async def should_dispatch(
        self, tenant_id: TenantId, dedup_key: str, channel_name: str
    ) -> bool:
        """建 durable 行并**原子认领**该 ``(tenant, dedup, channel)``（有限租约）。

        - 幂等建行后以一条条件 ``UPDATE ... RETURNING`` 抢占到期行：``status='pending'``
          且 ``next_attempt_at`` 为空或已到期 → 抢占成功返回 True，并把
          ``next_attempt_at`` 置为 ``now + lease`` 作租约（**不改 status**）。
        - delivered → False（不重复投递）；租约/退避期内 → False（未到点不重试）。
        - 并发同键仅一个 True（check-then-act 被行锁 + WHERE 条件串行化）。
        - 租户条件显式 fail-closed：UPDATE 的 WHERE 恒含 ``tenant_id``（硬边界 8）。
        """
        now = self._now()
        lease_expiry = now + self._LEASE
        async with self._factory() as session:
            await self._ensure_row(session, tenant_id, dedup_key, channel_name)
            claimed = await session.execute(
                update(NotificationDeliveryRow)
                .where(
                    NotificationDeliveryRow.tenant_id == str(tenant_id),
                    NotificationDeliveryRow.dedup_key == dedup_key,
                    NotificationDeliveryRow.channel_name == channel_name,
                    NotificationDeliveryRow.status == "pending",
                    or_(
                        NotificationDeliveryRow.next_attempt_at.is_(None),
                        NotificationDeliveryRow.next_attempt_at <= now,
                    ),
                )
                .values(next_attempt_at=lease_expiry)
                .returning(NotificationDeliveryRow.delivery_id)
            )
            decision = claimed.first() is not None
            await session.commit()
        return decision

    async def record_failure(
        self,
        tenant_id: TenantId,
        dedup_key: str,
        channel_name: str,
        *,
        error: BaseException,
    ) -> None:
        """记录投递失败：递增 ``attempts``、置正退避、写脱敏 ``last_error``。

        退避公式随 attempts 严格增长（``base * 2^(attempts-1)``）；``last_error``
        只写异常类型名，不落异常消息/payload/凭证文本（与 outbox 投递同纪律）。
        """
        now = self._now()
        async with self._factory() as session:
            row = await self._get_or_create_row(
                session, tenant_id, dedup_key, channel_name
            )
            row.attempts += 1
            row.status = "pending"
            row.next_attempt_at = self._backoff_after(now, row.attempts)
            row.last_error = self._safe_error(error)
            await session.commit()

    async def record_success(
        self, tenant_id: TenantId, dedup_key: str, channel_name: str
    ) -> None:
        """标记 delivered、写 ``delivered_at``，清空重试字段。

        之后 ``should_dispatch`` 返回 False——同一 ``(tenant, dedup, channel)``
        不重复投递。
        """
        now = self._now()
        async with self._factory() as session:
            row = await self._get_or_create_row(
                session, tenant_id, dedup_key, channel_name
            )
            row.status = "delivered"
            row.delivered_at = now
            row.next_attempt_at = None
            row.last_error = None
            await session.commit()

    async def _ensure_row(
        self,
        session: AsyncSession,
        tenant_id: TenantId,
        dedup_key: str,
        channel_name: str,
    ) -> None:
        """幂等建行（``INSERT ... ON CONFLICT DO NOTHING``），不读回。

        ``UNIQUE(tenant_id, dedup_key, channel_name)`` 兜底；是否可投递由后续
        条件 UPDATE 认领判定——不在此处 read-then-act。
        """
        await session.execute(
            insert(NotificationDeliveryRow)
            .values(
                delivery_id=new_id("nd"),
                tenant_id=str(tenant_id),
                dedup_key=dedup_key,
                channel_name=channel_name,
            )
            .on_conflict_do_nothing(
                index_elements=["tenant_id", "dedup_key", "channel_name"]
            )
        )

    async def _get_or_create_row(
        self,
        session: AsyncSession,
        tenant_id: TenantId,
        dedup_key: str,
        channel_name: str,
    ) -> NotificationDeliveryRow:
        """取 ``(tenant, dedup, channel)`` 的 durable 行；不存在则创建（幂等）。

        ``INSERT ... ON CONFLICT DO NOTHING``：重复调用只建一行，
        ``UNIQUE(tenant_id, dedup_key, channel_name)`` 兜底；随后同事务读回。
        """
        await session.execute(
            insert(NotificationDeliveryRow)
            .values(
                delivery_id=new_id("nd"),
                tenant_id=str(tenant_id),
                dedup_key=dedup_key,
                channel_name=channel_name,
            )
            .on_conflict_do_nothing(
                index_elements=["tenant_id", "dedup_key", "channel_name"]
            )
        )
        row = (
            await session.execute(
                select(NotificationDeliveryRow).where(
                    NotificationDeliveryRow.tenant_id == str(tenant_id),
                    NotificationDeliveryRow.dedup_key == dedup_key,
                    NotificationDeliveryRow.channel_name == channel_name,
                )
            )
        ).scalars().first()
        if row is None:  # pragma: no cover - 同事务内插入必可读
            raise RuntimeError("notification_deliveries 行创建失败")
        return row

    @staticmethod
    def _safe_error(exc: BaseException) -> str:
        """脱敏错误：只留异常类型名，不落异常消息/payload（防凭证进 last_error）。"""
        return type(exc).__name__

    @staticmethod
    def _backoff_after(now: datetime, attempts: int) -> datetime:
        """失败后下次可重试时间：确定性正退避，且随 attempts 严格增长。

        公式 ``base * 2^(attempts-1)``（attempts 从 1 递增）：第 1 次失败后
        30s、第 2 次 60s、第 3 次 120s……``delay2 > delay1 > 0``
        （test_store_backoff_grows_deterministically）。``base`` 固定 30s，
        不依赖异常消息/payload，天然脱敏。
        """
        base = timedelta(seconds=30)
        return now + base * (2 ** (attempts - 1))
