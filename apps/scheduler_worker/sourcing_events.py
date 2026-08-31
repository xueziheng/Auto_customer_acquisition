"""需求就绪事实到 Sourcing Case V2 Run 的安全触发接线。"""

from __future__ import annotations

import unicodedata

from domains.sourcing.service import (
    OpenSourcingCase,
    SourcingActor,
    SourcingNeedSnapshot,
    SourcingService,
)
from shared.errors import ValidationError, detached_dependency_error
from shared.events.catalog import NeedBecameSourcingReady, NeedValidated
from shared.schemas.identifiers import TenantId, ValidatedNeedId
from workflows.engine.runner import WorkflowEngine
from workflows.sourcing_case.flow import WORKFLOW_TYPE
from workflows.sourcing_case.ports import SourcingNeedReader


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


class SourcingTriggerHandler:
    """仅消费两种需求就绪事实；重复/乱序投递共享同一业务键。"""

    def __init__(
        self,
        *,
        engine: WorkflowEngine,
        sourcing: SourcingService,
        need_reader: SourcingNeedReader,
        tenant_id: TenantId,
        sourcing_actor: SourcingActor,
    ) -> None:
        self._engine = engine
        self._sourcing = sourcing
        self._need_reader = need_reader
        self._tenant_id = tenant_id
        self._sourcing_actor = sourcing_actor

    async def handle(self, event: object) -> None:
        """核验事件与快照后先取得 canonical Case，再幂等启动 V2 Run。"""

        if not isinstance(event, (NeedValidated, NeedBecameSourcingReady)):
            raise ValidationError("未知寻源触发事件类型")
        if event.tenant_id != self._tenant_id:
            return
        if isinstance(event, NeedValidated) and event.completeness < 3:
            return
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
            _raise_dependency_error(
                open_error,
                transient_message="寻源案例开案暂不可用",
                permanent_message="寻源案例开案失败",
            )
        if case_id is None:
            raise ValidationError("寻源案例开案失败")
        start_error: Exception | None = None
        try:
            await self._engine.start(
                self._tenant_id,
                WORKFLOW_TYPE,
                str(case_id),
                _safe_context(snapshot, str(case_id)),
                trigger_key,
            )
        except Exception as error:  # noqa: BLE001 - Outbox 边界只接收常规依赖错误
            start_error = error
        if start_error is not None:
            _raise_dependency_error(
                start_error,
                transient_message="寻源工作流启动暂不可用",
                permanent_message="寻源工作流启动失败",
            )


__all__ = ("SourcingTriggerHandler",)
