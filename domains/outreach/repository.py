"""触达域存储接口。

**内部实现，其他域不得导入。**
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Protocol, runtime_checkable

from domains.outreach.models import (
    Campaign,
    Enrollment,
    EnrollmentState,
    SuppressionEntry,
)
from shared.schemas.identifiers import (
    CampaignId,
    ContactPointId,
    EnrollmentId,
    ProspectAccountId,
    TenantId,
)


@runtime_checkable
class CampaignRepository(Protocol):
    async def add(self, campaign: Campaign) -> None: ...

    async def get(
        self, tenant_id: TenantId, campaign_id: CampaignId
    ) -> Campaign | None: ...

    async def update(self, campaign: Campaign) -> None: ...

    async def add_version(self, campaign: Campaign) -> None:
        """存边界新版本。**旧版本保留**——审计要能回答
        「上个月实际执行的是哪一版边界」。"""
        ...

    async def list_active(self, tenant_id: TenantId) -> list[Campaign]: ...


@runtime_checkable
class EnrollmentRepository(Protocol):
    async def add(self, enrollment: Enrollment) -> None: ...

    async def get(
        self, tenant_id: TenantId, enrollment_id: EnrollmentId
    ) -> Enrollment | None: ...

    async def update(self, enrollment: Enrollment) -> None: ...

    async def find_active_for_account(
        self, tenant_id: TenantId, account_id: ProspectAccountId
    ) -> Enrollment | None:
        """查该企业的活跃 enrollment（跨所有 Campaign）。

        入组前必查：两个序列同时给一家公司发信，客户会收到两套说辞。
        """
        ...

    async def list_due(
        self, tenant_id: TenantId, now: datetime, limit: int
    ) -> list[Enrollment]:
        """到达发送时间的 enrollment。``scheduler_worker`` 的扫描入口。

        实现要求：``next_send_at <= now`` 且状态可发送。要用
        ``FOR UPDATE SKIP LOCKED`` 或等价手段——多个扫描周期重叠时
        不能取到同一批（那会导致重复发送尝试，靠幂等键兜底但没必要
        制造冲突）。
        """
        ...

    async def stop_all_for_target(
        self,
        tenant_id: TenantId,
        contact_point_id: ContactPointId | None,
        account_id: ProspectAccountId | None,
        new_state: EnrollmentState,
        reason: str,
    ) -> int:
        """批量停止某联系人/企业的所有活跃 enrollment，返回停了几条。

        抑制生效时调用。跨 Campaign。
        """
        ...

    async def stop_all_for_identity(
        self, tenant_id: TenantId, sending_identity_id: str, reason: str
    ) -> int:
        """发件身份被熔断时挂起其名下所有序列。"""
        ...


@runtime_checkable
class SuppressionRepository(Protocol):
    async def add(self, entry: SuppressionEntry) -> None:
        """加入抑制名单。幂等：重复加入不报错。**永不提供删除方法。**

        移除抑制（几乎不应发生）走单独的人工审批流程，直接操作数据库，
        并留审计——不给代码路径就不会被误用。
        """
        ...

    async def is_suppressed(
        self,
        tenant_id: TenantId,
        contact_point_id: ContactPointId | None,
        account_id: ProspectAccountId | None,
    ) -> bool:
        """查抑制状态。

        **发送关键路径上的查询**：必须有覆盖索引；服务不可用时调用方
        应拒绝发送而不是放行。
        """
        ...

    async def list_recent(
        self, tenant_id: TenantId, limit: int
    ) -> list[SuppressionEntry]: ...


@runtime_checkable
class QuotaRepository(Protocol):
    """Campaign 每日额度计数。"""

    async def increment_new_contacts(
        self, tenant_id: TenantId, campaign_id: CampaignId, on_day: date
    ) -> int:
        """原子递增，返回递增后的值。调用方比较上限决定放行与否。

        先递增再比较、超了就回滚（或用条件更新），不要先读再写——
        并发下会突破老板批准的上限。
        """
        ...

    async def increment_total_messages(
        self, tenant_id: TenantId, campaign_id: CampaignId, on_day: date
    ) -> int: ...

    async def get_usage(
        self, tenant_id: TenantId, campaign_id: CampaignId, on_day: date
    ) -> tuple[int, int]:
        """返回 ``(new_contacts_used, total_messages_used)``。"""
        ...
