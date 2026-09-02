"""需求就绪事实到 Sourcing Case V2 Run 的安全触发接线。"""

from __future__ import annotations

import unicodedata
from datetime import datetime
from typing import Protocol

from domains.demand.schemas import NeedClusterPriorityFacts
from domains.sourcing.service import (
    OpenSourcingCase,
    SourcingActor,
    SourcingAdmissionEnqueueCommand,
    SourcingNeedSnapshot,
    SourcingPriorityFactsInput,
    SourcingService,
)
from shared.errors import ValidationError, detached_dependency_error
from shared.events.catalog import (
    NeedBecameSourcingReady,
    NeedClusterMembershipChanged,
    NeedValidated,
)
from shared.schemas.identifiers import NeedClusterId, TenantId, ValidatedNeedId
from workflows.sourcing_case.ports import SourcingNeedReader


class DemandPriorityFactsReader(Protocol):
    """scheduler 所需的 Demand 公共只读窄接口。"""

    async def get_cluster_priority_facts(
        self, tenant_id: TenantId, need_id: ValidatedNeedId
    ) -> NeedClusterPriorityFacts: ...


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _safe_context(snapshot: SourcingNeedSnapshot, case_id: str) -> dict[str, object]:
    if not isinstance(snapshot.product_category.value, str):
        raise ValidationError("可信寻源需求品类必须是文本")
    category = _normalize(snapshot.product_category.value)
    if not category:
        raise ValidationError("可信寻源需求品类不能为空")
    keywords = sorted(
        {
            normalized
            for fact in (snapshot.application, snapshot.material, snapshot.size_spec)
            if fact is not None and isinstance(fact.value, str)
            if (normalized := _normalize(fact.value))
        }
    )
    return {
        "case_id": case_id,
        "need_id": str(snapshot.need_id),
        "need_snapshot_hash": snapshot.snapshot_hash,
        "product_category": category,
        "keywords": keywords,
    }


def _raise_dependency_error(
    error: Exception,
    *,
    transient_message: str,
    permanent_message: str,
) -> None:
    """在 ``except`` 外按可重试性抛固定错误，彻底丢弃原异常链与 context。"""

    raise detached_dependency_error(
        error,
        transient_message=transient_message,
        permanent_message=permanent_message,
    ) from None


def _valid_identifier(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and len(value) <= 200
        and all(ord(character) >= 32 and ord(character) != 127 for character in value)
    )


def _is_utc_datetime(value: object) -> bool:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return False
    offset = value.utcoffset()
    return offset is not None and offset.total_seconds() == 0


def _priority_input(
    raw: object,
    *,
    expected_need_id: ValidatedNeedId,
) -> SourcingPriorityFactsInput | None:
    """只把 Demand 公共 DTO 的完整结构转换成 sourcing 强类型事实。"""

    if (
        not isinstance(raw, NeedClusterPriorityFacts)
        or not _valid_identifier(raw.need_id)
        or raw.need_id != expected_need_id
        or (raw.cluster_id is not None and not _valid_identifier(raw.cluster_id))
        or type(raw.cluster_member_count) is not int
        or raw.cluster_member_count < 1
        or not _is_utc_datetime(raw.facts_observed_at)
    ):
        return None
    try:
        return SourcingPriorityFactsInput(
            need_id=expected_need_id,
            cluster_id=(
                NeedClusterId(raw.cluster_id) if raw.cluster_id is not None else None
            ),
            cluster_member_count=raw.cluster_member_count,
            facts_observed_at=raw.facts_observed_at,
        )
    except Exception:  # noqa: BLE001 - 结构非法只映射固定业务状态
        return None


async def _read_priority_input(
    demand: DemandPriorityFactsReader,
    tenant_id: TenantId,
    need_id: ValidatedNeedId,
) -> SourcingPriorityFactsInput | None:
    error: Exception | None = None
    raw: object | None = None
    try:
        raw = await demand.get_cluster_priority_facts(tenant_id, need_id)
    except Exception as caught:  # noqa: BLE001 - Outbox 边界按既有重试语义分类
        error = caught
    if error is not None:
        classified = detached_dependency_error(
            error,
            transient_message="需求簇优先级事实暂不可用",
            permanent_message="需求簇优先级事实无效",
        )
        if classified.is_retryable:
            raise classified from None
        return None
    return _priority_input(raw, expected_need_id=need_id)


def _dependency_is_retryable(
    error: Exception,
    *,
    transient_message: str,
    permanent_message: str,
) -> None:
    """永久领域拒绝安全确认；暂态依赖故障以固定错误交回 Outbox。"""

    classified = detached_dependency_error(
        error,
        transient_message=transient_message,
        permanent_message=permanent_message,
    )
    if classified.is_retryable:
        raise classified from None


class SourcingTriggerHandler:
    """仅消费两种需求就绪事实；重复/乱序投递共享同一业务键。"""

    def __init__(
        self,
        *,
        sourcing: SourcingService,
        demand: DemandPriorityFactsReader,
        need_reader: SourcingNeedReader,
        tenant_id: TenantId,
        sourcing_actor: SourcingActor,
    ) -> None:
        self._sourcing = sourcing
        self._demand = demand
        self._need_reader = need_reader
        self._tenant_id = tenant_id
        self._sourcing_actor = sourcing_actor

    async def handle(self, event: object) -> None:
        """核验事件与快照后取得 canonical Case，并仅幂等写入 durable admission。"""

        if not isinstance(event, (NeedValidated, NeedBecameSourcingReady)):
            raise ValidationError("未知寻源触发事件类型")
        if event.tenant_id != self._tenant_id:
            return
        if isinstance(event, NeedValidated) and event.completeness < 3:
            return
        if not _valid_identifier(event.need_id):
            raise ValidationError("可信寻源触发 Need 标识无效")
        need_id = ValidatedNeedId(event.need_id)
        snapshot: SourcingNeedSnapshot | None = None
        snapshot_error: Exception | None = None
        try:
            snapshot = await self._need_reader.read(self._tenant_id, need_id)
        except Exception as error:  # noqa: BLE001 - Outbox 边界只接收常规依赖错误
            snapshot_error = error
        if snapshot_error is not None:
            _raise_dependency_error(
                snapshot_error,
                transient_message="可信寻源需求快照暂不可用",
                permanent_message="可信寻源需求快照读取失败",
            )
        if (
            not isinstance(snapshot, SourcingNeedSnapshot)
            or snapshot.need_id != need_id
        ):
            raise ValidationError("可信需求快照与触发事件不匹配")
        trigger_key = f"sourcing-case:v2:{self._tenant_id}:{need_id}"
        case_id = None
        open_error: Exception | None = None
        try:
            case_id = await self._sourcing.open_case(
                self._tenant_id,
                OpenSourcingCase(need=snapshot, trigger_key=trigger_key),
                actor=self._sourcing_actor,
            )
        except Exception as error:  # noqa: BLE001 - Outbox 边界只接收常规依赖错误
            open_error = error
        if open_error is not None:
            _dependency_is_retryable(
                open_error,
                transient_message="寻源案例开案暂不可用",
                permanent_message="寻源案例开案失败",
            )
        if case_id is None:
            return
        facts = await _read_priority_input(self._demand, self._tenant_id, need_id)
        command = (
            SourcingAdmissionEnqueueCommand(facts=facts)
            if facts is not None
            else SourcingAdmissionEnqueueCommand(
                facts=None,
                blocked_reason="priority_facts_invalid",
            )
        )
        enqueue_error: Exception | None = None
        try:
            await self._sourcing.enqueue_admission(
                self._tenant_id,
                case_id,
                need_id,
                ready_at=event.occurred_at,
                command=command,
                actor=self._sourcing_actor,
            )
        except Exception as error:  # noqa: BLE001 - Outbox 边界只接收常规依赖错误
            enqueue_error = error
        if enqueue_error is not None:
            _dependency_is_retryable(
                enqueue_error,
                transient_message="寻源准入入队暂不可用",
                permanent_message="寻源准入入队失败",
            )


class SourcingClusterMembershipHandler:
    """以 Demand 当前事实刷新完整需求簇的可等待准入。"""

    def __init__(
        self,
        *,
        demand: DemandPriorityFactsReader,
        sourcing: SourcingService,
        tenant_id: TenantId,
        sourcing_actor: SourcingActor,
    ) -> None:
        self._demand = demand
        self._sourcing = sourcing
        self._tenant_id = tenant_id
        self._sourcing_actor = sourcing_actor

    async def handle(self, event: object) -> None:
        if not isinstance(event, NeedClusterMembershipChanged):
            raise ValidationError("未知需求簇成员变更事件类型")
        if (
            not _valid_identifier(event.tenant_id)
            or not _valid_identifier(event.cluster_id)
            or not _valid_identifier(event.changed_need_id)
            or type(event.member_count) is not int
            or event.member_count < 1
        ):
            raise ValidationError("需求簇成员变更事件载荷无效")
        if event.tenant_id != self._tenant_id:
            return
        need_id = ValidatedNeedId(str(event.changed_need_id))
        facts = await _read_priority_input(self._demand, self._tenant_id, need_id)
        if facts is None or facts.cluster_id != event.cluster_id:
            return
        refresh_error: Exception | None = None
        try:
            await self._sourcing.refresh_cluster_admissions(
                self._tenant_id,
                need_id,
                facts=facts,
                refreshed_at=facts.facts_observed_at,
                actor=self._sourcing_actor,
            )
        except Exception as error:  # noqa: BLE001 - Outbox 边界只接收常规依赖错误
            refresh_error = error
        if refresh_error is not None:
            _dependency_is_retryable(
                refresh_error,
                transient_message="寻源准入需求簇刷新暂不可用",
                permanent_message="寻源准入需求簇刷新失败",
            )


__all__ = (
    "DemandPriorityFactsReader",
    "SourcingClusterMembershipHandler",
    "SourcingTriggerHandler",
)
