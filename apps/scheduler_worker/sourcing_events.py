"""需求就绪事实到 Sourcing Case V2 Run 的安全触发接线。"""

from __future__ import annotations

import unicodedata

from domains.sourcing.service import (
    OpenSourcingCase,
    SourcingActor,
    SourcingNeedSnapshot,
    SourcingService,
)
from shared.errors import TransientError, ValidationError
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
    *, transient: bool, failed: bool, transient_message: str, permanent_message: str
) -> None:
    """在 ``except`` 外按可重试性抛固定错误，彻底丢弃原异常链与 context。"""

    if transient:
        raise TransientError(transient_message)
    if failed:
        raise ValidationError(permanent_message)


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
        read_failed = False
        read_transient = False
        snapshot: SourcingNeedSnapshot | None = None
        try:
            snapshot = await self._need_reader.read(self._tenant_id, need_id)
        except TransientError:
            read_transient = True
        except Exception:  # noqa: BLE001 - 丢弃下层自由异常，避免 outbox 日志泄漏
            read_failed = True
        _raise_dependency_error(
            transient=read_transient,
            failed=read_failed,
            transient_message="可信寻源需求快照暂不可用",
            permanent_message="可信寻源需求快照读取失败",
        )
        if (
            not isinstance(snapshot, SourcingNeedSnapshot)
            or snapshot.need_id != need_id
        ):
            raise ValidationError("可信需求快照与触发事件不匹配")
        trigger_key = f"sourcing-case:v2:{self._tenant_id}:{need_id}"
        open_failed = False
        open_transient = False
        case_id = None
        try:
            case_id = await self._sourcing.open_case(
                self._tenant_id,
                OpenSourcingCase(need=snapshot, trigger_key=trigger_key),
                actor=self._sourcing_actor,
            )
        except TransientError:
            open_transient = True
        except Exception:  # noqa: BLE001 - 丢弃数据库异常原文，仅暴露固定分类
            open_failed = True
        _raise_dependency_error(
            transient=open_transient,
            failed=open_failed,
            transient_message="寻源案例开案暂不可用",
            permanent_message="寻源案例开案失败",
        )
        if open_failed or case_id is None:
            raise ValidationError("寻源案例开案失败")
        start_failed = False
        start_transient = False
        try:
            await self._engine.start(
                self._tenant_id,
                WORKFLOW_TYPE,
                str(case_id),
                _safe_context(snapshot, str(case_id)),
                trigger_key,
            )
        except TransientError:
            start_transient = True
        except Exception:  # noqa: BLE001 - 引擎异常可能含连接信息，必须清洗
            start_failed = True
        _raise_dependency_error(
            transient=start_transient,
            failed=start_failed,
            transient_message="寻源工作流启动暂不可用",
            permanent_message="寻源工作流启动失败",
        )


__all__ = ("SourcingTriggerHandler",)
