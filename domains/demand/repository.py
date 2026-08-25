"""需求域存储接口。

**内部实现，其他域不得导入。**

每个方法签名都带 ``tenant_id``（硬边界 8）。不要为了"方便"提供
不带租户参数的查询——那个方便会在多租户上线时变成数据泄露。
"""

from __future__ import annotations

from typing import Protocol, Self, runtime_checkable

from domains.demand.models import (
    DemandSignal,
    NeedCluster,
    NeedHypothesis,
    ValidatedNeed,
)
from shared.events.bus import EventBus
from shared.schemas.identifiers import (
    ArtifactId,
    DemandSignalId,
    NeedClusterId,
    NeedHypothesisId,
    ProspectAccountId,
    TenantId,
    ValidatedNeedId,
)


@runtime_checkable
class SnapshotArtifactEvidenceRepository(Protocol):
    """只暴露网页快照证据绑定，不让 demand 域依赖 Artifact Store 内部类型。"""

    async def matches_web_snapshot(
        self,
        tenant_id: TenantId,
        artifact_id: ArtifactId,
        content_hash: str,
    ) -> bool: ...


@runtime_checkable
class DemandSignalRepository(Protocol):
    async def add(self, signal: DemandSignal) -> bool:
        """来源身份冲突返回 False（不入库）；True=新插入。

        dedup identity = (tenant_id, entity_name, signal_type, source_type,
        source_id)——全非空五列（规格 §4；page_hash 可空 tuple 方案已否决）。
        """
        ...

    async def get(
        self, tenant_id: TenantId, signal_id: DemandSignalId
    ) -> DemandSignal | None: ...

    async def find_duplicate(
        self,
        tenant_id: TenantId,
        entity_name: str,
        signal_type: str,
        source_type: str,
        source_id: str,
    ) -> DemandSignal | None:
        """按来源身份 5 列查重复信号（同事务重读胜者用）。"""
        ...

    async def discard(
        self,
        tenant_id: TenantId,
        signal_id: DemandSignalId,
        reason: str,
    ) -> DemandSignal | None:
        """tenant-bound SELECT ... FOR UPDATE，返回转换前快照（规格 §6.1）：
        不存在 → None；CAPTURED → 同事务 UPDATE 为 discarded+reason 后返回
        更新前 snapshot；DISCARDED/LINKED_TO_HYPOTHESIS → 不改动返回当前
        snapshot。调用方不得把返回对象当作 DB 当前态。"""
        ...

    async def list_unlinked(
        self, tenant_id: TenantId, limit: int
    ) -> list[DemandSignal]:
        """列出尚未关联到假设的信号，供假设生成任务消费。"""
        ...

    async def list_for_radar(
        self,
        tenant_id: TenantId,
        *,
        signal_type: str | None,
        status: str | None,
        limit: int,
    ) -> list[DemandSignal]: ...


@runtime_checkable
class DemandUnitOfWork(Protocol):
    """demand 域事务边界（域级接口；实现为 SqlAlchemyDemandUnitOfWork）。"""

    signals: DemandSignalRepository
    snapshot_artifacts: SnapshotArtifactEvidenceRepository
    hypotheses: NeedHypothesisRepository
    needs: ValidatedNeedRepository
    clusters: NeedClusterRepository
    bus: EventBus

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None: ...


@runtime_checkable
class NeedHypothesisRepository(Protocol):
    async def add(self, hypothesis: NeedHypothesis) -> bool:
        """新插入返回 True；活跃冲突（(tenant_id, account_id, category) 部分唯一
        索引命中）返回 False（spec D2）。"""
        ...

    async def get(
        self, tenant_id: TenantId, hypothesis_id: NeedHypothesisId
    ) -> NeedHypothesis | None: ...

    async def update(self, hypothesis: NeedHypothesis) -> None: ...

    async def find_active_by_account_and_category(
        self,
        tenant_id: TenantId,
        account_id: ProspectAccountId,
        category: str,
    ) -> NeedHypothesis | None:
        """查同一企业同一类别的活跃假设。

        用于避免重复创建——否则同一家公司会被不同批次的探索反复
        生成假设，然后被联系多次，直接推高投诉率。
        """
        ...

    async def get_for_update(
        self, tenant_id: TenantId, hypothesis_id: NeedHypothesisId
    ) -> NeedHypothesis | None:
        """SELECT ... FOR UPDATE 读假设（转换路径行锁，spec D7）。"""
        ...

    async def list_for_outreach(
        self,
        tenant_id: TenantId,
        countries: list[str] | None,
        limit: int,
    ) -> list[NeedHypothesis]:
        """列出可触达的假设。

        实现注意：抑制名单过滤和 Ownership Lock 过滤属于**跨域**判断，
        不在本 repository 里做（那会需要 import 别的域）。由
        ``service`` 层组合，或在 SQL 里 join 只读视图。
        """
        ...

    async def list_for_radar(
        self,
        tenant_id: TenantId,
        *,
        status: str | None,
        limit: int,
    ) -> list[NeedHypothesis]: ...


@runtime_checkable
class ValidatedNeedRepository(Protocol):
    async def add(self, need: ValidatedNeed) -> None: ...

    async def get(
        self, tenant_id: TenantId, need_id: ValidatedNeedId
    ) -> ValidatedNeed | None: ...

    async def get_for_update(
        self, tenant_id: TenantId, need_id: ValidatedNeedId
    ) -> ValidatedNeed | None:
        """SELECT ... FOR UPDATE 读已验证需求（转换路径行锁，spec D7）。"""
        ...

    async def update(self, need: ValidatedNeed) -> None: ...

    async def append_field_history(
        self,
        tenant_id: TenantId,
        need_id: ValidatedNeedId,
        field_name: str,
        old_value: str | None,
        new_value: str,
        source_message_id: str,
        changed_by: str | None,
    ) -> None:
        """记录字段变更历史。

        **不能只覆盖当前值。** 客户把数量从 5000 改成 3000 是重要的
        商业信息，覆盖掉就丢了。这张历史表也是复盘"需求怎么演变的"
        的唯一依据。
        """
        ...

    async def list_sourcing_ready(
        self, tenant_id: TenantId, limit: int
    ) -> list[ValidatedNeed]:
        """列出待寻源的需求。

        Phase 1 按 ``created_at`` 排序。Phase 2 改为按需求簇规模排序
        （挂载点见 ``ROADMAP.md``）。
        """
        ...

    async def list_by_account(
        self, tenant_id: TenantId, account_id: ProspectAccountId
    ) -> list[ValidatedNeed]:
        """某企业的全部已验证需求。员工打开客户页面时要看到这个。"""
        ...

    async def list_for_radar(
        self,
        tenant_id: TenantId,
        *,
        status: str | None,
        limit: int,
    ) -> list[ValidatedNeed]: ...


@runtime_checkable
class NeedClusterRepository(Protocol):
    async def add(self, cluster: NeedCluster) -> None: ...

    async def get(
        self, tenant_id: TenantId, cluster_id: NeedClusterId
    ) -> NeedCluster | None: ...

    async def update(self, cluster: NeedCluster) -> None: ...

    async def find_candidate_cluster(
        self, tenant_id: TenantId, category: str, keywords: list[str]
    ) -> NeedCluster | None:
        """按类别与关键词找可归入的簇。

        Phase 1 用精确类别匹配 + 关键词重叠，不用向量检索——
        样本少时简单规则更可预测、更好调试。
        """
        ...

    async def list_for_radar(
        self, tenant_id: TenantId, *, limit: int
    ) -> list[NeedCluster]: ...
