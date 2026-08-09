"""通知去重存储契约（S3-9；``NotificationDedupStore`` Protocol）。

设计要点：
- **接口优先用 Protocol**（AGENTS.md 编码约定）：实现与接口分离，
  Postgres 实现在 ``infra/db/repositories/notifications.py``。
- **逐渠道去重**：``(tenant_id, dedup_key, channel_name)`` 是去重粒度——同一
  通知不同渠道各自独立持久化，部分渠道失败只续投失败渠道，不重复投递已成功渠道。
- **fail closed**：所有方法显式带 ``tenant_id``，实现不得绕过租户过滤
  （硬边界 8）。
- **fencing claim**：``should_dispatch`` 成功时返回随机 claim token；完成、失败或
  永久拒绝必须携带同一 token，过期 owner 的写入条件失败且不覆盖新 owner。
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

    ``should_dispatch`` 负责建 durable 行并抑制重复投递：已终态或退避期内返回
    ``None``，pending 且到点时返回随机 claim token。所有结果写必须携带该 token，
    返回 ``False`` 表示旧 owner 已失去租约，不得覆盖当前 owner。
    """

    async def should_dispatch(
        self, tenant_id: TenantId, dedup_key: str, channel_name: str
    ) -> str | None:
        """原子认领该 ``(tenant, dedup, channel)``，返回不可预测 token 或 ``None``。"""
        ...

    async def record_failure(
        self,
        tenant_id: TenantId,
        dedup_key: str,
        channel_name: str,
        *,
        claim_token: str,
        error: BaseException,
    ) -> bool:
        """记录瞬时投递失败；仅当前 claim 可写入，返回是否成功持久化。"""
        ...

    async def record_success(
        self,
        tenant_id: TenantId,
        dedup_key: str,
        channel_name: str,
        *,
        claim_token: str,
    ) -> bool:
        """记录投递成功；仅当前 claim 可标记 delivered，返回是否成功持久化。"""
        ...

    async def record_rejection(
        self,
        tenant_id: TenantId,
        dedup_key: str,
        channel_name: str,
        *,
        claim_token: str,
        error: BaseException,
    ) -> bool:
        """记录永久策略拒绝；仅当前 claim 可置终态，拒绝不得进入重试。"""
        ...
