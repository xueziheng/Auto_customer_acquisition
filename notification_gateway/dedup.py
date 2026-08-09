"""通知去重存储契约（S3-9；``NotificationDedupStore`` Protocol）。

设计要点：
- **接口优先用 Protocol**（AGENTS.md 编码约定）：实现与接口分离，
  Postgres 实现在 ``infra/db/repositories/notifications.py``。
- **逐渠道去重**：``(tenant_id, dedup_key, channel_name)`` 是去重粒度——同一
  通知不同渠道各自独立持久化，部分渠道失败只续投失败渠道，不重复投递已成功渠道。
- **fail closed**：所有方法显式带 ``tenant_id``，实现不得绕过租户过滤
  （硬边界 8）。
- **失败重试**：``record_failure`` 递增 attempts、置确定性正退避
  （``next_attempt_at``）、写脱敏 ``last_error``（不含异常消息/payload/凭证）；
  ``record_success`` 标记 delivered 并清空重试字段。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from shared.schemas.identifiers import TenantId


@runtime_checkable
class NotificationDedupStore(Protocol):
    """durable 通知去重存储契约。

    ``should_dispatch`` 负责建 durable 行并抑制重复投递：已 delivered 或退避期
    内返回 False，pending 且到点返回 True。``record_failure``/``record_success``
    由投递方在渠道投递结果后调用，持久化重试/完成状态。
    """

    async def should_dispatch(
        self, tenant_id: TenantId, dedup_key: str, channel_name: str
    ) -> bool:
        """判断该 ``(tenant, dedup, channel)`` 是否应投递，同时建 durable 行。"""
        ...

    async def record_failure(
        self,
        tenant_id: TenantId,
        dedup_key: str,
        channel_name: str,
        *,
        error: BaseException,
    ) -> None:
        """记录投递失败：递增 attempts、置正退避、写脱敏错误。"""
        ...

    async def record_success(
        self, tenant_id: TenantId, dedup_key: str, channel_name: str
    ) -> None:
        """记录投递成功：标记 delivered、清空重试字段。"""
        ...
