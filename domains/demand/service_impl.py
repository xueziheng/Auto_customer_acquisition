"""需求域服务实现（需求信号与需求假设生命周期）。

捕获语义（规格 §5/§7）：输入校验全部在开 UoW 前完成；所有 str 输入
item == item.strip()；WEB_PAGE 强约束（url+hash 非空且 source_id ==
page_hash）；去重 key 全非空 5 列；重复返回既有 ID 不重复发事件；
业务插入 + outbox 同事务。事件只用共享契约 DemandSignalCaptured
（metadata-only，不含 raw_observation/possible_need/provenance）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import cast

from domains.demand.errors import (
    HypothesisAlreadyResolvedError,
    InsufficientEvidenceError,
    MissingWebEvidenceError,
    SourcingThresholdNotMetError,
)
from domains.demand.models import (
    DemandSignal,
    HypothesisStatus,
    NeedHypothesis,
    NeedStatus,
    SignalStatus,
    SignalType,
    ValidatedNeed,
)
from domains.demand.repository import DemandUnitOfWork
from domains.demand.schemas import (
    HypothesisDiscoveryEvidenceView,
    HypothesisDiscoveryView,
    SignalCaptureRequest,
)
from shared.errors import InvalidStateTransition, ValidationError
from shared.events.catalog import (
    DemandSignalCaptured,
    NeedHypothesisCreated,
    NeedHypothesisRejected,
    NeedValidated,
)
from shared.schemas.evidence import (
    ConfidenceResult,
    EvidenceItem,
    EvidenceLevel,
    derive_confidence,
)
from shared.schemas.identifiers import (
    DemandSignalId,
    EmployeeId,
    MessageId,
    NeedHypothesisId,
    ProspectAccountId,
    TenantId,
    ValidatedNeedId,
    new_id,
)
from shared.schemas.money import CurrencyCode, Money
from shared.schemas.provenance import (
    FactualField,
    InferredField,
    Provenance,
    SourceType,
)

_EVIDENCE_RANK = {level: index for index, level in enumerate(EvidenceLevel)}
_PROMOTABLE_SOURCE_TYPES = frozenset(
    {
        SourceType.CONVERSATION.value,
        SourceType.UPLOAD.value,
        SourceType.EMPLOYEE_INPUT.value,
    }
)
_PROMOTE_FIELD_WHITELIST = frozenset(
    {
        "product_category",
        "application",
        "material",
        "size_spec",
        "quantity",
        "packaging",
        "destination",
        "required_by",
        "target_price",
        "current_supply_issue",
        "certification_required",
    }
)
_TEXT_PROMOTE_FIELDS = _PROMOTE_FIELD_WHITELIST - {
    "quantity",
    "required_by",
    "target_price",
}
_UPDATE_FIELD_WHITELIST = _PROMOTE_FIELD_WHITELIST - {"product_category"}


def _merge_evidence(
    existing: list[EvidenceItem], incoming: list[EvidenceItem]
) -> list[EvidenceItem]:
    """以来源身份做幂等并集，保持首次出现顺序。"""
    merged: list[EvidenceItem] = []
    position: dict[tuple[str, str], int] = {}
    for item in existing + incoming:
        key = (item.source_type, item.source_id)
        index = position.get(key)
        if index is None:
            position[key] = len(merged)
            merged.append(item)
        elif _EVIDENCE_RANK[item.level] > _EVIDENCE_RANK[merged[index].level]:
            merged[index] = item
    return merged


def _merge_ids(
    existing: list[DemandSignalId], incoming: list[DemandSignalId]
) -> list[DemandSignalId]:
    """信号 ID 幂等并集，保持首次出现顺序。"""
    merged: list[DemandSignalId] = []
    seen: set[str] = set()
    for signal_id in existing + incoming:
        if str(signal_id) not in seen:
            seen.add(str(signal_id))
            merged.append(signal_id)
    return merged


def _coerce_field_value(name: str, value: object) -> object:
    """把外部字段转成域模型要求的确定性类型。"""
    if name == "quantity":
        if isinstance(value, bool):
            raise ValidationError("需求字段类型无效")
        try:
            return int(value)  # type: ignore[call-overload]
        except (TypeError, ValueError) as exc:
            raise ValidationError("需求字段类型无效") from exc
    if name == "required_by":
        try:
            return date.fromisoformat(str(value))
        except ValueError as exc:
            raise ValidationError("需求字段类型无效") from exc
    if name == "target_price":
        if not isinstance(value, dict):
            raise ValidationError("需求字段类型无效")
        try:
            return Money(
                amount=Decimal(str(value["amount"])),
                currency=CurrencyCode(str(value["currency"])),
            )
        except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
            raise ValidationError("需求字段类型无效") from exc
    if name in _TEXT_PROMOTE_FIELDS and (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
    ):
        raise ValidationError("需求字段类型无效")
    return value


def _highest_evidence_level(hypothesis: NeedHypothesis) -> EvidenceLevel:
    """返回假设证据的最高等级，用于可解释的拒绝信息。"""
    return max(
        (item.level for item in hypothesis.evidence()),
        key=_EVIDENCE_RANK.__getitem__,
        default=EvidenceLevel.AGENT_INDUSTRY_INFERENCE,
    )


def _highest_promotable_evidence_level(
    hypothesis: NeedHypothesis,
) -> EvidenceLevel:
    """返回真正满足晋升门槛的最高证据等级。"""
    minimum = _EVIDENCE_RANK[EvidenceLevel.CUSTOMER_INTEREST_REPLY]
    return max(
        (
            item.level
            for item in hypothesis.evidence()
            if _EVIDENCE_RANK[item.level] >= minimum
            and item.source_type in _PROMOTABLE_SOURCE_TYPES
        ),
        key=_EVIDENCE_RANK.__getitem__,
        default=EvidenceLevel.AGENT_INDUSTRY_INFERENCE,
    )


def _factual_value_to_text(field: FactualField[object] | None) -> str | None:
    """将事实字段值确定性序列化为历史表文本。"""
    if field is None:
        return None
    value = field.value
    if isinstance(value, Money):
        return str(value.amount)
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


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

    async def create_hypothesis(
        self,
        tenant_id: TenantId,
        account_id: ProspectAccountId,
        category: str,
        signal_ids: list[str],
        reasoning: str,
        inferred_by: str,
    ) -> NeedHypothesisId:
        """用租户内信号快照创建或幂等并入活跃需求假设。"""
        if (
            not isinstance(tenant_id, str)
            or not tenant_id.strip()
            or tenant_id != tenant_id.strip()
        ):
            raise ValidationError("租户无效")
        if len(tenant_id) > 40:
            raise ValidationError("租户超长")
        if (
            not isinstance(account_id, str)
            or not account_id.strip()
            or account_id != account_id.strip()
        ):
            raise ValidationError("目标企业无效")
        if len(account_id) > 40:
            raise ValidationError("目标企业超长")
        category_text = self._require_text(category, "需求类别", max_len=200)
        assert category_text is not None
        if (
            not isinstance(signal_ids, list)
            or not signal_ids
            or any(
                not isinstance(signal_id, str)
                or not signal_id.strip()
                or signal_id != signal_id.strip()
                for signal_id in signal_ids
            )
        ):
            raise ValidationError("需求信号不能为空")
        if any(len(signal_id) > 40 for signal_id in signal_ids):
            raise ValidationError("需求信号标识无效")
        reasoning_text = self._require_text(reasoning, "推断理由")
        inferred_by_text = self._require_text(inferred_by, "推断者", max_len=64)
        assert reasoning_text is not None and inferred_by_text is not None
        now = self._validate_now(self._now())
        evidence: list[EvidenceItem] = []
        async with self._uow_factory(tenant_id) as uow:
            for signal_id in signal_ids:
                signal = await uow.signals.get(tenant_id, DemandSignalId(signal_id))
                if signal is None:
                    raise ValidationError("需求信号不存在")
                if signal.status is SignalStatus.DISCARDED:
                    raise ValidationError("需求信号已丢弃")
                evidence.append(
                    EvidenceItem(
                        level=signal.evidence_level,
                        source_type=signal.provenance.source_type.value,
                        source_id=signal.provenance.source_id,
                        observed_at=signal.observed_at,
                        summary=signal.raw_observation,
                    )
                )
            incoming_ids = _merge_ids(
                [], [DemandSignalId(signal_id) for signal_id in signal_ids]
            )
            evidence = _merge_evidence([], evidence)
            hypothesis = NeedHypothesis(
                hypothesis_id=NeedHypothesisId(new_id("hyp")),
                tenant_id=tenant_id,
                account_id=account_id,
                category=category_text,
                reasoning=InferredField(
                    value=reasoning_text,
                    based_on=evidence,
                    inferred_by=inferred_by_text,
                    inferred_at=now,
                ),
                signal_ids=incoming_ids,
                created_at=now,
            )
            inserted = await uow.hypotheses.add(hypothesis)
            if inserted:
                await uow.bus.publish(
                    NeedHypothesisCreated(
                        tenant_id=tenant_id,
                        occurred_at=now,
                        run_id=None,
                        hypothesis_id=hypothesis.hypothesis_id,
                        account_id=account_id,
                        category=category_text,
                        confidence_tier=derive_confidence(evidence, now=now).tier,
                    )
                )
                return hypothesis.hypothesis_id
            winner = await uow.hypotheses.find_active_by_account_and_category(
                tenant_id, account_id, category_text
            )
            if winner is None:
                raise ValidationError("需求假设写入竞态异常")
            active = await uow.hypotheses.get_for_update(
                tenant_id, winner.hypothesis_id
            )
            if active is None:
                raise ValidationError("需求假设写入竞态异常")
            merged = NeedHypothesis(
                hypothesis_id=active.hypothesis_id,
                tenant_id=active.tenant_id,
                account_id=active.account_id,
                category=active.category,
                reasoning=InferredField(
                    value=active.reasoning.value,
                    based_on=_merge_evidence(active.reasoning.based_on, evidence),
                    inferred_by=active.reasoning.inferred_by,
                    inferred_at=now,
                ),
                signal_ids=_merge_ids(active.signal_ids, incoming_ids),
                created_at=active.created_at,
                status=active.status,
                rejection_reason=active.rejection_reason,
                validated_need_id=active.validated_need_id,
            )
            await uow.hypotheses.update(merged)
            return merged.hypothesis_id

    async def promote_to_validated(
        self,
        tenant_id: TenantId,
        hypothesis_id: str,
        source_message_id: str,
        extracted_fields: dict[str, object],
        confirmed_by: str | None = None,
    ) -> ValidatedNeedId:
        """将具备客户直接证据的假设原子晋升为已验证需求。"""
        if (
            not isinstance(hypothesis_id, str)
            or not hypothesis_id.strip()
            or hypothesis_id != hypothesis_id.strip()
        ):
            raise ValidationError("需求假设标识无效")
        if len(hypothesis_id) > 40:
            raise ValidationError("需求假设标识超长")
        if (
            not isinstance(source_message_id, str)
            or not source_message_id.strip()
            or source_message_id != source_message_id.strip()
        ):
            raise ValidationError("来源消息无效")
        if len(source_message_id) > 40:
            raise ValidationError("来源消息超长")
        if not isinstance(extracted_fields, dict) or not extracted_fields:
            raise ValidationError("提取字段不能为空")
        if set(extracted_fields) - _PROMOTE_FIELD_WHITELIST:
            raise ValidationError("未知需求字段")
        if "product_category" not in extracted_fields:
            raise ValidationError("产品类别不能为空")
        if confirmed_by is not None and (
            not isinstance(confirmed_by, str)
            or not confirmed_by.strip()
            or confirmed_by != confirmed_by.strip()
        ):
            raise ValidationError("确认人无效")
        if confirmed_by is not None and len(confirmed_by) > 40:
            raise ValidationError("确认人无效")
        coerced = {
            name: _coerce_field_value(name, value)
            for name, value in extracted_fields.items()
        }
        now = self._validate_now(self._now())
        async with self._uow_factory(tenant_id) as uow:
            hypothesis = await uow.hypotheses.get_for_update(
                tenant_id, NeedHypothesisId(hypothesis_id)
            )
            if hypothesis is None:
                raise ValidationError("需求假设不存在")
            if hypothesis.status is HypothesisStatus.VALIDATED:
                if hypothesis.validated_need_id is None:
                    raise ValidationError("需求假设写入竞态异常")
                return hypothesis.validated_need_id
            if hypothesis.status is HypothesisStatus.REJECTED:
                raise HypothesisAlreadyResolvedError("已否决的假设不可晋升")
            if not hypothesis.can_promote_to_validated():
                highest = _highest_evidence_level(hypothesis)
                raise InsufficientEvidenceError(
                    "证据不足，不可晋升为已验证需求："
                    f"当前最高证据等级 {highest.value}，"
                    "要求 ≥ customer_interest_reply 且来源为会话/上传/员工录入"
                )

            confirmer = EmployeeId(confirmed_by) if confirmed_by else None
            fields: dict[str, FactualField[object]] = {}
            for name, value in coerced.items():
                fields[name] = FactualField(
                    value=value,
                    provenance=Provenance(
                        source_type=SourceType.CONVERSATION,
                        source_id=source_message_id,
                        extracted_by=confirmed_by or "human",
                        extracted_at=now,
                        confirmed_by=confirmer,
                        confirmed_at=now if confirmer else None,
                    ),
                )

            need = ValidatedNeed(
                need_id=ValidatedNeedId(new_id("need")),
                tenant_id=tenant_id,
                account_id=hypothesis.account_id,
                product_category=cast(
                    FactualField[str], fields["product_category"]
                ),
                source_message_id=MessageId(source_message_id),
                created_at=now,
                source_conversation_id=None,
                application=cast(FactualField[str] | None, fields.get("application")),
                material=cast(FactualField[str] | None, fields.get("material")),
                size_spec=cast(FactualField[str] | None, fields.get("size_spec")),
                quantity=cast(FactualField[int] | None, fields.get("quantity")),
                packaging=cast(FactualField[str] | None, fields.get("packaging")),
                destination=cast(
                    FactualField[str] | None, fields.get("destination")
                ),
                required_by=cast(
                    FactualField[date] | None, fields.get("required_by")
                ),
                target_price=cast(
                    FactualField[Money] | None, fields.get("target_price")
                ),
                current_supply_issue=cast(
                    FactualField[str] | None, fields.get("current_supply_issue")
                ),
                certification_required=cast(
                    FactualField[str] | None,
                    fields.get("certification_required"),
                ),
                confirmed_by=confirmer,
                cluster_id=None,
            )
            if need.completeness >= 3:
                need = replace(need, status=NeedStatus.SOURCING_READY)
            await uow.needs.add(need)
            await uow.hypotheses.update(
                replace(
                    hypothesis,
                    status=HypothesisStatus.VALIDATED,
                    validated_need_id=need.need_id,
                )
            )
            await uow.bus.publish(
                NeedValidated(
                    tenant_id=tenant_id,
                    occurred_at=now,
                    run_id=None,
                    need_id=need.need_id,
                    account_id=need.account_id,
                    category=hypothesis.category,
                    evidence_level=_highest_promotable_evidence_level(hypothesis),
                    completeness=need.completeness,
                )
            )
            return need.need_id

    async def reject_hypothesis(
        self,
        tenant_id: TenantId,
        hypothesis_id: str,
        loss_reason: str,
        rejected_by: str | None = None,
    ) -> None:
        """否决活跃假设，保留首次拒绝原因并发布一次领域事件。"""
        if (
            not isinstance(hypothesis_id, str)
            or not hypothesis_id.strip()
            or hypothesis_id != hypothesis_id.strip()
        ):
            raise ValidationError("需求假设标识无效")
        if len(hypothesis_id) > 40:
            raise ValidationError("需求假设标识超长")
        reason = self._require_text(loss_reason, "拒绝原因", max_len=200)
        assert reason is not None
        if rejected_by is not None and (
            not isinstance(rejected_by, str)
            or not rejected_by.strip()
            or rejected_by != rejected_by.strip()
            or len(rejected_by) > 40
        ):
            raise ValidationError("拒绝人无效")
        now = self._validate_now(self._now())
        async with self._uow_factory(tenant_id) as uow:
            hypothesis = await uow.hypotheses.get_for_update(
                tenant_id, NeedHypothesisId(hypothesis_id)
            )
            if hypothesis is None:
                raise ValidationError("需求假设不存在")
            if hypothesis.status is HypothesisStatus.VALIDATED:
                raise HypothesisAlreadyResolvedError("已验证需求不可否决")
            if hypothesis.status is HypothesisStatus.REJECTED:
                if hypothesis.rejection_reason == reason:
                    return
                raise InvalidStateTransition("拒绝原因冲突，拒绝覆盖")
            await uow.hypotheses.update(
                replace(
                    hypothesis,
                    status=HypothesisStatus.REJECTED,
                    rejection_reason=reason,
                )
            )
            await uow.bus.publish(
                NeedHypothesisRejected(
                    tenant_id=tenant_id,
                    occurred_at=now,
                    run_id=None,
                    hypothesis_id=hypothesis.hypothesis_id,
                    reason=reason,
                )
            )

    async def update_need_fields(
        self,
        tenant_id: TenantId,
        need_id: str,
        fields: dict[str, object],
        source_message_id: str,
        updated_by: str | None = None,
    ) -> None:
        """补全需求事实字段，并在同一事务中追加字段历史。"""
        if (
            not isinstance(need_id, str)
            or not need_id.strip()
            or need_id != need_id.strip()
        ):
            raise ValidationError("已验证需求标识无效")
        if len(need_id) > 40:
            raise ValidationError("已验证需求标识超长")
        if (
            not isinstance(source_message_id, str)
            or not source_message_id.strip()
            or source_message_id != source_message_id.strip()
        ):
            raise ValidationError("来源消息无效")
        if len(source_message_id) > 40:
            raise ValidationError("来源消息超长")
        if not isinstance(fields, dict) or not fields:
            raise ValidationError("更新字段不能为空")
        if set(fields) - _UPDATE_FIELD_WHITELIST:
            raise ValidationError("未知需求字段")
        if updated_by is not None and (
            not isinstance(updated_by, str)
            or not updated_by.strip()
            or updated_by != updated_by.strip()
            or len(updated_by) > 40
        ):
            raise ValidationError("更新人无效")
        coerced = {
            name: _coerce_field_value(name, value) for name, value in fields.items()
        }
        now = self._validate_now(self._now())
        async with self._uow_factory(tenant_id) as uow:
            need = await uow.needs.get_for_update(
                tenant_id, ValidatedNeedId(need_id)
            )
            if need is None:
                raise ValidationError("已验证需求不存在")
            if need.status in (
                NeedStatus.FULFILLED,
                NeedStatus.WITHDRAWN,
                NeedStatus.LOST,
            ):
                raise InvalidStateTransition("需求已终结，字段不可变更")

            updater = EmployeeId(updated_by) if updated_by else None
            updated = replace(need)
            for name, value in coerced.items():
                old_text = _factual_value_to_text(
                    cast(FactualField[object] | None, getattr(need, name))
                )
                new_field = FactualField(
                    value=value,
                    provenance=Provenance(
                        source_type=SourceType.CONVERSATION,
                        source_id=source_message_id,
                        extracted_by=updated_by or "human",
                        extracted_at=now,
                        confirmed_by=updater,
                        confirmed_at=now if updater else None,
                    ),
                )
                new_text = _factual_value_to_text(new_field)
                assert new_text is not None
                await uow.needs.append_field_history(
                    tenant_id,
                    need.need_id,
                    name,
                    old_text,
                    new_text,
                    source_message_id,
                    updated_by,
                )
                setattr(updated, name, new_field)
            if (
                updated.status is NeedStatus.VALIDATED
                and updated.completeness >= 3
            ):
                updated.status = NeedStatus.SOURCING_READY
            await uow.needs.update(updated)

    async def mark_sourcing_ready(
        self,
        tenant_id: TenantId,
        need_id: str,
    ) -> None:
        """在完整度达到三级后，将需求幂等标记为可寻源。"""
        if (
            not isinstance(need_id, str)
            or not need_id.strip()
            or need_id != need_id.strip()
        ):
            raise ValidationError("已验证需求标识无效")
        if len(need_id) > 40:
            raise ValidationError("已验证需求标识超长")
        async with self._uow_factory(tenant_id) as uow:
            need = await uow.needs.get_for_update(
                tenant_id, ValidatedNeedId(need_id)
            )
            if need is None:
                raise ValidationError("已验证需求不存在")
            if need.status in (
                NeedStatus.FULFILLED,
                NeedStatus.WITHDRAWN,
                NeedStatus.LOST,
            ):
                raise InvalidStateTransition("需求已终结，不可标记可寻源")
            if need.status in (
                NeedStatus.SOURCING_READY,
                NeedStatus.HANDED_TO_SOURCING,
            ):
                return
            if not need.is_sourcing_ready():
                raise SourcingThresholdNotMetError(
                    "完整度不足，不能进寻源：还缺 "
                    + "、".join(need.missing_fields_for_sourcing())
                )
            await uow.needs.update(
                replace(need, status=NeedStatus.SOURCING_READY)
            )

    async def get_confidence(
        self,
        tenant_id: TenantId,
        hypothesis_id: str,
    ) -> ConfidenceResult:
        """根据当前证据集现场推导置信档位，不读取或保存概率。"""
        if (
            not isinstance(hypothesis_id, str)
            or not hypothesis_id.strip()
            or hypothesis_id != hypothesis_id.strip()
        ):
            raise ValidationError("需求假设标识无效")
        if len(hypothesis_id) > 40:
            raise ValidationError("需求假设标识超长")
        async with self._uow_factory(tenant_id) as uow:
            hypothesis = await uow.hypotheses.get(
                tenant_id, NeedHypothesisId(hypothesis_id)
            )
            if hypothesis is None:
                raise ValidationError("需求假设不存在")
            return derive_confidence(
                hypothesis.evidence(), now=self._validate_now(self._now())
            )

    async def get_hypothesis_for_discovery(
        self,
        tenant_id: TenantId,
        hypothesis_id: NeedHypothesisId,
    ) -> HypothesisDiscoveryView:
        """返回活跃假设及其原始信号的最小、租户隔离投影。"""
        if (
            not isinstance(hypothesis_id, str)
            or not hypothesis_id
            or hypothesis_id != hypothesis_id.strip()
            or len(hypothesis_id) > 40
        ):
            raise ValidationError("需求假设标识无效")
        async with self._uow_factory(tenant_id) as uow:
            hypothesis = await uow.hypotheses.get(tenant_id, hypothesis_id)
            if hypothesis is None:
                raise ValidationError("需求假设不存在")
            if hypothesis.status not in (
                HypothesisStatus.INFERRED,
                HypothesisStatus.CONTACTING,
            ):
                raise InvalidStateTransition("需求假设当前不可用于账户发现")
            evidence: list[HypothesisDiscoveryEvidenceView] = []
            for signal_id in hypothesis.signal_ids:
                signal = await uow.signals.get(tenant_id, signal_id)
                if signal is None or signal.status is SignalStatus.DISCARDED:
                    raise ValidationError("需求假设证据链不完整")
                evidence.append(
                    HypothesisDiscoveryEvidenceView(
                        signal_id=str(signal.signal_id),
                        summary=signal.raw_observation,
                        source_url=signal.provenance.source_url,
                    )
                )
            if not evidence:
                raise ValidationError("需求假设证据链不完整")
            refs = tuple(item.signal_id for item in evidence)
            return HypothesisDiscoveryView(
                hypothesis_id=str(hypothesis.hypothesis_id),
                category=hypothesis.category,
                reasoning=hypothesis.reasoning.value,
                evidence=tuple(evidence),
                source_signal_refs=refs,
            )
