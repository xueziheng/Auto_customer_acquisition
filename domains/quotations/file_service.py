"""文件metadata关联：当前机会范围与历史批准归属，不授予正式文件使用权。"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import TYPE_CHECKING, Protocol, cast

from domains.quotations.approval_rules import (
    build_quote_approval_snapshot,
    require_quote_approval_receipt,
    require_quote_run_binding,
)
from domains.quotations.approval_schemas import (
    QuoteApprovalApplicationReceipt,
    QuoteWorkflowRunFact,
)
from domains.quotations.approval_service import QuoteWorkflowRunReader
from domains.quotations.content import project_customer, require_content_integrity
from domains.quotations.errors import (
    QuotationUnavailableError,
    QuoteApprovalError,
    QuoteApprovalUnavailableError,
    QuoteFileAccessUnavailableError,
    QuoteFileError,
    QuoteFilePermissionError,
    QuoteFileUnavailableError,
)
from domains.quotations.file_schemas import (
    QuoteFileApprovalFact,
    QuoteFileRecord,
    QuoteFileView,
    QuoteGeneratedArtifactFact,
)
from domains.quotations.version_repository import (
    QuotationUnitOfWork,
    QuotationUowFactory,
)
from domains.quotations.version_schemas import QuoteDetailView
from shared.errors import PermissionDenied
from shared.schemas.identifiers import (
    ArtifactId,
    EmployeeId,
    OpportunityId,
    QuoteFileId,
    QuoteId,
    TenantId,
)
from shared.schemas.quote_document import customer_quote_hash
from shared.schemas.quote_facts import fact_identity
from shared.schemas.quote_files import QUOTE_PDF_TEMPLATE_VERSIONS

if TYPE_CHECKING:
    from domains.quotations.service import QuotationActorReader


class QuoteFileScopeAuthorizer(Protocol):
    """受信上层保护当前员工→机会范围，历史读取也须持有此租约。"""

    def guard(
        self,
        tenant_id: TenantId,
        opportunity_id: OpportunityId,
        *,
        actor_id: EmployeeId,
    ) -> AbstractAsyncContextManager[None]:
        """按当前机会ABAC保护到报价关联事务退出，不返回权限token。"""
        ...


class QuoteGeneratedArtifactReader(Protocol):
    """只读真实安全metadata，不读bytes或私有对象地址。"""

    async def read(
        self, tenant_id: TenantId, artifact_id: ArtifactId
    ) -> QuoteGeneratedArtifactFact | None:
        """缺记录含跨租户返回None，故障不能伪造成不存在。"""
        ...


class QuoteFileService(Protocol):
    """独立文件用途；record仅供受信生成/恢复调用，不接客户自报绑定。"""

    async def record_file(
        self,
        tenant_id: TenantId,
        quote_id: QuoteId,
        artifact_id: ArtifactId,
        *,
        actor_id: EmployeeId,
    ) -> QuoteFileView:
        """只从真实metadata与receipt新增关联，不改变报价状态。"""
        ...

    async def get_file(
        self,
        tenant_id: TenantId,
        quote_id: QuoteId,
        file_id: QuoteFileId,
        *,
        actor_id: EmployeeId,
    ) -> QuoteFileView:
        """精确读取原文件并重验三个hash及历史归属。"""
        ...

    async def list_files(
        self, tenant_id: TenantId, quote_id: QuoteId, *, actor_id: EmployeeId
    ) -> tuple[QuoteFileView, ...]:
        """仅列当前范围内单个报价的安全metadata。"""
        ...

    async def get_file_approval(
        self, tenant_id: TenantId, quote_id: QuoteId, *, actor_id: EmployeeId
    ) -> QuoteFileApprovalFact | None:
        """从真实receipt发现稳定run，不要求调用方制造executor。"""
        ...


def _identifier(value: str, prefix: str) -> None:
    """固定格式拒绝非法标识，不将任意值传给存储。"""
    if (
        not isinstance(value, str)
        or re.fullmatch(rf"{prefix}_[0-7][0-9A-HJKMNP-TV-Z]{{25}}", value) is None
    ):
        raise QuoteFileError("invalid_input")


class QuoteFileServiceImpl:
    """本域唯一文件实现；全部依赖必填，scope与metadata读取均无默认allow。"""

    def __init__(
        self,
        uow_factory: QuotationUowFactory,
        actor_reader: QuotationActorReader,
        scope_authorizer: QuoteFileScopeAuthorizer,
        artifact_reader: QuoteGeneratedArtifactReader,
        workflow_run_reader: QuoteWorkflowRunReader,
        *,
        id_generator: Callable[[str], str],
    ) -> None:
        """独立依赖本域端口，不反向调用门面的内部四角色读取。"""
        self._uows, self._actors, self._scope = (
            uow_factory,
            actor_reader,
            scope_authorizer,
        )
        self._artifacts, self._runs, self._id = (
            artifact_reader,
            workflow_run_reader,
            id_generator,
        )

    @asynccontextmanager
    async def _access(
        self, tenant: TenantId, quote_id: QuoteId, actor_id: EmployeeId
    ) -> AsyncIterator[QuoteDetailView]:
        """先真实actor，再不可变机会bootstrap，scope保持到内部提交退出。"""
        if any(
            dep is None
            for dep in (
                self._uows,
                self._actors,
                self._scope,
                self._artifacts,
                self._runs,
                self._id,
            )
        ):
            raise QuoteFileUnavailableError("dependency_unavailable")
        _identifier(tenant, "tn")
        _identifier(quote_id, "quo")
        try:
            fact_identity(actor_id)
        except ValueError:
            raise QuoteFileError("invalid_input") from None
        try:
            try:
                actor = await self._actors.read_current(tenant, actor_id)
            except Exception:  # noqa: BLE001 -- 员工依赖不可用不能默认许可
                raise QuoteFileUnavailableError("dependency_unavailable") from None
            if actor is None or (
                actor.tenant_id,
                actor.employee_id,
                actor.is_active,
            ) != (tenant, actor_id, True):
                raise QuoteFilePermissionError("permission_denied")
            async with self._uows(tenant) as uow:
                initial = await uow.quotes.get(tenant, quote_id)
            if initial is None or initial.content.tenant_id != tenant:
                raise QuoteFileError("not_found")
            async with self._scope.guard(
                tenant, initial.content.opportunity_id, actor_id=actor_id
            ):
                async with self._uows(tenant) as uow:
                    quote = await uow.quotes.get(tenant, quote_id)
                if quote is None or quote.content != initial.content:
                    raise QuoteFileUnavailableError("storage_inconsistent")
                require_content_integrity(quote.content)
                yield quote
        except (QuoteFileError, QuoteFileUnavailableError):
            raise
        except PermissionDenied:
            raise QuoteFilePermissionError("permission_denied") from None
        except (QuotationUnavailableError, QuoteApprovalUnavailableError, QuoteFileAccessUnavailableError) as error:
            raise QuoteFileUnavailableError(error.code) from None
        except QuoteApprovalError as error:
            if error.code == "workflow_binding_invalid":
                raise QuoteFileError("workflow_binding_invalid") from None
            raise QuoteFileUnavailableError("storage_inconsistent") from None
        except Exception:  # noqa: BLE001 -- 未分类基础设施错误禁止透出SQL或原文
            raise QuoteFileUnavailableError("dependency_unavailable") from None

    async def _receipt(
        self, uow: QuotationUnitOfWork, quote: QuoteDetailView
    ) -> QuoteApprovalApplicationReceipt | None:
        """持本域session读完整原组/前一版，只校历史，不取当前Need/政策。"""
        c = quote.content
        receipt = await uow.quotes.approval_receipt(c.tenant_id, c.quote_id)
        if receipt is not None:
            versions = await uow.quotes.list_versions(c.tenant_id, c.opportunity_id)
            previous = next(
                (v for v in versions if v.content.version == c.version - 1), None
            )
            if previous is not None:
                require_content_integrity(previous.content)
            snapshot = build_quote_approval_snapshot(quote, previous)
            bindings = await uow.quotes.approval_bindings(c.tenant_id, c.quote_id)
            require_quote_approval_receipt(
                snapshot, receipt, bindings, receipt.approval_run_id
            )
        return receipt

    async def _approval(
        self, quote: QuoteDetailView
    ) -> QuoteApprovalApplicationReceipt | None:
        """锁外读取真实run；completed/failed不影响已存在的历史成功归属。"""
        c = quote.content
        async with self._uows(c.tenant_id) as uow:
            receipt = await self._receipt(uow, quote)
        if receipt is not None:
            try:
                run = await self._runs.read(c.tenant_id, receipt.approval_run_id)
            except Exception:  # noqa: BLE001 -- run读取失败不得伪造历史归属
                raise QuoteFileUnavailableError("dependency_unavailable") from None
            require_quote_run_binding(
                c.tenant_id, c.quote_id, c.version, c.content_hash, run
            )
            run = cast(QuoteWorkflowRunFact, run)
            if run.run_id != receipt.approval_run_id:
                raise QuoteFileError("workflow_binding_invalid")
        return receipt

    async def _metadata(
        self, tenant: TenantId, artifact_id: ArtifactId
    ) -> QuoteGeneratedArtifactFact | None:
        """仅受信metadata端口，不fallback为客户端fact。"""
        try:
            return await self._artifacts.read(tenant, artifact_id)
        except QuoteFileUnavailableError:
            raise
        except Exception:  # noqa: BLE001 -- metadata基础设施错误必须固定脱敏
            raise QuoteFileUnavailableError("dependency_unavailable") from None

    def _validate_metadata(
        self,
        quote: QuoteDetailView,
        receipt: QuoteApprovalApplicationReceipt,
        meta: QuoteGeneratedArtifactFact,
        artifact_id: ArtifactId,
    ) -> None:
        """模板与所有metadata业务绑定严格匹配，不分配候选文件ID。"""
        c = quote.content
        if meta.generated_by not in QUOTE_PDF_TEMPLATE_VERSIONS:
            raise QuoteFileError("template_unsupported")
        if (
            meta.tenant_id,
            meta.artifact_id,
            meta.kind,
            meta.mime_type,
            meta.subject_ref,
            meta.sequence_number,
            meta.workflow_run_id,
            meta.idempotency_key,
        ) != (
            c.tenant_id,
            artifact_id,
            "quote_pdf",
            "application/pdf",
            c.quote_id,
            c.version,
            receipt.approval_run_id,
            f"{c.quote_id}:{c.version}:quote_pdf:{meta.generated_by}",
        ):
            raise QuoteFileError("metadata_mismatch")

    def _view(
        self,
        quote: QuoteDetailView,
        receipt: QuoteApprovalApplicationReceipt,
        meta: QuoteGeneratedArtifactFact,
        artifact_id: ArtifactId,
        file_id: QuoteFileId,
    ) -> QuoteFileView:
        """三个hash分别来自原报价、客户投影与真实metadata。"""
        self._validate_metadata(quote, receipt, meta, artifact_id)
        c = quote.content
        return QuoteFileView(
            file_id=file_id,
            quote_id=c.quote_id,
            quote_version=c.version,
            artifact_id=artifact_id,
            content_hash=meta.artifact_hash,
            quote_content_hash=c.content_hash,
            customer_content_hash=customer_quote_hash(project_customer(quote)),
            template_version=meta.generated_by,
            size_bytes=meta.size_bytes,
            generated_at=meta.generated_at,
        )

    async def record_file(
        self,
        tenant_id: TenantId,
        quote_id: QuoteId,
        artifact_id: ArtifactId,
        *,
        actor_id: EmployeeId,
    ) -> QuoteFileView:
        """metadata在写锁外读取，同机会advisory内重验并只增；未知提交不删文件。"""
        _identifier(artifact_id, "art")
        async with self._access(tenant_id, quote_id, actor_id) as quote:
            receipt = await self._approval(quote)
            if receipt is None:
                raise QuoteFileError("approval_missing")
            meta = await self._metadata(tenant_id, artifact_id)
            if meta is None:
                raise QuoteFileError("not_found")
            self._validate_metadata(quote, receipt, meta, artifact_id)
            try:
                async with self._uows(tenant_id) as uow:
                    await uow.quotes.lock_opportunity(
                        tenant_id, quote.content.opportunity_id
                    )
                    current = await uow.quotes.get(tenant_id, quote_id)
                    if (
                        current is None
                        or current.content != quote.content
                        or await self._receipt(uow, current) != receipt
                    ):
                        raise QuoteFileUnavailableError("storage_inconsistent")
                    existing = await uow.quotes.file_by_template(
                        tenant_id, quote_id, meta.generated_by
                    )
                    if existing is not None:
                        candidate = self._view(
                            quote, receipt, meta, artifact_id, existing.view.file_id
                        )
                        if (
                            existing.tenant_id != tenant_id
                            or existing.approval_run_id != receipt.approval_run_id
                            or existing.view != candidate
                        ):
                            raise QuoteFileError("file_conflict")
                        result = existing.view
                    else:
                        result = self._view(
                            quote,
                            receipt,
                            meta,
                            artifact_id,
                            QuoteFileId(self._id("qfl")),
                        )
                        await uow.quotes.add_file(
                            tenant_id,
                            QuoteFileRecord(
                                tenant_id=tenant_id,
                                view=result,
                                approval_run_id=receipt.approval_run_id,
                            ),
                        )
                    await uow.commit()
                return result
            except (
                QuoteFileError,
                QuoteFileUnavailableError,
                QuotationUnavailableError,
                QuoteApprovalError,
                QuoteApprovalUnavailableError,
            ):
                raise
            except Exception:  # noqa: BLE001 -- commit/close异常不证明关联未提交
                raise QuoteFileUnavailableError("storage_unknown") from None

    async def _check_file(
        self,
        quote: QuoteDetailView,
        record: QuoteFileRecord,
        receipt: QuoteApprovalApplicationReceipt | None,
    ) -> QuoteFileView:
        """原文件逐次重投影与metadata重读；损坏一律拒绝而非透传。"""
        if receipt is None or record.tenant_id != quote.content.tenant_id:
            raise QuoteFileUnavailableError("storage_inconsistent")
        meta = await self._metadata(record.tenant_id, record.view.artifact_id)
        if meta is None:
            raise QuoteFileUnavailableError("storage_inconsistent")
        try:
            expected = self._view(
                quote, receipt, meta, record.view.artifact_id, record.view.file_id
            )
        except QuoteFileError:
            raise QuoteFileUnavailableError("storage_inconsistent") from None
        if record.view != expected or record.approval_run_id != receipt.approval_run_id:
            raise QuoteFileUnavailableError("storage_inconsistent")
        return record.view

    async def _stored_approval(
        self, quote: QuoteDetailView
    ) -> QuoteApprovalApplicationReceipt | None:
        """已存文件的历史绑定损坏统一分类，与临时依赖失败区分。"""
        try:
            return await self._approval(quote)
        except (QuoteApprovalError, QuoteFileError):
            raise QuoteFileUnavailableError("storage_inconsistent") from None

    async def get_file(
        self,
        tenant_id: TenantId,
        quote_id: QuoteId,
        file_id: QuoteFileId,
        *,
        actor_id: EmployeeId,
    ) -> QuoteFileView:
        """当前scope内精确读关联并核原报价/客户hash、批准和metadata。"""
        _identifier(file_id, "qfl")
        async with self._access(tenant_id, quote_id, actor_id) as quote:
            async with self._uows(tenant_id) as uow:
                record = await uow.quotes.file_by_id(tenant_id, quote_id, file_id)
            if record is None:
                raise QuoteFileError("not_found")
            return await self._check_file(
                quote, record, await self._stored_approval(quote)
            )

    async def list_files(
        self, tenant_id: TenantId, quote_id: QuoteId, *, actor_id: EmployeeId
    ) -> tuple[QuoteFileView, ...]:
        """仅本报价的受信模板集合，按模板/ID升序，不阻止历史终态读取。"""
        async with self._access(tenant_id, quote_id, actor_id) as quote:
            async with self._uows(tenant_id) as uow:
                records = await uow.quotes.files_for_quote(tenant_id, quote_id)
            if len(records) > len(QUOTE_PDF_TEMPLATE_VERSIONS):
                raise QuoteFileUnavailableError("storage_inconsistent")
            if not records:
                return ()
            receipt = await self._stored_approval(quote)
            return tuple(
                [
                    await self._check_file(quote, record, receipt)
                    for record in sorted(
                        records, key=lambda r: (r.view.template_version, r.view.file_id)
                    )
                ]
            )

    async def get_file_approval(
        self, tenant_id: TenantId, quote_id: QuoteId, *, actor_id: EmployeeId
    ) -> QuoteFileApprovalFact | None:
        """只返回安全历史归属；不包含审批人/载荷，也不补记APPLIED。"""
        async with self._access(tenant_id, quote_id, actor_id) as quote:
            receipt = await self._approval(quote)
            if receipt is None:
                return None
            return QuoteFileApprovalFact(
                tenant_id=tenant_id,
                quote_id=quote_id,
                quote_version=receipt.quote_version,
                quote_content_hash=receipt.content_hash,
                approval_run_id=receipt.approval_run_id,
                approval_facts_hash=receipt.facts_hash,
                applied_at=receipt.applied_at,
            )
