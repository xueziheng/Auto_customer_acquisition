"""demand 域假设侧仓储实现（NeedHypothesis / ValidatedNeed）。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import select, text
from sqlalchemy import update as sa_update
from sqlalchemy.dialects.postgresql import Insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from domains.demand.models import (
    HypothesisStatus,
    NeedHypothesis,
    NeedStatus,
    ValidatedNeed,
)
from domains.demand.repository import (
    NeedHypothesisRepository,
    ValidatedNeedRepository,
)
from infra.db.tables import (
    NeedHypothesisRow,
    ValidatedNeedFieldHistoryRow,
    ValidatedNeedRow,
)
from shared.errors import TenantIsolationViolation, ValidationError
from shared.schemas.evidence import EvidenceItem, EvidenceLevel
from shared.schemas.identifiers import (
    ConversationId,
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

_tenant_logger = logging.getLogger("infra.db.repositories.need_hypotheses")


class _HypothesisRepository:
    def __init__(
        self,
        session: AsyncSession,
        tenant_id: TenantId,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._now = now if now is not None else lambda: datetime.now(UTC)

    def _tenant_matches(self, tenant_id: TenantId, action: str) -> bool:
        if tenant_id == self._tenant_id:
            return True
        _tenant_logger.critical(
            "检测到跨租户数据隔离违规",
            extra={"action": action, "tenant_id": str(self._tenant_id)},
        )
        return False

    def _require_tenant(self, tenant_id: TenantId, action: str) -> None:
        if not self._tenant_matches(tenant_id, action):
            raise TenantIsolationViolation("跨租户数据隔离违规")


def _value_to_json(value: object) -> object:
    """用确定性表示序列化业务字段值。"""
    if isinstance(value, Money):
        return {"amount": str(value.amount), "currency": value.currency}
    if isinstance(value, date):
        return value.isoformat()
    return value


def _provenance_to_json(provenance: Provenance) -> dict[str, object]:
    return {
        "source_type": provenance.source_type.value,
        "source_id": provenance.source_id,
        "extracted_by": provenance.extracted_by,
        "extracted_at": provenance.extracted_at.isoformat(),
        "confirmed_by": provenance.confirmed_by,
        "confirmed_at": (
            provenance.confirmed_at.isoformat() if provenance.confirmed_at else None
        ),
        "source_url": provenance.source_url,
        "page_hash": provenance.page_hash,
    }


def _factual_to_json(field: FactualField[Any] | None) -> dict[str, object] | None:
    if field is None:
        return None
    return {
        "value": _value_to_json(field.value),
        "provenance": _provenance_to_json(field.provenance),
    }


def _decode_value(value: object, kind: str) -> object:
    if kind == "quantity":
        return int(cast(int | str, value))
    if kind == "required_by":
        return date.fromisoformat(str(value))
    if kind == "target_price":
        money = cast(dict[str, object], value)
        return Money(
            amount=Decimal(str(money["amount"])),
            currency=CurrencyCode(str(money["currency"])),
        )
    return value


def _json_to_factual(
    raw: dict[str, object] | None, value_kind: str
) -> FactualField[Any] | None:
    if raw is None:
        return None
    provenance_raw = cast(dict[str, object], raw["provenance"])
    provenance = Provenance(
        source_type=SourceType(str(provenance_raw["source_type"])),
        source_id=str(provenance_raw["source_id"]),
        extracted_by=str(provenance_raw["extracted_by"]),
        extracted_at=datetime.fromisoformat(str(provenance_raw["extracted_at"])),
        confirmed_by=(
            EmployeeId(str(provenance_raw["confirmed_by"]))
            if provenance_raw.get("confirmed_by")
            else None
        ),
        confirmed_at=(
            datetime.fromisoformat(str(provenance_raw["confirmed_at"]))
            if provenance_raw.get("confirmed_at")
            else None
        ),
        source_url=(
            str(provenance_raw["source_url"])
            if provenance_raw.get("source_url") is not None
            else None
        ),
        page_hash=(
            str(provenance_raw["page_hash"])
            if provenance_raw.get("page_hash") is not None
            else None
        ),
    )
    return FactualField(
        value=_decode_value(raw["value"], value_kind),
        provenance=provenance,
    )


def _evidence_to_json(item: EvidenceItem) -> dict[str, object]:
    return {
        "level": item.level.value,
        "source_type": item.source_type,
        "source_id": item.source_id,
        "observed_at": item.observed_at.isoformat(),
        "summary": item.summary,
    }


def _json_to_evidence(raw: dict[str, object]) -> EvidenceItem:
    return EvidenceItem(
        level=EvidenceLevel(str(raw["level"])),
        source_type=str(raw["source_type"]),
        source_id=str(raw["source_id"]),
        observed_at=datetime.fromisoformat(str(raw["observed_at"])),
        summary=str(raw["summary"]),
    )


def _inferred_to_json(field: InferredField[str]) -> dict[str, object]:
    return {
        "value": field.value,
        "based_on": [_evidence_to_json(item) for item in field.based_on],
        "inferred_by": field.inferred_by,
        "inferred_at": field.inferred_at.isoformat(),
    }


def _json_to_inferred(raw: dict[str, object]) -> InferredField[str]:
    return InferredField(
        value=str(raw["value"]),
        based_on=[
            _json_to_evidence(item)
            for item in cast(list[dict[str, object]], raw["based_on"])
        ],
        inferred_by=str(raw["inferred_by"]),
        inferred_at=datetime.fromisoformat(str(raw["inferred_at"])),
    )


def _row_to_hypothesis(row: NeedHypothesisRow) -> NeedHypothesis:
    return NeedHypothesis(
        hypothesis_id=NeedHypothesisId(row.hypothesis_id),
        tenant_id=TenantId(row.tenant_id),
        account_id=ProspectAccountId(row.account_id),
        category=row.category,
        reasoning=_json_to_inferred(cast(dict[str, object], row.reasoning)),
        signal_ids=[DemandSignalId(item) for item in cast(list[str], row.signal_ids)],
        created_at=row.created_at,
        status=HypothesisStatus(row.status),
        rejection_reason=row.rejection_reason,
        validated_need_id=(
            ValidatedNeedId(row.validated_need_id)
            if row.validated_need_id is not None
            else None
        ),
    )


def _build_hypothesis_insert(hypothesis: NeedHypothesis) -> Insert:
    """构建与 0022 部分唯一索引谓词严格一致的幂等插入。"""
    return (
        pg_insert(NeedHypothesisRow)
        .values(
            tenant_id=str(hypothesis.tenant_id),
            hypothesis_id=str(hypothesis.hypothesis_id),
            account_id=str(hypothesis.account_id),
            category=hypothesis.category,
            reasoning=_inferred_to_json(hypothesis.reasoning),
            signal_ids=[str(item) for item in hypothesis.signal_ids],
            status=hypothesis.status.value,
            rejection_reason=hypothesis.rejection_reason,
            validated_need_id=(
                str(hypothesis.validated_need_id)
                if hypothesis.validated_need_id is not None
                else None
            ),
            created_at=hypothesis.created_at,
        )
        .on_conflict_do_nothing(
            index_elements=["tenant_id", "account_id", "category"],
            index_where=text("status IN ('inferred','contacting')"),
        )
    )


class NeedHypothesisRepositoryImpl(_HypothesisRepository, NeedHypothesisRepository):
    async def add(self, hypothesis: NeedHypothesis) -> bool:
        self._require_tenant(hypothesis.tenant_id, "need_hypothesis_add")
        result = await self._session.execute(
            _build_hypothesis_insert(hypothesis)
        )
        return cast(CursorResult[Any], result).rowcount > 0

    async def get(
        self, tenant_id: TenantId, hypothesis_id: NeedHypothesisId
    ) -> NeedHypothesis | None:
        self._require_tenant(tenant_id, "need_hypothesis_get")
        row = (
            await self._session.execute(
                select(NeedHypothesisRow).where(
                    NeedHypothesisRow.tenant_id == str(self._tenant_id),
                    NeedHypothesisRow.hypothesis_id == str(hypothesis_id),
                )
            )
        ).scalar_one_or_none()
        return _row_to_hypothesis(row) if row is not None else None

    async def update(self, hypothesis: NeedHypothesis) -> None:
        self._require_tenant(hypothesis.tenant_id, "need_hypothesis_update")
        result = await self._session.execute(
            sa_update(NeedHypothesisRow)
            .where(
                NeedHypothesisRow.tenant_id == str(self._tenant_id),
                NeedHypothesisRow.hypothesis_id == str(hypothesis.hypothesis_id),
            )
            .values(
                status=hypothesis.status.value,
                rejection_reason=hypothesis.rejection_reason,
                validated_need_id=(
                    str(hypothesis.validated_need_id)
                    if hypothesis.validated_need_id is not None
                    else None
                ),
                reasoning=_inferred_to_json(hypothesis.reasoning),
                signal_ids=[str(item) for item in hypothesis.signal_ids],
            )
        )
        if cast(CursorResult[Any], result).rowcount != 1:
            raise ValidationError("需求假设更新失败")

    async def get_for_update(
        self, tenant_id: TenantId, hypothesis_id: NeedHypothesisId
    ) -> NeedHypothesis | None:
        self._require_tenant(tenant_id, "need_hypothesis_get_for_update")
        row = (
            await self._session.execute(
                select(NeedHypothesisRow)
                .where(
                    NeedHypothesisRow.tenant_id == str(self._tenant_id),
                    NeedHypothesisRow.hypothesis_id == str(hypothesis_id),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        return _row_to_hypothesis(row) if row is not None else None

    async def find_active_by_account_and_category(
        self,
        tenant_id: TenantId,
        account_id: ProspectAccountId,
        category: str,
    ) -> NeedHypothesis | None:
        self._require_tenant(
            tenant_id, "need_hypothesis_find_active_by_account_and_category"
        )
        row = (
            await self._session.execute(
                select(NeedHypothesisRow).where(
                    NeedHypothesisRow.tenant_id == str(self._tenant_id),
                    NeedHypothesisRow.account_id == str(account_id),
                    NeedHypothesisRow.category == category,
                    NeedHypothesisRow.status.in_(("inferred", "contacting")),
                )
            )
        ).scalar_one_or_none()
        return _row_to_hypothesis(row) if row is not None else None

    async def list_for_outreach(
        self, tenant_id: TenantId, countries: list[str] | None, limit: int
    ) -> list[NeedHypothesis]:
        self._require_tenant(tenant_id, "need_hypothesis_list_for_outreach")
        del countries  # 国家筛选依赖跨域 account 视图，不在本仓储切片内。
        rows = (
            await self._session.execute(
                select(NeedHypothesisRow)
                .where(
                    NeedHypothesisRow.tenant_id == str(self._tenant_id),
                    NeedHypothesisRow.status.in_(("inferred", "contacting")),
                )
                .order_by(NeedHypothesisRow.created_at, NeedHypothesisRow.hypothesis_id)
                .limit(limit)
            )
        ).scalars().all()
        return [_row_to_hypothesis(row) for row in rows]


_FIELD_KINDS: dict[str, str] = {
    "product_category": "str",
    "application": "str",
    "material": "str",
    "size_spec": "str",
    "quantity": "quantity",
    "packaging": "str",
    "destination": "str",
    "required_by": "required_by",
    "target_price": "target_price",
    "current_supply_issue": "str",
    "certification_required": "str",
}


def _need_to_row(need: ValidatedNeed) -> ValidatedNeedRow:
    values: dict[str, object] = {
        "tenant_id": str(need.tenant_id),
        "need_id": str(need.need_id),
        "account_id": str(need.account_id),
        "source_message_id": str(need.source_message_id),
        "source_conversation_id": (
            str(need.source_conversation_id)
            if need.source_conversation_id is not None
            else None
        ),
        "status": need.status.value,
        "created_at": need.created_at,
        "confirmed_by": str(need.confirmed_by) if need.confirmed_by else None,
    }
    for field_name in _FIELD_KINDS:
        values[field_name] = _factual_to_json(getattr(need, field_name))
    return ValidatedNeedRow(**values)


def _row_to_need(row: ValidatedNeedRow) -> ValidatedNeed:
    def field(name: str) -> FactualField[Any] | None:
        try:
            return _json_to_factual(
                cast(dict[str, object] | None, getattr(row, name)),
                _FIELD_KINDS[name],
            )
        except (TypeError, KeyError, ValueError) as exc:
            raise ValidationError("字段快照损坏") from exc

    product_category = field("product_category")
    if product_category is None:
        raise ValidationError("字段快照损坏")
    return ValidatedNeed(
        need_id=ValidatedNeedId(row.need_id),
        tenant_id=TenantId(row.tenant_id),
        account_id=ProspectAccountId(row.account_id),
        product_category=cast(FactualField[str], product_category),
        source_message_id=MessageId(row.source_message_id),
        source_conversation_id=(
            ConversationId(row.source_conversation_id)
            if row.source_conversation_id is not None
            else None
        ),
        created_at=row.created_at,
        status=NeedStatus(row.status),
        application=cast(FactualField[str] | None, field("application")),
        material=cast(FactualField[str] | None, field("material")),
        size_spec=cast(FactualField[str] | None, field("size_spec")),
        quantity=cast(FactualField[int] | None, field("quantity")),
        packaging=cast(FactualField[str] | None, field("packaging")),
        destination=cast(FactualField[str] | None, field("destination")),
        required_by=cast(FactualField[date] | None, field("required_by")),
        target_price=cast(FactualField[Money] | None, field("target_price")),
        current_supply_issue=cast(
            FactualField[str] | None, field("current_supply_issue")
        ),
        certification_required=cast(
            FactualField[str] | None, field("certification_required")
        ),
        confirmed_by=EmployeeId(row.confirmed_by) if row.confirmed_by else None,
        cluster_id=None,
    )


class ValidatedNeedRepositoryImpl(_HypothesisRepository, ValidatedNeedRepository):
    async def add(self, need: ValidatedNeed) -> None:
        self._require_tenant(need.tenant_id, "validated_need_add")
        self._session.add(_need_to_row(need))

    async def get(
        self, tenant_id: TenantId, need_id: ValidatedNeedId
    ) -> ValidatedNeed | None:
        self._require_tenant(tenant_id, "validated_need_get")
        row = (
            await self._session.execute(
                select(ValidatedNeedRow).where(
                    ValidatedNeedRow.tenant_id == str(self._tenant_id),
                    ValidatedNeedRow.need_id == str(need_id),
                )
            )
        ).scalar_one_or_none()
        return _row_to_need(row) if row is not None else None

    async def get_for_update(
        self, tenant_id: TenantId, need_id: ValidatedNeedId
    ) -> ValidatedNeed | None:
        self._require_tenant(tenant_id, "validated_need_get_for_update")
        row = (
            await self._session.execute(
                select(ValidatedNeedRow)
                .where(
                    ValidatedNeedRow.tenant_id == str(self._tenant_id),
                    ValidatedNeedRow.need_id == str(need_id),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        return _row_to_need(row) if row is not None else None

    async def update(self, need: ValidatedNeed) -> None:
        self._require_tenant(need.tenant_id, "validated_need_update")
        values: dict[str, object] = {
            "status": need.status.value,
            "confirmed_by": str(need.confirmed_by) if need.confirmed_by else None,
        }
        for field_name in _FIELD_KINDS:
            values[field_name] = _factual_to_json(getattr(need, field_name))
        result = await self._session.execute(
            sa_update(ValidatedNeedRow)
            .where(
                ValidatedNeedRow.tenant_id == str(self._tenant_id),
                ValidatedNeedRow.need_id == str(need.need_id),
            )
            .values(**values)
        )
        if cast(CursorResult[Any], result).rowcount != 1:
            raise ValidationError("已验证需求更新失败")

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
        self._require_tenant(tenant_id, "validated_need_append_field_history")
        # 同一 UoW 可先 add need 再追加历史；无 ORM relationship 时先落父行，
        # 避免 flush 阶段由对象加入顺序触发复合 FK 失败。
        await self._session.flush()
        self._session.add(
            ValidatedNeedFieldHistoryRow(
                tenant_id=str(self._tenant_id),
                history_id=new_id("vh"),
                need_id=str(need_id),
                field_name=field_name,
                old_value=old_value,
                new_value=new_value,
                source_message_id=source_message_id,
                changed_by=changed_by,
                changed_at=self._now(),
            )
        )

    async def list_sourcing_ready(
        self, tenant_id: TenantId, limit: int
    ) -> list[ValidatedNeed]:
        self._require_tenant(tenant_id, "validated_need_list_sourcing_ready")
        rows = (
            await self._session.execute(
                select(ValidatedNeedRow)
                .where(
                    ValidatedNeedRow.tenant_id == str(self._tenant_id),
                    ValidatedNeedRow.status == NeedStatus.SOURCING_READY.value,
                )
                .order_by(ValidatedNeedRow.created_at, ValidatedNeedRow.need_id)
                .limit(limit)
            )
        ).scalars().all()
        return [_row_to_need(row) for row in rows]

    async def list_by_account(
        self, tenant_id: TenantId, account_id: ProspectAccountId
    ) -> list[ValidatedNeed]:
        self._require_tenant(tenant_id, "validated_need_list_by_account")
        rows = (
            await self._session.execute(
                select(ValidatedNeedRow)
                .where(
                    ValidatedNeedRow.tenant_id == str(self._tenant_id),
                    ValidatedNeedRow.account_id == str(account_id),
                )
                .order_by(ValidatedNeedRow.created_at, ValidatedNeedRow.need_id)
            )
        ).scalars().all()
        return [_row_to_need(row) for row in rows]
