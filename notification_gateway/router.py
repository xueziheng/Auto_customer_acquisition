"""S3-9 通知路由：注入式 dedup store + routing policy，URGENT 多渠道并发 fan-out。

设计要点：
- **policy 拥有渠道选择**：router 不内建「优先级→渠道」映射（本切片无 in-app/email
  渠道）；``RoutingPolicy.channels_for`` 决定投哪些渠道（含 LOW），router 只投返回结果。
- **注入式 durable dedup store**：``NotificationDedupStore``（``notification_gateway.dedup``
  的 Protocol）由构造注入，router 不自持内存判重。
- **并发 fan-out**：policy 返回的渠道经 ``asyncio.gather`` 并发投递；单渠道失败不阻塞
  成功渠道——失败渠道经 ``record_failure`` 记为可重试（pending + 退避），成功渠道
  ``record_success``。
- **store 写失败不中断**：``record_failure``/``record_success`` 的 store 写若抛错，
  记录为持久化失败并继续处理其余渠道的持久化，不泄漏 store/DB 异常文本。
- **路由失败以固定脱敏 TransientError 呈现**：任一渠道失败或持久化失败即抛可重试
  ``TransientError``，不阻塞主业务事务；消息固定脱敏（不含渠道异常文本/凭证），
  store 只收到原始异常，``record_failure`` 内部自会脱敏为类型名。
"""
from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable

from notification_gateway.dedup import NotificationDedupStore
from notification_gateway.models import Notification, NotificationChannel
from shared.errors import TransientError


@runtime_checkable
class RoutingPolicy(Protocol):
    """渠道选择策略 —— 拥有「投哪些渠道」的决定权。

    router 不判断优先级/业务语义，只投 ``channels_for`` 返回的渠道；
    ``available`` 为已注册渠道，返回值必须是其中的子集。
    """

    def channels_for(
        self,
        notification: Notification,
        available: list[NotificationChannel],
    ) -> list[NotificationChannel]:
        """从 ``available`` 中选出该通知应投递的渠道。"""
        ...


class NotificationRouter:
    """通知路由器：policy 选渠道 → store 判重 → 并发投递。

    ``store`` 必须是 durable ``NotificationDedupStore``（非内存）；``policy`` 实现
    ``RoutingPolicy``；渠道经 ``register_channel`` 注册后由 policy 选取。
    """

    def __init__(self, store: NotificationDedupStore, policy: RoutingPolicy) -> None:
        self._store = store
        self._policy = policy
        self._channels: list[NotificationChannel] = []

    def register_channel(self, channel: NotificationChannel) -> None:
        """注册一个渠道；policy 从已注册渠道中选取。"""
        self._channels.append(channel)

    async def dispatch(self, notification: Notification) -> None:
        """经 policy 选渠道、经 store 判重后并发投递。

        任一渠道失败：失败渠道经 ``record_failure`` 记为可重试，成功渠道照常
        ``record_success``。``record_failure``/``record_success`` 的 store 写若抛错，
        记持久化失败并继续处理其余渠道。最终有任何失败时只抛**固定脱敏、可重试**
        ``TransientError``（绝不泄漏 DB/渠道异常文本）。全部成功且持久化完成或被
        store 抑制则正常返回。
        """
        channels = self._policy.channels_for(notification, self._channels)

        async def _attempt(
            channel: NotificationChannel,
        ) -> NotificationChannel | None:
            if not await self._store.should_dispatch(
                notification.tenant_id, notification.dedup_key, channel.name
            ):
                return None  # store 抑制（已投递 / 退避期内）
            await channel.deliver(notification)
            return channel

        results = await asyncio.gather(
            *(_attempt(ch) for ch in channels),
            return_exceptions=True,
        )

        failures: list[NotificationChannel] = []
        persistence_failed: list[str] = []
        for channel, result in zip(channels, results):
            if result is None:
                continue
            if isinstance(result, BaseException):
                failures.append(channel)
                try:
                    await self._store.record_failure(
                        notification.tenant_id,
                        notification.dedup_key,
                        channel.name,
                        error=result,
                    )
                except Exception:  # noqa: BLE001  store/DB 级异常：不得中断其他渠道持久化
                    persistence_failed.append(channel.name)
            else:
                try:
                    await self._store.record_success(
                        notification.tenant_id,
                        notification.dedup_key,
                        channel.name,
                    )
                except Exception:  # noqa: BLE001  store/DB 级异常：不得中断其他渠道持久化
                    persistence_failed.append(channel.name)

        if failures or persistence_failed:
            context: dict[str, str] = {}
            if failures:
                context["failed_channels"] = ",".join(ch.name for ch in failures)
            if persistence_failed:
                context["persistence_failed_channels"] = ",".join(persistence_failed)
            raise TransientError(
                "通知投递部分失败：渠道失败或状态持久化失败，可重试",
                context=context,
            )
