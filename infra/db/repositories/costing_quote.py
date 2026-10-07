"""四类成本报价确认的 tenant-bound PostgreSQL 只增仓储。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import TypeAdapter
from sqlalchemy import case, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from domains.costing.errors import InvalidPricingEvidenceError
from domains.costing.quote_repository import EvidenceRecord
from domains.costing.quote_service import provenance_fields, view_content_hash
from domains.costing.schemas import (
    CostCoverageView,
    ExpenseEvidenceView,
    PriceEvidenceView,
    PricingPolicyView,
    QuoteFxView,
    SupplierPriceEvidenceView,
)
from infra.db.repositories.costing import _TenantBoundRepository
from infra.db.tables import (
    CostingCoverageRow,
    CostingPolicyRow,
    CostingPriceEvidenceRow,
    CostingQuoteFxRow,
    OpportunityRow,
    RawArtifactRow,
)
from shared.errors import IdempotencyConflict
from shared.schemas.identifiers import CostSheetId, OpportunityId, TenantId


class CostingOpportunityReferenceReaderImpl(_TenantBoundRepository):
    """不投影客户事实、不加锁；只提供同租户机会存在的时点事实。"""

    async def exists(self, tenant_id: TenantId, opportunity_id: OpportunityId) -> bool:
        self._require_tenant(tenant_id, "costing_opportunity_reference")
        statement = select(select(OpportunityRow.opportunity_id).where(
            OpportunityRow.tenant_id == self._tenant_id,
            OpportunityRow.opportunity_id == opportunity_id,
        ).exists())
        return (await self._session.execute(statement)).scalar_one()


type _ConfirmedEvidence = (
    PricingPolicyView | SupplierPriceEvidenceView | ExpenseEvidenceView
    | CostCoverageView | QuoteFxView
)


class _EvidenceRepository[T: _ConfirmedEvidence](_TenantBoundRepository):
    """SQL 只负责隔离和持久化；金额及适用性由域服务判定。"""

    row_type: Any
    value_type: Any
    id_field: str
    hash_field: str = "content_hash"

    def __init__(self, session: AsyncSession, tenant_id: TenantId) -> None:
        super().__init__(session, tenant_id)
        self._adapter = TypeAdapter(self.value_type)

    def _validate(self, record: EvidenceRecord[T]) -> None:
        """持久路径不接受缺来源的计算 fixture 或不一致的逐字段确认。"""
        value = record.value
        if getattr(value, self.id_field) != record.record_id or getattr(
            value, self.hash_field
        ) != view_content_hash(value):
            raise InvalidPricingEvidenceError("确认内容身份不一致")
        payload = value.model_dump(
            mode="python",
            exclude={
                self.id_field,
                self.hash_field,
                "source",
                "field_provenance",
                "confirmed_by",
                "confirmed_at",
            },
        )
        fields = set(provenance_fields(payload))
        if set(value.field_provenance) != fields:
            raise InvalidPricingEvidenceError("缺少逐字段确认来源")
        source = getattr(value, "source", None)
        if not isinstance(value, CostCoverageView) and (
            source is None
            or source.tenant_id != record.tenant_id
            or source.source_ref != value.source_ref
        ):
            raise InvalidPricingEvidenceError("缺少当前租户的已确认来源")
        for provenance in value.field_provenance.values():
            if (
                provenance.confirmed_by != value.confirmed_by
                or provenance.confirmed_at != value.confirmed_at
                or provenance.extracted_by != "human"
                or (
                    source is not None
                    and (
                        provenance.source_id != source.source_ref
                        or provenance.source_type.value != source.source_type
                        or (
                            source.source_type == "web_page"
                            and (
                                provenance.source_url != source.source_url
                                or provenance.page_hash != source.content_hash
                            )
                        )
                    )
                )
            ):
                raise InvalidPricingEvidenceError("逐字段确认人与来源不一致")

    async def add(self, record: EvidenceRecord[T]) -> None:
        """只追加，并再次核对来源 metadata 与不可变原始资料哈希。"""
        self._require_tenant(record.tenant_id, "costing_evidence_add")
        self._validate(record)
        value = record.value
        values = {
            "tenant_id": str(record.tenant_id),
            self.id_field: record.record_id,
            "idempotency_key": record.idempotency_key,
            "request_hash": record.request_hash,
            self.hash_field: getattr(value, self.hash_field),
            "payload": value.model_dump(mode="json"),
            "confirmed_by": value.confirmed_by,
            "confirmed_at": value.confirmed_at,
        }
        source = getattr(value, "source", None)
        if source is not None:
            artifact = (
                await self._session.execute(
                    select(RawArtifactRow).where(
                        RawArtifactRow.tenant_id == str(self._tenant_id),
                        RawArtifactRow.artifact_id == source.artifact_id,
                    )
                )
            ).scalar_one_or_none()
            if artifact is None or artifact.content_hash != source.content_hash:
                raise InvalidPricingEvidenceError(
                    "来源原始资料不存在或内容 hash 不一致"
                )
            values["artifact_id"] = source.artifact_id
        if isinstance(value, PricingPolicyView):
            values.update(category=value.category, effective_from=value.effective_from)
        elif isinstance(value, CostCoverageView):
            values.update(
                cost_sheet_id=value.cost_sheet_id, sheet_hash=value.expected_sheet_hash
            )
        elif hasattr(value, "opportunity_id"):
            values["opportunity_id"] = value.opportunity_id
        self._session.add(self.row_type(**values))
        try:
            await self._session.flush()
        except IntegrityError as exc:
            if getattr(exc.orig, "sqlstate", None) == "23505":
                raise IdempotencyConflict(
                    "确认内容或幂等键已存在，请复用原确认操作"
                ) from None
            raise

    def _record(self, row: Any) -> EvidenceRecord[T]:
        """从持久 JSON 恢复 typed DTO，并拒绝历史缺失来源或错配确认。"""
        value = self._adapter.validate_json(json.dumps(row.payload))
        record = EvidenceRecord(
            TenantId(row.tenant_id),
            getattr(row, self.id_field),
            row.idempotency_key,
            row.request_hash,
            value,
        )
        self._validate(record)
        if (
            row.confirmed_by != value.confirmed_by
            or row.confirmed_at != value.confirmed_at
            or getattr(row, self.hash_field) != getattr(value, self.hash_field)
        ):
            raise InvalidPricingEvidenceError("持久确认列与内容不一致")
        if hasattr(row, "artifact_id") and row.artifact_id != value.source.artifact_id:
            raise InvalidPricingEvidenceError("持久来源关联不一致")
        if isinstance(value, CostCoverageView) and (
            row.cost_sheet_id,
            row.sheet_hash,
        ) != (value.cost_sheet_id, value.expected_sheet_hash):
            raise InvalidPricingEvidenceError("持久清单与成本内容关联不一致")
        if isinstance(value, PricingPolicyView) and (
            row.category,
            row.effective_from,
        ) != (value.category, value.effective_from):
            raise InvalidPricingEvidenceError("持久政策生效范围不一致")
        if (
            hasattr(row, "opportunity_id")
            and row.opportunity_id != value.opportunity_id
        ):
            raise InvalidPricingEvidenceError("持久依据机会关联不一致")
        return record

    async def _get(
        self, tenant_id: TenantId, record_id: str, *, lock: bool
    ) -> EvidenceRecord[T] | None:
        """所有读取入口统一限定绑定租户，锁不会扩大可见范围。"""
        self._require_tenant(tenant_id, "costing_evidence_get")
        statement = select(self.row_type).where(
            self.row_type.tenant_id == str(self._tenant_id),
            getattr(self.row_type, self.id_field) == record_id,
        )
        if lock:
            statement = statement.with_for_update()
        row = (await self._session.execute(statement)).scalar_one_or_none()
        return None if row is None else self._record(row)

    async def get(
        self, tenant_id: TenantId, record_id: str
    ) -> EvidenceRecord[T] | None:
        """读取同租户不可变确认。"""
        return await self._get(tenant_id, record_id, lock=False)

    async def get_for_update(
        self, tenant_id: TenantId, record_id: str
    ) -> EvidenceRecord[T] | None:
        """在当前事务锁定同租户确认。"""
        return await self._get(tenant_id, record_id, lock=True)

    async def get_by_key_for_update(
        self, tenant_id: TenantId, key: str
    ) -> EvidenceRecord[T] | None:
        """数据库事务咨询锁保护尚未存在的幂等键，多连接并发也只提交一次。"""
        self._require_tenant(tenant_id, "costing_evidence_key")
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {
                "key": f"costing-evidence:{self._tenant_id}:{self.row_type.__tablename__}:{key}",
            },
        )
        row = (
            await self._session.execute(
                select(self.row_type)
                .where(
                    self.row_type.tenant_id == str(self._tenant_id),
                    self.row_type.idempotency_key == key,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        return None if row is None else self._record(row)


class PricingPolicyRepositoryImpl(_EvidenceRepository[PricingPolicyView]):
    """只读有确认来源的新政策，品类优先且严格按生效时间选择。"""

    row_type, value_type, id_field = CostingPolicyRow, PricingPolicyView, "policy_id"

    async def lock_selection(self, tenant_id: TenantId, *, exclusive: bool) -> None:
        """同tenant政策集合锁，防止freeze选政策时并发插入改变当前选择。"""
        self._require_tenant(tenant_id, "costing_policy_selection_lock")
        function = (
            "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
        )
        await self._session.execute(
            text(f"SELECT {function}(hashtextextended(:key,0))"),
            {
                "key": json.dumps(
                    ["costing-current-policy-v1", tenant_id], separators=(",", ":")
                )
            },
        )

    async def get_effective(
        self, tenant_id: TenantId, category: str | None, at: datetime
    ) -> EvidenceRecord[PricingPolicyView] | None:
        """新政策不提前生效，历史记录不覆写，同时间按确认时间稳定排序。"""
        self._require_tenant(tenant_id, "costing_policy_effective")
        row = (
            await self._session.execute(
                select(CostingPolicyRow)
                .where(
                    CostingPolicyRow.tenant_id == str(self._tenant_id),
                    CostingPolicyRow.effective_from <= at,
                    CostingPolicyRow.confirmed_at <= at,
                    or_(
                        CostingPolicyRow.category == category,
                        CostingPolicyRow.category.is_(None),
                    ),
                )
                .order_by(
                    case((CostingPolicyRow.category == category, 0), else_=1),
                    CostingPolicyRow.effective_from.desc(),
                    CostingPolicyRow.confirmed_at.desc(),
                    CostingPolicyRow.policy_id.desc(),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        return None if row is None else self._record(row)


class PriceEvidenceRepositoryImpl(_EvidenceRepository[PriceEvidenceView]):
    """供应商价格与费用共表，判别联合保留不同业务字段。"""

    row_type, value_type, id_field = (
        CostingPriceEvidenceRow,
        PriceEvidenceView,
        "evidence_id",
    )
    hash_field = "evidence_hash"

    async def list_by_opportunity(self, tenant_id: TenantId, opportunity_id: OpportunityId) -> tuple[EvidenceRecord[PriceEvidenceView], ...]:
        self._require_tenant(tenant_id, "costing_price_list")
        rows = (await self._session.scalars(select(CostingPriceEvidenceRow).where(
            CostingPriceEvidenceRow.tenant_id == self._tenant_id,
            CostingPriceEvidenceRow.opportunity_id == opportunity_id,
        ).order_by(CostingPriceEvidenceRow.confirmed_at, CostingPriceEvidenceRow.evidence_id))).all()
        return tuple(self._record(row) for row in rows)


class CostCoverageRepositoryImpl(_EvidenceRepository[CostCoverageView]):
    """完整性清单以内容 hash 为可恢复身份。"""

    row_type, value_type, id_field = CostingCoverageRow, CostCoverageView, "coverage_id"

    async def get_latest(self, tenant_id: TenantId, cost_sheet_id: CostSheetId) -> EvidenceRecord[CostCoverageView] | None:
        """读取历史最近确认，不为刷新补写清单或重确认适用性。"""
        self._require_tenant(tenant_id, "costing_coverage_latest")
        row = await self._session.scalar(select(CostingCoverageRow).where(
            CostingCoverageRow.tenant_id == tenant_id, CostingCoverageRow.cost_sheet_id == cost_sheet_id)
            .order_by(CostingCoverageRow.confirmed_at.desc(), CostingCoverageRow.coverage_id.desc()).limit(1))
        return self._record(row) if row else None

    async def get_for_sheet_hash(
        self, tenant_id: TenantId, cost_sheet_id: CostSheetId, sheet_hash: str
    ) -> EvidenceRecord[CostCoverageView] | None:
        """SQL精确tenant/sheet/hash后稳定选取，不应用层猜选历史清单。"""
        self._require_tenant(tenant_id, "costing_coverage_current")
        row = await self._session.scalar(
            select(CostingCoverageRow)
            .where(
                CostingCoverageRow.tenant_id == tenant_id,
                CostingCoverageRow.cost_sheet_id == cost_sheet_id,
                CostingCoverageRow.sheet_hash == sheet_hash,
            )
            .order_by(
                CostingCoverageRow.confirmed_at.desc(),
                CostingCoverageRow.coverage_id.desc(),
            )
            .limit(1)
        )
        return self._record(row) if row else None


class QuoteFxRepositoryImpl(_EvidenceRepository[QuoteFxView]):
    """核算到报价的独立汇率仓储。"""

    row_type, value_type, id_field = CostingQuoteFxRow, QuoteFxView, "fx_id"
