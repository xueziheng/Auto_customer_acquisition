"""Need Cluster 优先级事实的 demand 服务测试。"""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from shared.errors import ValidationError
from shared.schemas.identifiers import (
    EmployeeId,
    MessageId,
    NeedClusterId,
    ProspectAccountId,
    TenantId,
    ValidatedNeedId,
)
from shared.schemas.provenance import FactualField, Provenance, SourceType

NOW = datetime(2026, 9, 2, 9, 0, tzinfo=UTC)
TENANT = TenantId("tn_priority")
OTHER_TENANT = TenantId("tn_other")
NEED_ID = ValidatedNeedId("vnd_priority")
OTHER_NEED_ID = ValidatedNeedId("vnd_other")
THIRD_NEED_ID = ValidatedNeedId("vnd_third")
MISSING_NEED_ID = ValidatedNeedId("vnd_missing")
CLUSTER_ID = NeedClusterId("ncl_priority")
ACTOR_ID = EmployeeId("emp_recurrence")
SOURCE_MESSAGE_ID = MessageId("msg_recurrence")


@dataclass(frozen=True)
class _Need:
    """仅实现公开读取所需属性的仓储返回值。"""

    need_id: ValidatedNeedId
    tenant_id: TenantId
    cluster_id: NeedClusterId | None
    completeness: int
    created_at: object


@dataclass(frozen=True)
class _Cluster:
    """仅实现公开读取所需属性的仓储返回值。"""

    cluster_id: NeedClusterId
    tenant_id: TenantId
    member_need_ids: list[ValidatedNeedId]
    created_at: object = NOW - timedelta(hours=2)
    updated_at: object = NOW


def _need(
    need_id: ValidatedNeedId,
    *,
    tenant_id: TenantId = TENANT,
    cluster_id: NeedClusterId | None = None,
    completeness: int = 3,
    created_at: object = NOW,
) -> _Need:
    return _Need(
        need_id=need_id,
        tenant_id=tenant_id,
        cluster_id=cluster_id,
        completeness=completeness,
        created_at=created_at,
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


def _service(uow: _Uow, *, now: object = NOW) -> object:
    implementation = importlib.import_module("domains.demand.service_impl").DemandServiceImpl
    return implementation(lambda tenant_id: uow.context(), now=lambda: now)


def _priority_facts_type() -> type[object]:
    schema = importlib.import_module("domains.demand.schemas")
    facts_type = getattr(schema, "NeedClusterPriorityFacts", None)
    assert facts_type is not None, "RED：NeedClusterPriorityFacts 尚未创建"
    return facts_type


def _validated_need(*, recurring_requirement: object = None) -> object:
    """用真实需求实体证明 recurrence 不进入任何就绪推导。"""
    models = importlib.import_module("domains.demand.models")
    provenance = Provenance(
        source_type=SourceType.CONVERSATION,
        source_id=str(SOURCE_MESSAGE_ID),
        extracted_by=str(ACTOR_ID),
        extracted_at=NOW,
        confirmed_by=ACTOR_ID,
        confirmed_at=NOW,
    )
    return models.ValidatedNeed(
        need_id=NEED_ID,
        tenant_id=TENANT,
        account_id=ProspectAccountId("acc_recurrence"),
        product_category=FactualField("hinges", provenance),
        source_message_id=SOURCE_MESSAGE_ID,
        created_at=NOW,
        status=models.NeedStatus.VALIDATED,
        application=FactualField("marine", provenance),
        recurring_requirement=recurring_requirement,
    )


@pytest.mark.parametrize("value", [True, False])
def test_recurring_requirement_accepts_real_boolean_without_changing_readiness(
    value: bool,
) -> None:
    """若 recurrence 被纳入完整度/寻源门槛，level-2 Need 会被错误推进。"""
    provenance = Provenance(
        source_type=SourceType.CONVERSATION,
        source_id=str(SOURCE_MESSAGE_ID),
        extracted_by=str(ACTOR_ID),
        extracted_at=NOW,
        confirmed_by=ACTOR_ID,
        confirmed_at=NOW,
    )
    missing = _validated_need()
    present = _validated_need(
        recurring_requirement=FactualField(value=value, provenance=provenance)
    )

    assert present.recurring_requirement is not None
    assert present.recurring_requirement.value is value
    assert missing.recurring_requirement is None
    assert present.completeness == missing.completeness == 2
    assert present.is_sourcing_ready() is missing.is_sourcing_ready() is False
    assert present.status is missing.status


@pytest.mark.parametrize("value", ["true", "false", "True", "False"])
def test_recurring_requirement_rejects_bool_like_strings(value: str) -> None:
    """删除严格 bool 校验会把客户原文字符串伪装成已确认布尔事实。"""
    provenance = Provenance(
        source_type=SourceType.CONVERSATION,
        source_id=str(SOURCE_MESSAGE_ID),
        extracted_by=str(ACTOR_ID),
        extracted_at=NOW,
        confirmed_by=ACTOR_ID,
        confirmed_at=NOW,
    )

    with pytest.raises(ValidationError, match="重复采购事实类型无效"):
        _validated_need(
            recurring_requirement=FactualField(value=value, provenance=provenance)
        )


def test_recurring_requirement_rejects_web_provenance() -> None:
    """公开网页只能形成需求信号，不能写成客户明确表达的重复采购事实。"""
    provenance = Provenance(
        source_type=SourceType.WEB_PAGE,
        source_id="a" * 64,
        extracted_by="research-model",
        extracted_at=NOW,
        source_url="https://example.test/catalog",
        page_hash="a" * 64,
    )

    with pytest.raises(ValidationError, match="重复采购事实来源无效"):
        _validated_need(
            recurring_requirement=FactualField(value=True, provenance=provenance)
        )


def test_recurring_requirement_reuses_factual_provenance_guards() -> None:
    """Agent inference 与空来源必须由共享事实/来源契约拦截。"""
    with pytest.raises(ValidationError, match="AGENT_INFERENCE"):
        FactualField(
            value=True,
            provenance=Provenance(
                source_type=SourceType.AGENT_INFERENCE,
                source_id="inf_1",
                extracted_by="model-v1",
                extracted_at=NOW,
            ),
        )
    with pytest.raises(ValidationError, match="source_id 不能为空"):
        Provenance(
            source_type=SourceType.CONVERSATION,
            source_id="",
            extracted_by=str(ACTOR_ID),
            extracted_at=NOW,
        )


async def test_cluster_priority_facts_reject_invalid_tenant_before_opening_uow() -> None:
    """无效 tenant 输入必须在构造 tenant-bound UoW 前失败，不能触达任一仓储。"""
    implementation = importlib.import_module("domains.demand.service_impl").DemandServiceImpl
    opened_tenants: list[TenantId] = []

    def unopened_uow(tenant_id: TenantId) -> object:
        opened_tenants.append(tenant_id)
        raise AssertionError("无效 tenant 不得打开 UoW 或触达仓储")

    service = implementation(unopened_uow, now=lambda: NOW)

    with pytest.raises(ValidationError, match="需求雷达查询无效"):
        await service.get_cluster_priority_facts(TenantId(" tenant"), NEED_ID)

    assert opened_tenants == []


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


async def test_cluster_priority_facts_are_stable_when_only_reader_clock_advances() -> None:
    """把 observed_at 重新绑定读时钟会使相同底层事实产生不同 hash。"""

    cluster = _Cluster(
        cluster_id=CLUSTER_ID,
        tenant_id=TENANT,
        member_need_ids=[NEED_ID, OTHER_NEED_ID],
        created_at=NOW - timedelta(hours=2),
        updated_at=NOW - timedelta(hours=1),
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

    first = await _service(uow, now=NOW).get_cluster_priority_facts(TENANT, NEED_ID)
    second = await _service(
        uow, now=NOW + timedelta(days=1)
    ).get_cluster_priority_facts(TENANT, NEED_ID)

    assert first == second == _priority_facts_type()(
        need_id=str(NEED_ID),
        cluster_id=str(CLUSTER_ID),
        cluster_member_count=2,
        facts_observed_at=NOW - timedelta(hours=1),
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


async def test_unclustered_priority_facts_use_stable_need_creation_time() -> None:
    created_at = NOW - timedelta(days=3)
    uow = _Uow(
        needs=_Needs({NEED_ID: _need(NEED_ID, created_at=created_at)}, []),
        clusters=_Clusters({}, []),
    )

    first = await _service(uow, now=NOW).get_cluster_priority_facts(TENANT, NEED_ID)
    second = await _service(
        uow, now=NOW + timedelta(days=7)
    ).get_cluster_priority_facts(TENANT, NEED_ID)

    assert first == second
    assert first.facts_observed_at == created_at


@pytest.mark.parametrize(
    "cluster",
    [
        _Cluster(CLUSTER_ID, TENANT, [NEED_ID], updated_at=None),
        _Cluster(CLUSTER_ID, TENANT, [NEED_ID], updated_at=NOW.replace(tzinfo=None)),
        _Cluster(
            CLUSTER_ID,
            TENANT,
            [NEED_ID],
            created_at=NOW,
            updated_at=NOW - timedelta(seconds=1),
        ),
    ],
)
async def test_cluster_priority_facts_reject_invalid_stable_version_time(
    cluster: _Cluster,
) -> None:
    uow = _Uow(
        needs=_Needs({NEED_ID: _need(NEED_ID, cluster_id=CLUSTER_ID)}, []),
        clusters=_Clusters({CLUSTER_ID: cluster}, []),
    )

    with pytest.raises(ValidationError, match="^需求簇事实版本时间无效$"):
        await _service(uow).get_cluster_priority_facts(TENANT, NEED_ID)


@pytest.mark.parametrize("created_at", [None, NOW.replace(tzinfo=None)])
async def test_unclustered_priority_facts_reject_invalid_creation_time(
    created_at: object,
) -> None:
    uow = _Uow(
        needs=_Needs({NEED_ID: _need(NEED_ID, created_at=created_at)}, []),
        clusters=_Clusters({}, []),
    )

    with pytest.raises(ValidationError, match="^已验证需求事实版本时间无效$"):
        await _service(uow).get_cluster_priority_facts(TENANT, NEED_ID)
