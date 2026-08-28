"""报价同session窄仓储，只映射/持久约束，不复制角色或报价适用性。"""

import json
from datetime import datetime

from pydantic import ValidationError as SchemaError
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession

from domains.quotations.content import require_content_integrity
from domains.quotations.errors import QuotationError, QuotationUnavailableError
from domains.quotations.file_schemas import QuoteFileRecord, QuoteFileView
from domains.quotations.schemas import (
    QuoteApprovalApplicationReceipt,
    QuoteApprovalFact,
    QuoteApprovalSubmission,
    QuoteContentSnapshot,
    QuoteDetailView,
    QuoteIssuer,
    QuoteSendReceipt,
    QuoteState,
    QuoteStateEvent,
    StoredQuoteIssuer,
)
from domains.quotations.service import quote_approval_payload_hash
from infra.db.base import TenantScopedRepository
from infra.db.tables import (
    QuotationApprovalBindingRow,
    QuotationApprovalReceiptRow,
    QuotationEvidenceRefRow,
    QuotationFileRow,
    QuotationIssuerRow,
    QuotationLineRow,
    QuotationRow,
    QuotationSendReceiptRow,
    QuotationStateEventRow,
)
from shared.errors import TenantIsolationViolation, ValidationError
from shared.schemas.identifiers import (
    MessageAttemptId,
    OpportunityId,
    QuoteFileId,
    QuoteId,
    TenantId,
    new_id,
)


class QuotationVersionRepositoryImpl(TenantScopedRepository):
    """全部查询经绑定tenant基座，所有返回内容逐次验证。"""

    def _file_record(self, row: QuotationFileRow) -> QuoteFileRecord:
        """列映射不重算业务hash；领域在每次对外返回前重验。"""
        try:
            return QuoteFileRecord(tenant_id=row.tenant_id, approval_run_id=row.approval_run_id,
                view=QuoteFileView(file_id=row.file_id, quote_id=row.quote_id,
                    quote_version=row.quote_version, artifact_id=row.artifact_id,
                    content_hash=row.artifact_hash, quote_content_hash=row.quote_content_hash,
                    customer_content_hash=row.customer_content_hash, template_version=row.template_version,
                    size_bytes=row.size_bytes, generated_at=row.generated_at))
        except (SchemaError, ValueError, TypeError):
            raise QuotationUnavailableError("storage_inconsistent") from None

    async def file_by_id(self, tenant_id: TenantId, quote_id: QuoteId, file_id: QuoteFileId) -> QuoteFileRecord | None:
        """三个身份同时过滤，不按裸artifact或file访问其他报价。"""
        self._tenant(tenant_id)
        row = await self._session.scalar(self.scoped_query(QuotationFileRow).where(
            QuotationFileRow.quote_id == quote_id, QuotationFileRow.file_id == file_id))
        return None if row is None else self._file_record(row)

    async def file_by_template(self, tenant_id: TenantId, quote_id: QuoteId, template_version: str) -> QuoteFileRecord | None:
        """在本租户报价模板唯一约束下取原winner。"""
        self._tenant(tenant_id)
        row = await self._session.scalar(self.scoped_query(QuotationFileRow).where(
            QuotationFileRow.quote_id == quote_id, QuotationFileRow.template_version == template_version))
        return None if row is None else self._file_record(row)

    async def files_for_quote(self, tenant_id: TenantId, quote_id: QuoteId) -> tuple[QuoteFileRecord, ...]:
        """稳定顺序读取本报价全部文件，不开放跨报价列表。"""
        self._tenant(tenant_id)
        rows = await self._session.scalars(self.scoped_query(QuotationFileRow).where(
            QuotationFileRow.quote_id == quote_id).order_by(QuotationFileRow.template_version, QuotationFileRow.file_id))
        return tuple(self._file_record(row) for row in rows)

    async def add_file(self, tenant_id: TenantId, record: QuoteFileRecord) -> None:
        """同session只增，真实绑定与不可变由SQL兜底。"""
        self._tenant(tenant_id)
        if record.tenant_id != tenant_id:
            raise QuotationUnavailableError("storage_inconsistent")
        values = record.view.model_dump()
        values["artifact_hash"] = values.pop("content_hash")
        self._session.add(QuotationFileRow(tenant_id=tenant_id, approval_run_id=record.approval_run_id, **values))
        await self._session.flush()

    def __init__(self, session: AsyncSession, tenant_id: TenantId) -> None:
        """借用UoW会话，不自行提交。"""
        super().__init__(tenant_id)
        self._session = session

    def _tenant(self, tenant_id: TenantId) -> None:
        """写入和读取参数不能越过构造时租户。"""
        if tenant_id != self._tenant_id:
            raise TenantIsolationViolation("报价仓储租户不匹配")

    async def approval_bindings(self, tenant_id: TenantId, quote_id: QuoteId) -> tuple[QuoteApprovalFact,...]:
        """读原提交事实并核验列与安全payload，不重建当前决定。"""
        self._tenant(tenant_id)
        rows = await self._session.scalars(self.scoped_query(QuotationApprovalBindingRow).where(
            QuotationApprovalBindingRow.quote_id == quote_id).order_by(QuotationApprovalBindingRow.approval_type))
        facts = []
        try:
            for row in rows:
                fact = QuoteApprovalFact.model_validate_json(json.dumps(row.fact))
                if (fact.tenant_id,fact.approval_id,fact.approval_type,fact.request_hash,
                    fact.payload.quote_id,fact.payload.quote_version,fact.payload.content_hash,
                    quote_approval_payload_hash(fact.payload)) != (tenant_id,row.approval_id,
                    row.approval_type,row.request_hash,quote_id,row.quote_version,row.content_hash,row.payload_hash):
                    raise QuotationUnavailableError("storage_inconsistent")
                facts.append(fact)
            return tuple(facts)
        except (SchemaError,ValueError,TypeError,ValidationError):
            raise QuotationUnavailableError("storage_inconsistent") from None

    async def add_approval_bindings(self, tenant_id: TenantId, submission: QuoteApprovalSubmission) -> None:
        """一次新增唯一轮全部包，SQL验证同quote及原包绑定。"""
        self._tenant(tenant_id)
        if submission.tenant_id != tenant_id:
            raise QuotationUnavailableError("storage_inconsistent")
        for fact in submission.facts:
            self._session.add(QuotationApprovalBindingRow(tenant_id=tenant_id,quote_id=submission.quote_id,
                approval_type=fact.approval_type,approval_id=fact.approval_id,
                quote_version=submission.quote_version,content_hash=submission.content_hash,
                bound_at=fact.created_at,request_hash=fact.request_hash,
                payload_hash=quote_approval_payload_hash(fact.payload),fact=fact.model_dump(mode="json")))
        await self._session.flush()

    async def approval_receipt(self, tenant_id: TenantId, quote_id: QuoteId) -> QuoteApprovalApplicationReceipt | None:
        """只返回数据库真实成功快照，完整业务绑定仍由报价域校验。"""
        self._tenant(tenant_id)
        row = await self._session.scalar(self.scoped_query(QuotationApprovalReceiptRow).where(
            QuotationApprovalReceiptRow.quote_id == quote_id))
        if row is None:
            return None
        try:
            values = {name:getattr(row,name) for name in QuoteApprovalApplicationReceipt.model_fields}
            values["applied_at"] = row.applied_at.isoformat()
            return QuoteApprovalApplicationReceipt.model_validate_json(json.dumps(values))
        except (SchemaError,ValueError,TypeError):
            raise QuotationUnavailableError("storage_inconsistent") from None

    async def add_approval_receipt(self, tenant_id: TenantId, receipt: QuoteApprovalApplicationReceipt) -> None:
        """同session只增，成功前任何故障均随报价事务回滚。"""
        self._tenant(tenant_id)
        if receipt.tenant_id != tenant_id:
            raise QuotationUnavailableError("storage_inconsistent")
        values = receipt.model_dump(mode="json")
        values["applied_at"] = receipt.applied_at
        self._session.add(QuotationApprovalReceiptRow(**values))
        await self._session.flush()

    async def lock_opportunity(
        self, tenant_id: TenantId, opportunity_id: OpportunityId
    ) -> None:
        """报价机会advisory锁与外层Opportunity SHARE兼容。"""
        self._tenant(tenant_id)
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {
                "key": json.dumps(
                    ["quotation-create-v1", tenant_id, opportunity_id],
                    separators=(",", ":"),
                )
            },
        )

    def _detail(self, row: QuotationRow) -> QuoteDetailView:
        """内容hash与显式列都必须同一绑定，不读最新Need/issuer重建。"""
        try:
            content = QuoteContentSnapshot.model_validate_json(json.dumps(row.content))
            require_content_integrity(content)
            names = (
                "tenant_id",
                "quote_id",
                "opportunity_id",
                "version",
                "operation_id",
                "request_hash",
                "content_hash",
                "prepared_by",
                "owner_id",
                "replaces_quote_id",
                "replaced_quote_version",
                "valid_until",
                "created_at",
            )
            if any(getattr(content, n) != getattr(row, n) for n in names) or (
                content.basis.basis_id,
                content.basis.cost_sheet_id,
                content.issuer.issuer_id,
            ) != (row.basis_id, row.cost_sheet_id, row.issuer_id):
                raise QuotationUnavailableError("storage_inconsistent")
            return QuoteDetailView(content=content, state=QuoteState(row.state))
        except (SchemaError, ValueError, TypeError, ValidationError):
            raise QuotationUnavailableError("storage_inconsistent") from None

    async def get(
        self, tenant_id: TenantId, quote_id: QuoteId, *, for_update: bool = False
    ) -> QuoteDetailView | None:
        """按报价ID读严格快照；行锁只由已取机会锁的调用方请求。"""
        self._tenant(tenant_id)
        query = self.scoped_query(QuotationRow).where(QuotationRow.quote_id == quote_id)
        row = await self._session.scalar(
            query.with_for_update() if for_update else query
        )
        return self._detail(row) if row else None

    async def get_by_operation(
        self, tenant_id: TenantId, operation_id: str
    ) -> QuoteDetailView | None:
        """按本租户永久操作身份查唯一真实报价。"""
        self._tenant(tenant_id)
        row = await self._session.scalar(
            self.scoped_query(QuotationRow).where(
                QuotationRow.operation_id == operation_id
            )
        )
        return self._detail(row) if row else None

    async def list_versions(
        self, tenant_id: TenantId, opportunity_id: OpportunityId
    ) -> tuple[QuoteDetailView, ...]:
        """版本降序包含终态历史。"""
        self._tenant(tenant_id)
        rows = await self._session.scalars(
            self.scoped_query(QuotationRow)
            .where(QuotationRow.opportunity_id == opportunity_id)
            .order_by(QuotationRow.version.desc())
        )
        return tuple(self._detail(row) for row in rows)

    async def add(self, tenant_id: TenantId, quote: QuoteDetailView) -> None:
        """一次插入draft内容/行/全部依据/created审计；不提供update内容入口。"""
        self._tenant(tenant_id)
        c = quote.content
        if c.tenant_id != tenant_id or quote.state != QuoteState.DRAFT:
            raise QuotationError("invalid_state")
        require_content_integrity(c)
        names = (
            "tenant_id",
            "quote_id",
            "opportunity_id",
            "version",
            "operation_id",
            "request_hash",
            "content_hash",
            "prepared_by",
            "owner_id",
            "replaces_quote_id",
            "replaced_quote_version",
            "valid_until",
            "created_at",
        )
        self._session.add(
            QuotationRow(
                **{n: getattr(c, n) for n in names},
                state=quote.state.value,
                basis_id=c.basis.basis_id,
                cost_sheet_id=c.basis.cost_sheet_id,
                issuer_id=c.issuer.issuer_id,
                content=c.model_dump(mode="json"),
            )
        )
        await self._session.flush()
        for line in c.lines:
            self._session.add(
                QuotationLineRow(
                    tenant_id=tenant_id,
                    quote_id=c.quote_id,
                    line_number=line.line_number,
                    payload=line.model_dump(mode="json"),
                )
            )
        for evidence in c.basis.price_evidence:
            self._session.add(
                QuotationEvidenceRefRow(
                    tenant_id=tenant_id,
                    quote_id=c.quote_id,
                    evidence_id=evidence.evidence_id,
                    evidence_hash=evidence.evidence_hash,
                    kind=evidence.kind,
                )
            )
        self._session.add(
            QuotationStateEventRow(
                tenant_id=tenant_id,
                event_id=new_id("qev"),
                quote_id=c.quote_id,
                from_state=None,
                to_state="draft",
                actor_id=c.prepared_by,
                reason="created",
                at=c.created_at,
                reference_id=c.operation_id,
            )
        )
        await self._session.flush()

    async def transition(
        self,
        tenant_id: TenantId,
        quote_id: QuoteId,
        expected: QuoteState,
        target: QuoteState,
        event: QuoteStateEvent,
    ) -> bool:
        """状态CAS和同一事件原子落库；矩阵/理由由领域决定。"""
        self._tenant(tenant_id)
        if (event.quote_id, event.from_state, event.to_state) != (
            quote_id,
            expected,
            target,
        ):
            raise QuotationError("invalid_state")
        result = await self._session.execute(
            update(QuotationRow)
            .where(
                QuotationRow.tenant_id == tenant_id,
                QuotationRow.quote_id == quote_id,
                QuotationRow.state == expected.value,
            )
            .values(state=target.value)
            .returning(QuotationRow.quote_id)
        )
        if result.scalar_one_or_none() != quote_id:
            return False
        self._session.add(
            QuotationStateEventRow(
                tenant_id=tenant_id, **event.model_dump(mode="python")
            )
        )
        await self._session.flush()
        return True

    async def overdue_opportunities(
        self, tenant_id: TenantId, *, now: datetime, limit: int
    ) -> tuple[OpportunityId, ...]:
        """不持有候选行锁，避免expiry反向锁机会。"""
        self._tenant(tenant_id)
        rows = await self._session.scalars(
            self.scoped_query(QuotationRow)
            .where(
                QuotationRow.state.in_(
                    ("draft", "pending_approval", "approved", "sent")
                ),
                QuotationRow.valid_until <= now,
            )
            .order_by(QuotationRow.valid_until, QuotationRow.quote_id)
            .limit(limit)
        )
        return tuple(dict.fromkeys(OpportunityId(row.opportunity_id) for row in rows))

    async def lock_issuer(self, tenant_id: TenantId) -> None:
        """租户专用抬头事务锁，不影响已选定快照的报价lease。"""
        self._tenant(tenant_id)
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {
                "key": json.dumps(
                    ["quotation-issuer-v1", tenant_id], separators=(",", ":")
                )
            },
        )

    def _issuer(self, row: QuotationIssuerRow) -> StoredQuoteIssuer:
        """完整抬头和显式确认列共同验证。"""
        from domains.quotations.content import quote_issuer_hash

        try:
            issuer = QuoteIssuer.model_validate_json(json.dumps(row.payload))
            if (
                issuer.issuer_id,
                issuer.content_hash,
                issuer.confirmed_by,
                issuer.confirmed_at,
            ) != (
                row.issuer_id,
                row.content_hash,
                row.confirmed_by,
                row.confirmed_at,
            ) or quote_issuer_hash(issuer) != issuer.content_hash:
                raise QuotationUnavailableError("storage_inconsistent")
            return StoredQuoteIssuer(
                issuer=issuer,
                version=row.version,
                idempotency_key=row.idempotency_key,
                request_hash=row.request_hash,
            )
        except (SchemaError, ValueError, TypeError, ValidationError):
            raise QuotationUnavailableError("storage_inconsistent") from None

    async def issuer_by_key(
        self, tenant_id: TenantId, key: str
    ) -> StoredQuoteIssuer | None:
        """真实永久幂等键映射。"""
        self._tenant(tenant_id)
        row = await self._session.scalar(
            self.scoped_query(QuotationIssuerRow).where(
                QuotationIssuerRow.idempotency_key == key
            )
        )
        return self._issuer(row) if row else None

    async def current_issuer(self, tenant_id: TenantId) -> QuoteIssuer | None:
        """按最大确认版本选当前抬头。"""
        record = await self.current_issuer_record(tenant_id)
        return record.issuer if record else None

    async def current_issuer_record(
        self, tenant_id: TenantId
    ) -> StoredQuoteIssuer | None:
        """完整版本记录供锁内分配；与context reader复用同解码。"""
        self._tenant(tenant_id)
        row = await self._session.scalar(
            self.scoped_query(QuotationIssuerRow)
            .order_by(QuotationIssuerRow.version.desc())
            .limit(1)
        )
        return self._issuer(row) if row else None

    async def get_issuer(
        self, tenant_id: TenantId, issuer_id: str
    ) -> QuoteIssuer | None:
        """读取报价lease选定的版本，不改为最新。"""
        self._tenant(tenant_id)
        row = await self._session.scalar(
            self.scoped_query(QuotationIssuerRow).where(
                QuotationIssuerRow.issuer_id == issuer_id
            )
        )
        return self._issuer(row).issuer if row else None

    async def add_issuer(self, tenant_id: TenantId, record: StoredQuoteIssuer) -> None:
        """保存老板已确认完整不可变抬头。"""
        self._tenant(tenant_id)
        i = record.issuer
        self._session.add(
            QuotationIssuerRow(
                tenant_id=tenant_id,
                issuer_id=i.issuer_id,
                version=record.version,
                idempotency_key=record.idempotency_key,
                request_hash=record.request_hash,
                content_hash=i.content_hash,
                confirmed_by=i.confirmed_by,
                confirmed_at=i.confirmed_at,
                payload=i.model_dump(mode="json"),
            )
        )
        await self._session.flush()

    async def send_receipt(
        self, tenant_id: TenantId, attempt_id: MessageAttemptId
    ) -> QuoteSendReceipt | None:
        """只返回本租户真实保存回执。"""
        self._tenant(tenant_id)
        row = await self._session.scalar(
            self.scoped_query(QuotationSendReceiptRow).where(
                QuotationSendReceiptRow.attempt_id == attempt_id
            )
        )
        return (
            QuoteSendReceipt(
                **{n: getattr(row, n) for n in QuoteSendReceipt.model_fields}
            )
            if row
            else None
        )

    async def add_send_receipt(
        self, tenant_id: TenantId, receipt: QuoteSendReceipt
    ) -> None:
        """和approved→sent事件由上层同事务提交。"""
        self._tenant(tenant_id)
        if receipt.tenant_id != tenant_id:
            raise QuotationError("receipt_invalid")
        self._session.add(QuotationSendReceiptRow(**receipt.model_dump(mode="python")))
        await self._session.flush()
