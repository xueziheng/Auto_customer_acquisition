"""Need Cluster 优先级事实的 demand 服务测试。"""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from shared.errors import ValidationError
from shared.schemas.identifiers import (
    NeedClusterId,
    TenantId,
    ValidatedNeedId,
)

NOW = datetime(2026, 9, 2, 9, 0, tzinfo=UTC)
TENANT = TenantId("tn_priority")
OTHER_TENANT = TenantId("tn_other")
NEED_ID = ValidatedNeedId("vnd_priority")
OTHER_NEED_ID = ValidatedNeedId("vnd_other")
THIRD_NEED_ID = ValidatedNeedId("vnd_third")
MISSING_NEED_ID = ValidatedNeedId("vnd_missing")
CLUSTER_ID = NeedClusterId("ncl_priority")


@dataclass(frozen=True)
class _Need:
    """仅实现公开读取所需属性的仓储返回值。"""

    need_id: ValidatedNeedId
    tenant_id: TenantId
    cluster_id: NeedClusterId | None
    completeness: int


@dataclass(frozen=True)
class _Cluster:
    """仅实现公开读取所需属性的仓储返回值。"""

    cluster_id: NeedClusterId
    tenant_id: TenantId
    member_need_ids: list[ValidatedNeedId]


def _need(
    need_id: ValidatedNeedId,
    *,
    tenant_id: TenantId = TENANT,
    cluster_id: NeedClusterId | None = None,
    completeness: int = 3,
) -> _Need:
    return _Need(
        need_id=need_id,
        tenant_id=tenant_id,
        cluster_id=cluster_id,
        completeness=completeness,
    )


@dataclass
class _Needs:
    rows: dict[ValidatedNeedId, _Need]
    get_calls: list[ValidatedNeedId]

    async def get(
        self, tenant_id: TenantId, need_id: ValidatedNeedId
    ) -> _Need | None:
        self.get_calls.append(need_id)
        return self.rows.get(need_id)


@dataclass
class _Clusters:
    rows: dict[NeedClusterId, _Cluster]
    get_calls: list[NeedClusterId]

    async def get(
        self, tenant_id: TenantId, cluster_id: NeedClusterId
    ) -> _Cluster | None:
        self.get_calls.append(cluster_id)
        return self.rows.get(cluster_id)


@dataclass
class _Uow:
    needs: _Needs
    clusters: _Clusters

    @asynccontextmanager
    async def context(self) -> AsyncIterator[_Uow]:
        yield self


def _service(uow: _Uow) -> object:
    implementation = importlib.import_module("domains.demand.service_impl").DemandServiceImpl
    return implementation(lambda tenant_id: uow.context(), now=lambda: NOW)


def _priority_facts_type() -> type[object]:
    schema = importlib.import_module("domains.demand.schemas")
    facts_type = getattr(schema, "NeedClusterPriorityFacts", None)
    assert facts_type is not None, "RED：NeedClusterPriorityFacts 尚未创建"
    return facts_type


async def test_cluster_priority_facts_expose_verified_cluster_membership() -> None:
    """删掉成员双向核验或错误计数时，已核验的累计事实必须失效。"""
    cluster = _Cluster(
        cluster_id=CLUSTER_ID,
        tenant_id=TENANT,
        member_need_ids=[NEED_ID, OTHER_NEED_ID, THIRD_NEED_ID],
    )
    uow = _Uow(
        needs=_Needs(
            {
                NEED_ID: _need(NEED_ID, cluster_id=CLUSTER_ID),
                OTHER_NEED_ID: _need(OTHER_NEED_ID, cluster_id=CLUSTER_ID),
                THIRD_NEED_ID: _need(THIRD_NEED_ID, cluster_id=CLUSTER_ID),
            },
            [],
        ),
        clusters=_Clusters({CLUSTER_ID: cluster}, []),
    )

    facts = await _service(uow).get_cluster_priority_facts(TENANT, NEED_ID)

    assert facts == _priority_facts_type()(
        need_id=str(NEED_ID),
        cluster_id=str(CLUSTER_ID),
        cluster_member_count=3,
        facts_observed_at=NOW,
    )


async def test_cluster_priority_facts_reject_foreign_cluster_before_member_io() -> None:
    """若仓储意外返回其他租户的簇，不能继续读取其成员。"""
    foreign_cluster = _Cluster(
        cluster_id=CLUSTER_ID,
        tenant_id=OTHER_TENANT,
        member_need_ids=[NEED_ID],
    )
    needs = _Needs({NEED_ID: _need(NEED_ID, cluster_id=CLUSTER_ID)}, [])
    uow = _Uow(needs=needs, clusters=_Clusters({CLUSTER_ID: foreign_cluster}, []))

    with pytest.raises(ValidationError, match="需求簇租户不一致"):
        await _service(uow).get_cluster_priority_facts(TENANT, NEED_ID)

    assert needs.get_calls == [NEED_ID]


async def test_cluster_priority_facts_reject_missing_cluster_member() -> None:
    """簇列出的任一 Need 缺失时，累计成员数不是可用事实。"""
    cluster = _Cluster(
        cluster_id=CLUSTER_ID,
        tenant_id=TENANT,
        member_need_ids=[NEED_ID, MISSING_NEED_ID],
    )
    uow = _Uow(
        needs=_Needs({NEED_ID: _need(NEED_ID, cluster_id=CLUSTER_ID)}, []),
        clusters=_Clusters({CLUSTER_ID: cluster}, []),
    )

    with pytest.raises(ValidationError, match="需求簇成员链不完整"):
        await _service(uow).get_cluster_priority_facts(TENANT, NEED_ID)


async def test_cluster_priority_facts_reject_need_not_listed_by_cluster() -> None:
    """Need 单向指向簇时不能把其他成员数误报给下游。"""
    cluster = _Cluster(
        cluster_id=CLUSTER_ID,
        tenant_id=TENANT,
        member_need_ids=[OTHER_NEED_ID],
    )
    uow = _Uow(
        needs=_Needs(
            {
                NEED_ID: _need(NEED_ID, cluster_id=CLUSTER_ID),
                OTHER_NEED_ID: _need(OTHER_NEED_ID, cluster_id=CLUSTER_ID),
            },
            [],
        ),
        clusters=_Clusters({CLUSTER_ID: cluster}, []),
    )

    with pytest.raises(ValidationError, match="需求簇成员链不完整"):
        await _service(uow).get_cluster_priority_facts(TENANT, NEED_ID)


async def test_cluster_priority_facts_return_single_member_for_unclustered_completeness_two_need() -> None:
    """完整度 2 的已验证需求仍提供簇成员事实，不能在此读取入口套寻源门槛。"""
    low_completeness_need = _need(NEED_ID, completeness=2)
    uow = _Uow(needs=_Needs({NEED_ID: low_completeness_need}, []), clusters=_Clusters({}, []))

    facts = await _service(uow).get_cluster_priority_facts(TENANT, NEED_ID)

    assert low_completeness_need.completeness == 2
    assert facts == _priority_facts_type()(
        need_id=str(NEED_ID),
        cluster_id=None,
        cluster_member_count=1,
        facts_observed_at=NOW,
    )
    assert uow.clusters.get_calls == []
