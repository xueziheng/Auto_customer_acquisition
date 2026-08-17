"""需求域服务实现（浅域：2026-08-17 计划 Task 3/4 只实现 capture/discard）。

捕获语义（规格 §5/§7）：输入校验全部在开 UoW 前完成；所有 str 输入
item == item.strip()；WEB_PAGE 强约束（url+hash 非空且 source_id ==
page_hash）；去重 key 全非空 5 列；重复返回既有 ID 不重复发事件；
业务插入 + outbox 同事务。事件只用共享契约 DemandSignalCaptured
（metadata-only，不含 raw_observation/possible_need/provenance）。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from domains.demand.errors import MissingWebEvidenceError
from domains.demand.models import DemandSignal, SignalStatus, SignalType
from domains.demand.repository import DemandUnitOfWork
from domains.demand.schemas import SignalCaptureRequest
from shared.errors import InvalidStateTransition, ValidationError
from shared.events.catalog import DemandSignalCaptured
from shared.schemas.identifiers import DemandSignalId, TenantId, new_id
from shared.schemas.provenance import Provenance, SourceType


class DemandServiceImpl:
    """``DemandService`` 的 Postgres 实现（浅域子集）。"""

    def __init__(
        self,
        uow_factory: Callable[[TenantId], DemandUnitOfWork],
        *,
        now: Callable[[], datetime],
    ) -> None:
        if not callable(uow_factory) or not callable(now):
            raise ValidationError("需求服务依赖无效")
        self._uow_factory = uow_factory
        self._now = now

    @staticmethod
    def _validate_now(value: datetime) -> datetime:
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() != UTC.utcoffset(value)
        ):
            raise ValidationError("服务时钟必须为 UTC")
        return value

    @staticmethod
    def _require_text(
        value: object,
        label: str,
        *,
        max_len: int | None = None,
        can_be_none: bool = False,
    ) -> str | None:
        if can_be_none and value is None:
            return None
        if (
            not isinstance(value, str)
            or not value.strip()
            or value != value.strip()
        ):
            raise ValidationError(f"{label}无效")
        if max_len is not None and len(value) > max_len:
            raise ValidationError(f"{label}超长")
        return value

    async def capture_signal(
        self, tenant_id: TenantId, request: SignalCaptureRequest
    ) -> str:
        """记录一条需求信号（契约见 service.py docstring + 规格 §5/§7）。"""
        if (
            not isinstance(tenant_id, str)
            or not tenant_id.strip()
            or tenant_id != tenant_id.strip()
        ):
            raise ValidationError("信号租户无效")
        if len(tenant_id) > 32:
            raise ValidationError("信号租户超长")
        entity_name = self._require_text(request.entity_name, "信号企业名", max_len=200)
        raw_observation = self._require_text(request.raw_observation, "信号观察内容")
        source_id = self._require_text(request.source_id, "信号来源", max_len=200)
        extracted_by = self._require_text(request.extracted_by, "信号提取者", max_len=64)
        # 必填字段（can_be_none=False 已保证非 None）：显式收窄以通过 mypy
        assert entity_name is not None and raw_observation is not None
        assert source_id is not None and extracted_by is not None
        possible_need = self._require_text(
            request.possible_need, "信号可能需求", can_be_none=True
        )
        source_url = self._require_text(
            request.source_url, "信号来源 URL", max_len=2000, can_be_none=True
        )
        page_hash = self._require_text(
            request.page_hash, "信号页面哈希", max_len=200, can_be_none=True
        )
        try:
            signal_type = SignalType(request.signal_type)
        except ValueError:
            raise ValidationError("信号类型无效")
        try:
            source_type = SourceType(request.source_type)
        except ValueError:
            raise ValidationError("信号来源类型无效")
        observed_at = request.observed_at
        if (
            not isinstance(observed_at, datetime)
            or observed_at.tzinfo is None
            or observed_at.utcoffset() != UTC.utcoffset(observed_at)
        ):
            raise ValidationError("信号观察时间必须为 UTC")
        if source_type is SourceType.WEB_PAGE and (
            not source_url or not page_hash or source_id != page_hash
        ):
            raise MissingWebEvidenceError(
                "网页来源信号缺少 URL/page_hash 或 source_id 与 page_hash 不一致"
            )
        now = self._validate_now(self._now())
        signal = DemandSignal(
            signal_id=DemandSignalId(new_id("sig")),
            tenant_id=tenant_id,
            signal_type=signal_type,
            entity_name=entity_name,
            raw_observation=raw_observation,
            observed_at=observed_at,
            status=SignalStatus.CAPTURED,
            possible_need=possible_need,
            provenance=Provenance(
                source_type=source_type,
                source_id=source_id,
                extracted_by=extracted_by,
                extracted_at=now,
                confirmed_by=None,
                confirmed_at=None,
                source_url=source_url,
                page_hash=page_hash,
            ),
        )
        async with self._uow_factory(tenant_id) as uow:
            inserted = await uow.signals.add(signal)
            if inserted:
                await uow.bus.publish(
                    DemandSignalCaptured(
                        tenant_id=tenant_id,
                        occurred_at=now,
                        run_id=None,
                        signal_id=signal.signal_id,
                        entity_name=signal.entity_name,
                        signal_type=signal.signal_type.value,
                        source_url=signal.provenance.source_url,
                    )
                )
                return str(signal.signal_id)
            winner = await uow.signals.find_duplicate(
                tenant_id,
                entity_name,
                signal_type.value,
                source_type.value,
                source_id,
            )
            if winner is None:
                raise ValidationError("信号写入竞态异常")
            return str(winner.signal_id)

    async def discard_signal(
        self, tenant_id: TenantId, signal_id: str, reason: str
    ) -> None:
        """丢弃信号（契约见 service.py docstring + 规格 §8）。

        service 只用 repo 返回的转换前快照判定：None=不存在/跨租户不可见；
        LINKED=拒绝；DISCARDED 同 reason=幂等 no-op、不同 reason=冲突；
        CAPTURED=本次完成转换（DB 已更新）。
        """
        if (
            not isinstance(tenant_id, str)
            or not tenant_id.strip()
            or tenant_id != tenant_id.strip()
        ):
            raise ValidationError("信号租户无效")
        if len(tenant_id) > 32:
            raise ValidationError("信号租户超长")
        if (
            not isinstance(signal_id, str)
            or not signal_id.strip()
            or signal_id != signal_id.strip()
        ):
            raise ValidationError("信号标识无效")
        if len(signal_id) > 32:
            raise ValidationError("信号标识超长")
        reason_text = self._require_text(reason, "丢弃原因")
        assert reason_text is not None  # can_be_none=False 已保证非 None：收窄以通过 mypy
        async with self._uow_factory(tenant_id) as uow:
            snapshot = await uow.signals.discard(
                tenant_id, DemandSignalId(signal_id), reason_text
            )
            if snapshot is None:
                raise ValidationError("需求信号不存在")
            if snapshot.status is SignalStatus.LINKED_TO_HYPOTHESIS:
                raise InvalidStateTransition("已关联假设的信号不可丢弃")
            if snapshot.status is SignalStatus.DISCARDED:
                if snapshot.discard_reason == reason_text:
                    return
                raise InvalidStateTransition("丢弃原因冲突，拒绝覆盖")
