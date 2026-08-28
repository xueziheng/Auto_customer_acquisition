"""冻结专用tenant仓储；完整payload严格解码，不输出敏感事实。"""

import json
from datetime import datetime
from typing import Literal

from pydantic import ValidationError as SchemaError
from sqlalchemy import insert, select, text, update

from domains.costing.errors import CostFreezeError
from domains.costing.freeze_schemas import (
    CostScopeConfirmationView,
    FrozenCostBasis,
    StoredCostScope,
)
from domains.costing.quote_lock import (
    require_basis_integrity,
    require_completion,
    require_operation_integrity,
    require_scope_integrity,
)
from infra.db.repositories.costing import (
    CostSheetRepositoryImpl,
    _TenantBoundRepository,
)
from infra.db.tables import (
    CostingQuoteBasisRow,
    CostScopeConfirmationRow,
    QuoteCreationOperationRow,
)
from shared.errors import ValidationError as DomainValidationError
from shared.schemas.identifiers import CostSheetId, QuoteId, TenantId
from shared.schemas.quote_creation import (
    QuoteCreationCompletion,
    QuoteCreationOperationView,
)


class CostingFreezeRepositoryImpl(_TenantBoundRepository):
    """只增事实，同session完成scope/成本锁定/operation效果。"""

    async def list_scopes(self, tenant_id: TenantId, cost_sheet_id: CostSheetId) -> tuple[CostScopeConfirmationView, ...]:
        """持久列与payload逐条核验，不能把损坏历史当空列表。"""
        self._require_tenant(tenant_id, "quote_scope_list")
        rows = await self._session.scalars(select(CostScopeConfirmationRow).where(
            CostScopeConfirmationRow.tenant_id == tenant_id, CostScopeConfirmationRow.cost_sheet_id == cost_sheet_id)
            .order_by(CostScopeConfirmationRow.confirmed_at, CostScopeConfirmationRow.confirmation_id))
        return tuple(self._scope(row).view for row in rows)

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
        except (SchemaError, ValueError, TypeError, DomainValidationError):
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

    def _operation(self, row: QuoteCreationOperationRow) -> QuoteCreationOperationView:
        """严格恢复不可变意图/首次完成并核对sheet显式列。"""
        try:
            values = {
                name: getattr(row, name)
                for name in QuoteCreationOperationView.model_fields
            }
            values["created_at"] = row.created_at.isoformat()
            values["completed_at"] = (
                row.completed_at.isoformat() if row.completed_at else None
            )
            value = QuoteCreationOperationView.model_validate_json(json.dumps(values))
            require_operation_integrity(value)
            if value.intent.cost_sheet_id != row.cost_sheet_id:
                raise CostFreezeError("facts_corrupt")
            return value
        except (SchemaError, ValueError, TypeError, DomainValidationError):
            raise CostFreezeError("facts_corrupt") from None

    def _basis(self, row: CostingQuoteBasisRow) -> FrozenCostBasis:
        """全量JSON和显式外键/时钟/hash列均复验，不返回裁剪快照。"""
        try:
            value = FrozenCostBasis.model_validate_json(json.dumps(row.payload))
            require_basis_integrity(value)
            fields = (
                "tenant_id",
                "basis_id",
                "operation_id",
                "cost_sheet_id",
                "opportunity_id",
                "request_hash",
                "context_hash",
                "sheet_hash",
                "basis_hash",
                "policy_id",
                "valid_until",
                "frozen_at",
            )
            if (
                any(getattr(value, n) != getattr(row, n) for n in fields)
                or row.scope_confirmation_id != value.scope_confirmation.confirmation_id
            ):
                raise CostFreezeError("facts_corrupt")
            return value
        except (SchemaError, ValueError, TypeError, DomainValidationError):
            raise CostFreezeError("facts_corrupt") from None

    def _binding(
        self, operation: QuoteCreationOperationView, basis: FrozenCostBasis
    ) -> None:
        """双向FK之外验证原意图与业务快照绑定，损坏不猜选可用记录。"""
        intent = operation.intent
        if (
            basis.tenant_id,
            basis.operation_id,
            basis.basis_id,
            basis.request_hash,
            basis.cost_sheet_id,
            basis.opportunity_id,
            basis.context_hash,
            basis.sheet_hash,
            basis.valid_until,
            basis.scope_confirmation.confirmation_id,
            basis.scope_confirmation.content_hash,
            basis.scope_confirmation.terms,
            basis.pricing_options.unit_price,
            basis.pricing_options.rounding.model_dump(),
            basis.quote_fx.fx_id if basis.quote_fx is not None else None,
            basis.frozen_at,
        ) != (
            operation.tenant_id,
            operation.operation_id,
            operation.basis_id,
            operation.request_hash,
            intent.cost_sheet_id,
            intent.opportunity_id,
            intent.expected_context_hash,
            intent.expected_sheet_hash,
            intent.valid_until,
            intent.scope_confirmation_id,
            intent.scope_confirmation_hash,
            intent.terms,
            intent.unit_price,
            intent.rounding.model_dump(),
            intent.quote_fx_ref,
            operation.created_at,
        ):
            raise CostFreezeError("facts_corrupt")

    async def _bound_operation(
        self, row: QuoteCreationOperationRow
    ) -> QuoteCreationOperationView:
        operation = self._operation(row)
        basis = await self._session.scalar(
            select(CostingQuoteBasisRow).where(
                CostingQuoteBasisRow.tenant_id == self._tenant_id,
                CostingQuoteBasisRow.basis_id == operation.basis_id,
            )
        )
        if basis is None:
            raise CostFreezeError("facts_corrupt")
        self._binding(operation, self._basis(basis))
        return operation

    async def get_operation_by_key(
        self, tenant_id: TenantId, key: str
    ) -> QuoteCreationOperationView | None:
        """tenant+key永久绑定；调用者在写入路径先锁creation key。"""
        self._require_tenant(tenant_id, "creation_key_read")
        row = await self._session.scalar(
            select(QuoteCreationOperationRow)
            .where(
                QuoteCreationOperationRow.tenant_id == tenant_id,
                QuoteCreationOperationRow.idempotency_key == key,
            )
            .execution_options(populate_existing=True)
        )
        return await self._bound_operation(row) if row else None

    async def get_operation(
        self, tenant_id: TenantId, operation_id: str
    ) -> QuoteCreationOperationView | None:
        """按操作ID恢复原记录；不读跨域报价表。"""
        self._require_tenant(tenant_id, "creation_read")
        row = await self._session.scalar(
            select(QuoteCreationOperationRow)
            .where(
                QuoteCreationOperationRow.tenant_id == tenant_id,
                QuoteCreationOperationRow.operation_id == operation_id,
            )
            .execution_options(populate_existing=True)
        )
        return await self._bound_operation(row) if row else None

    async def pending_for_sheet(
        self, tenant_id: TenantId, cost_sheet_id: CostSheetId
    ) -> QuoteCreationOperationView | None:
        """部分唯一索引保证每张成本表至多一个待恢复操作。"""
        self._require_tenant(tenant_id, "creation_pending_read")
        rows = (
            await self._session.scalars(
                select(QuoteCreationOperationRow).where(
                    QuoteCreationOperationRow.tenant_id == tenant_id,
                    QuoteCreationOperationRow.cost_sheet_id == cost_sheet_id,
                    QuoteCreationOperationRow.state == "frozen",
                )
            )
        ).all()
        if len(rows) > 1:
            raise CostFreezeError("facts_corrupt")
        return await self._bound_operation(rows[0]) if rows else None

    async def completed_for_revision(
        self,
        tenant_id: TenantId,
        cost_sheet_id: CostSheetId,
        quote_id: QuoteId,
        quote_version: int,
    ) -> QuoteCreationOperationView | None:
        """同session精确过滤旧quote/version，不把其它版本或tenant回执当授权。"""
        self._require_tenant(tenant_id, "creation_revision_read")
        rows = (
            await self._session.scalars(
                select(QuoteCreationOperationRow).where(
                    QuoteCreationOperationRow.tenant_id == tenant_id,
                    QuoteCreationOperationRow.cost_sheet_id == cost_sheet_id,
                    QuoteCreationOperationRow.state == "completed",
                    QuoteCreationOperationRow.completion["quote_id"].astext == quote_id,
                    QuoteCreationOperationRow.completion["quote_version"].astext
                    == str(quote_version),
                )
            )
        ).all()
        if len(rows) > 1:
            raise CostFreezeError("facts_corrupt")
        return await self._bound_operation(rows[0]) if rows else None

    async def get_basis(
        self, tenant_id: TenantId, basis_id: str
    ) -> FrozenCostBasis | None:
        """恢复完整历史依据且复验操作双向业务绑定。"""
        self._require_tenant(tenant_id, "quote_basis_read")
        row = await self._session.scalar(
            select(CostingQuoteBasisRow).where(
                CostingQuoteBasisRow.tenant_id == tenant_id,
                CostingQuoteBasisRow.basis_id == basis_id,
            )
        )
        if row is None:
            return None
        basis = self._basis(row)
        operation = await self._session.scalar(
            select(QuoteCreationOperationRow).where(
                QuoteCreationOperationRow.tenant_id == tenant_id,
                QuoteCreationOperationRow.operation_id == basis.operation_id,
            )
        )
        if operation is None:
            raise CostFreezeError("facts_corrupt")
        self._binding(self._operation(operation), basis)
        return basis

    async def add_frozen(
        self,
        tenant_id: TenantId,
        basis: FrozenCostBasis,
        operation: QuoteCreationOperationView,
    ) -> None:
        """依据与操作同事务插入，FK明确延迟至提交；中途异常全部回滚。"""
        self._require_tenant(tenant_id, "quote_frozen_add")
        if basis.tenant_id != tenant_id or operation.state != "frozen":
            raise CostFreezeError("facts_corrupt")
        require_basis_integrity(basis)
        require_operation_integrity(operation)
        self._binding(operation, basis)
        fields = (
            "tenant_id",
            "basis_id",
            "operation_id",
            "cost_sheet_id",
            "opportunity_id",
            "request_hash",
            "context_hash",
            "sheet_hash",
            "basis_hash",
            "policy_id",
            "valid_until",
            "frozen_at",
        )
        await self._session.execute(
            insert(CostingQuoteBasisRow).values(
                **{name: getattr(basis, name) for name in fields},
                scope_confirmation_id=basis.scope_confirmation.confirmation_id,
                payload=basis.model_dump(mode="json"),
            )
        )
        fields = (
            "tenant_id",
            "operation_id",
            "idempotency_key",
            "request_hash",
            "basis_id",
            "state",
            "created_at",
            "completed_at",
        )
        await self._session.execute(
            insert(QuoteCreationOperationRow).values(
                **{name: getattr(operation, name) for name in fields},
                cost_sheet_id=operation.intent.cost_sheet_id,
                intent=operation.intent.model_dump(mode="json"),
                completion=None,
            )
        )

    async def complete(
        self,
        tenant_id: TenantId,
        operation_id: str,
        receipt: QuoteCreationCompletion,
        at: datetime,
    ) -> None:
        """仅frozen首次转completed，已完成同回执不再触发UPDATE。"""
        self._require_tenant(tenant_id, "creation_complete")
        operation = await self.get_operation(tenant_id, operation_id)
        if operation is None:
            raise CostFreezeError("record_not_found")
        require_completion(operation, receipt)
        if operation.state == "completed":
            return
        await self._session.execute(
            update(QuoteCreationOperationRow)
            .where(
                QuoteCreationOperationRow.tenant_id == tenant_id,
                QuoteCreationOperationRow.operation_id == operation_id,
                QuoteCreationOperationRow.state == "frozen",
            )
            .values(
                state="completed",
                completed_at=at,
                completion=receipt.model_dump(mode="json"),
            )
        )

    async def mark_sheet_locked_once(
        self, tenant_id: TenantId, cost_sheet_id: CostSheetId, at: datetime
    ) -> None:
        """已持sheet行锁后，仅首次窄写locked_at，不重写item/核算FX。"""
        self._require_tenant(tenant_id, "quote_sheet_lock")
        await CostSheetRepositoryImpl(self._session, tenant_id).mark_locked_once(
            tenant_id, cost_sheet_id, at
        )
