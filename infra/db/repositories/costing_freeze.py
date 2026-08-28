"""冻结专用tenant仓储；完整payload严格解码，不输出敏感事实。"""

import json
from typing import Literal

from pydantic import ValidationError as SchemaError
from sqlalchemy import select, text

from domains.costing.errors import CostFreezeError
from domains.costing.freeze_schemas import CostScopeConfirmationView, StoredCostScope
from domains.costing.quote_lock import require_scope_integrity
from infra.db.repositories.costing import _TenantBoundRepository
from infra.db.tables import CostScopeConfirmationRow
from shared.schemas.identifiers import TenantId


class CostingFreezeRepositoryImpl(_TenantBoundRepository):
    """只增事实，同session完成scope/成本锁定/operation效果。"""

    async def lock_key(
        self, tenant_id: TenantId, kind: Literal["scope", "creation"], key: str
    ) -> None:
        """tenant/kind/key均参与咨询锁身份，覆盖尚不存在的行。"""
        self._require_tenant(tenant_id, "quote_lock_key")
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {
                "key": json.dumps(
                    ["costing-quote-lock-v1", tenant_id, kind, key],
                    separators=(",", ":"),
                )
            },
        )

    def _scope(self, row: CostScopeConfirmationRow) -> StoredCostScope:
        """同时验证显式列、完整来源与业务hash，损坏不可当有效依据。"""
        try:
            view = CostScopeConfirmationView.model_validate_json(
                json.dumps(row.payload)
            )
            require_scope_integrity(view)
            fields = (
                "tenant_id",
                "confirmation_id",
                "cost_sheet_id",
                "opportunity_id",
                "need_id",
                "coverage_id",
                "content_hash",
                "sheet_hash",
                "need_facts_hash",
                "specification_hash",
                "terms_hash",
            )
            if any(getattr(row, name) != getattr(view, name) for name in fields) or (
                row.confirmed_by,
                row.confirmed_at,
            ) != (view.provenance.confirmed_by, view.provenance.confirmed_at):
                raise CostFreezeError("facts_corrupt")
            return StoredCostScope(
                view=view,
                idempotency_key=row.idempotency_key,
                request_hash=row.request_hash,
            )
        except (SchemaError, ValueError, TypeError):
            raise CostFreezeError("facts_corrupt") from None

    async def get_scope_by_key(
        self, tenant_id: TenantId, key: str
    ) -> StoredCostScope | None:
        """读取当前租户永久幂等映射；调用方负责先取key锁。"""
        self._require_tenant(tenant_id, "quote_scope_key_read")
        row = await self._session.scalar(
            select(CostScopeConfirmationRow).where(
                CostScopeConfirmationRow.tenant_id == tenant_id,
                CostScopeConfirmationRow.idempotency_key == key,
            )
        )
        return self._scope(row) if row else None

    async def get_scope(
        self, tenant_id: TenantId, confirmation_id: str
    ) -> CostScopeConfirmationView | None:
        """内部历史读取，不宣称当前仍适用。"""
        self._require_tenant(tenant_id, "quote_scope_read")
        row = await self._session.scalar(
            select(CostScopeConfirmationRow).where(
                CostScopeConfirmationRow.tenant_id == tenant_id,
                CostScopeConfirmationRow.confirmation_id == confirmation_id,
            )
        )
        return self._scope(row).view if row else None

    async def add_scope(self, tenant_id: TenantId, record: StoredCostScope) -> None:
        """完整确认与显式外键同事务保存，不更新或删除历史。"""
        self._require_tenant(tenant_id, "quote_scope_add")
        view = record.view
        if view.tenant_id != tenant_id:
            raise CostFreezeError("facts_corrupt")
        require_scope_integrity(view)
        fields = (
            "tenant_id",
            "confirmation_id",
            "cost_sheet_id",
            "opportunity_id",
            "need_id",
            "coverage_id",
            "content_hash",
            "sheet_hash",
            "need_facts_hash",
            "specification_hash",
            "terms_hash",
        )
        self._session.add(
            CostScopeConfirmationRow(
                **{name: getattr(view, name) for name in fields},
                idempotency_key=record.idempotency_key,
                request_hash=record.request_hash,
                confirmed_by=view.provenance.confirmed_by,
                confirmed_at=view.provenance.confirmed_at,
                payload=view.model_dump(mode="json"),
            )
        )
        await self._session.flush()
