"""正式文件实时授权与历史文件范围；不生成文件、不推进任何批准状态。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from pydantic import ValidationError as SchemaError

from domains.quotations.approval_rules import (
    build_quote_approval_snapshot,
    quote_approval_facts_hash,
    require_quote_approval_bindings,
    require_quote_approval_facts,
    require_quote_approval_receipt,
    require_quote_approved_decisions,
    require_quote_current_basis,
    require_quote_current_deciders,
    require_quote_run_binding,
)
from domains.quotations.approval_schemas import QuoteApprovalFact
from domains.quotations.approval_service import (
    QuoteApprovalPolicyReader,
    QuotePolicySelection,
    QuoteWorkflowRunReader,
)
from domains.quotations.content import (
    project_customer,
    require_content_integrity,
    validate_customer_projection,
)
from domains.quotations.context import QuoteContextProvider
from domains.quotations.errors import (
    QuotationError,
    QuotationUnavailableError,
    QuoteApprovalError,
    QuoteApprovalUnavailableError,
    QuoteContextError,
    QuoteContextUnavailableError,
    QuoteFileAccessError,
    QuoteFileAccessPermissionError,
    QuoteFileAccessUnavailableError,
    QuoteFileError,
    QuoteFileUnavailableError,
)
from domains.quotations.file_access_schemas import (
    QuoteFileBlockerCode,
    QuoteFileScopeFacts,
    QuoteFormalFileSnapshot,
)
from domains.quotations.file_schemas import QuoteFileView
from domains.quotations.file_service import QuoteFileService, _identifier
from domains.quotations.models import QuoteState
from domains.quotations.version_repository import QuotationUowFactory
from shared.errors import PermissionDenied
from shared.schemas.identifiers import (
    ApprovalId,
    EmployeeId,
    OpportunityId,
    QuoteFileId,
    QuoteId,
    TenantId,
)
from shared.schemas.provenance import FactualField
from shared.schemas.quote_document import customer_quote_hash
from shared.schemas.quote_facts import NeedQuoteFacts, fact_identity, fact_utc
from shared.schemas.quote_files import QUOTE_PDF_TEMPLATE_VERSIONS

if TYPE_CHECKING:
    from domains.quotations.service import QuotationActorReader


class QuoteFileApprovalFactsReader(Protocol):
    """仅受信approvals当前全组事实，不用历史receipt冒充当前状态。"""

    async def read(
        self, tenant_id: TenantId, approval_ids: tuple[ApprovalId, ...]
    ) -> tuple[QuoteApprovalFact, ...]:
        """逐ID读真实包；故障不得返回空组。"""
        ...


class QuoteFileNeedValidator(Protocol):
    """需求当前单位规则由demand公共服务提供，不读取客户原件。"""

    def require_current_unit(self, facts: NeedQuoteFacts) -> FactualField[str]:
        """无单位或来源陈旧必须失败关闭。"""
        ...


class QuoteFileAccessService(Protocol):
    """每次正式用途独立授权；历史不自动降级自正式用途。"""

    async def authorize(
        self, tenant_id: TenantId, quote_id: QuoteId, *, actor_id: EmployeeId
    ) -> QuoteFormalFileSnapshot:
        """检查当时的真实批准、全部当前依据及文件范围，零写。"""
        ...

    async def authorize_history(
        self,
        tenant_id: TenantId,
        quote_id: QuoteId,
        file_id: QuoteFileId,
        *,
        actor_id: EmployeeId,
    ) -> QuoteFileView:
        """只委托历史完整绑定及当前scope，不追逐当前Need或政策。"""
        ...


def require_quote_file_scope(facts: QuoteFileScopeFacts) -> None:
    """boss、本人owner的sales或owner当前直属manager；成本角色不获文件权。"""
    actor, owner = facts.actor, facts.owner
    if (
        actor.tenant_id != facts.tenant_id
        or owner.tenant_id != facts.tenant_id
        or not actor.is_active
        or not owner.is_active
        or not (
            actor.role == "boss"
            or actor.role == "sales"
            and actor.employee_id == owner.employee_id
            or actor.role == "manager"
            and owner.manager_id == actor.employee_id
        )
    ):
        raise QuoteFileAccessPermissionError("permission_denied")


@asynccontextmanager
async def file_access_errors() -> AsyncIterator[None]:
    """精确分离当前商业失效、持久损坏和基础设施故障，取消原样。"""
    try:
        yield
    except (
        QuoteFileAccessError,
        QuoteFileAccessPermissionError,
        QuoteFileAccessUnavailableError,
    ):
        raise
    except PermissionDenied:
        raise QuoteFileAccessPermissionError("permission_denied") from None
    except QuoteFileError as error:
        if error.code in {"invalid_input", "not_found"}:
            raise
        if error.code == "approval_missing":
            raise QuoteFileAccessError("approval_missing") from None
        raise QuoteFileAccessUnavailableError("storage_inconsistent") from None
    except (
        QuotationUnavailableError,
        QuoteApprovalUnavailableError,
        QuoteFileUnavailableError,
        QuoteContextUnavailableError,
    ) as error:
        code = (
            error.code
            if error.code in {"lock_timeout", "storage_inconsistent"}
            else "dependency_unavailable"
        )
        raise QuoteFileAccessUnavailableError(code) from None
    except QuoteContextError as error:
        if error.code == "facts_corrupt":
            raise QuoteFileAccessUnavailableError("storage_inconsistent") from None
        raise QuoteFileAccessError("context_changed") from None
    except QuotationError as error:
        if error.code == "issuer_not_found":
            raise QuoteFileAccessError("context_changed") from None
        raise QuoteFileAccessUnavailableError("storage_inconsistent") from None
    except QuoteApprovalError as error:
        mapping: dict[str, QuoteFileBlockerCode] = {
            "approval_expired": "approval_expired",
            "decider_invalid": "decider_invalid",
            "context_changed": "context_changed",
            "policy_stale": "policy_stale",
            "evidence_invalid": "basis_invalid",
            "evidence_expired": "basis_invalid",
        }
        if error.code in mapping:
            raise QuoteFileAccessError(mapping[error.code]) from None
        raise QuoteFileAccessUnavailableError("storage_inconsistent") from None
    except SchemaError:
        raise QuoteFileAccessUnavailableError("storage_inconsistent") from None
    except Exception:  # noqa: BLE001 -- 未知reader异常不能伪装成商业blocker
        raise QuoteFileAccessUnavailableError("dependency_unavailable") from None


class ContextQuoteFileScopeAuthorizer:
    """T6 guard只依赖context与同域唯一scope，避免文件服务构造环。"""

    def __init__(self, contexts: QuoteContextProvider) -> None:
        self._contexts = contexts

    @asynccontextmanager
    async def guard(
        self,
        tenant_id: TenantId,
        opportunity_id: OpportunityId,
        *,
        actor_id: EmployeeId,
    ) -> AsyncIterator[None]:
        """租约内业务scope唯一判权；不读取报价内部四角色入口。"""
        async with (
            file_access_errors(),
            self._contexts.open_file_scope(
                tenant_id, opportunity_id, actor_id
            ) as facts,
        ):
            require_quote_file_scope(facts)
            yield


class QuoteFileAccessServiceImpl:
    """只读正式授权，外部bytes操作必须在本方法全部租约退出后。"""

    def __init__(
        self,
        uow_factory: QuotationUowFactory,
        actor_reader: QuotationActorReader,
        contexts: QuoteContextProvider,
        approval_facts: QuoteFileApprovalFactsReader,
        need_validator: QuoteFileNeedValidator,
        policies: QuoteApprovalPolicyReader,
        workflow_runs: QuoteWorkflowRunReader,
        files: QuoteFileService,
        *,
        now: Callable[[], datetime],
        template_version: str,
    ) -> None:
        self._uows, self._actors, self._contexts, self._facts = (
            uow_factory,
            actor_reader,
            contexts,
            approval_facts,
        )
        self._need, self._policies, self._runs, self._files = (
            need_validator,
            policies,
            workflow_runs,
            files,
        )
        self._now, self._template = now, template_version

    @asynccontextmanager
    async def _policy_lease(
        self, tenant_id: TenantId, category: str
    ) -> AsyncIterator[QuotePolicySelection]:
        """与T5同语义关闭政策租约，不把文件业务拒绝交给成本UoW重新分类。"""
        lease = self._policies.open(tenant_id, category)
        selection = await lease.__aenter__()
        primary: BaseException | None = None
        try:
            yield selection
        except BaseException as error:
            primary = error
            raise
        finally:
            try:
                await lease.__aexit__(None, None, None)
            except BaseException:
                if not isinstance(primary, asyncio.CancelledError):
                    raise

    async def authorize_history(
        self,
        tenant_id: TenantId,
        quote_id: QuoteId,
        file_id: QuoteFileId,
        *,
        actor_id: EmployeeId,
    ) -> QuoteFileView:
        """T6已核当前actor/范围与历史全部绑定，无fresh默认回退。"""
        async with file_access_errors():
            return await self._files.get_file(
                tenant_id, quote_id, file_id, actor_id=actor_id
            )

    async def authorize(
        self, tenant_id: TenantId, quote_id: QuoteId, *, actor_id: EmployeeId
    ) -> QuoteFormalFileSnapshot:
        """先短scope拒绝，再一次锁齐当前事实，全部等待后重核政策与时钟。"""
        _identifier(tenant_id, "tn")
        _identifier(quote_id, "quo")
        try:
            fact_identity(actor_id)
        except ValueError:
            raise QuoteFileError("invalid_input") from None
        async with file_access_errors():
            actor = await self._actors.read_current(tenant_id, actor_id)
            if actor is None or (
                actor.tenant_id,
                actor.employee_id,
                actor.is_active,
            ) != (tenant_id, actor_id, True):
                raise QuoteFileAccessPermissionError("permission_denied")
            async with self._uows(tenant_id) as uow:
                initial = await uow.quotes.get(tenant_id, quote_id)
            if initial is None or initial.content.tenant_id != tenant_id:
                raise QuoteFileError("not_found")
            c = initial.content
            async with self._contexts.open_file_scope(
                tenant_id, c.opportunity_id, actor_id
            ) as scope:
                require_quote_file_scope(scope)
                initial_owner = scope.owner.employee_id
            approval = await self._files.get_file_approval(
                tenant_id, quote_id, actor_id=actor_id
            )
            if approval is None:
                raise QuoteFileAccessError("approval_missing")
            async with self._uows(tenant_id) as uow:
                original_receipt = await uow.quotes.approval_receipt(
                    tenant_id, quote_id
                )
                original_bindings = await uow.quotes.approval_bindings(
                    tenant_id, quote_id
                )
            initial_facts = await self._facts.read(
                tenant_id, tuple(f.approval_id for f in original_bindings)
            )
            run = await self._runs.read(tenant_id, approval.approval_run_id)
            require_quote_run_binding(
                tenant_id, quote_id, c.version, c.content_hash, run
            )
            if (
                run is None
                or run.run_id != approval.approval_run_id
                or original_receipt is None
            ):
                raise QuoteFileAccessUnavailableError("storage_inconsistent")
            if (approval.quote_content_hash, approval.approval_facts_hash) != (
                c.content_hash,
                original_receipt.facts_hash,
            ):
                raise QuoteFileAccessUnavailableError("storage_inconsistent")
            deciders = tuple(
                sorted(
                    {f.decided_by for f in initial_facts if f.decided_by is not None}
                )
            )
            if not deciders:
                raise QuoteFileAccessUnavailableError("storage_inconsistent")
            async with self._contexts.open_for_file(
                tenant_id,
                c.opportunity_id,
                actor_id,
                prepared_by=c.prepared_by,
                decider_ids=deciders,
            ) as current:
                business = current.business
                require_quote_file_scope(
                    QuoteFileScopeFacts(
                        tenant_id=tenant_id,
                        opportunity_id=c.opportunity_id,
                        actor=business.runtime.current_actor,
                        owner=business.runtime.owner,
                    )
                )
                if business.owner_id != initial_owner:
                    raise QuoteFileAccessError("context_changed")
                async with self._uows(tenant_id) as uow:
                    await uow.quotes.lock_opportunity(tenant_id, c.opportunity_id)
                    quote = await uow.quotes.get(tenant_id, quote_id)
                    bindings = await uow.quotes.approval_bindings(tenant_id, quote_id)
                    receipt = await uow.quotes.approval_receipt(tenant_id, quote_id)
                    if (
                        quote is None
                        or quote.content != c
                        or bindings != original_bindings
                        or receipt != original_receipt
                    ):
                        raise QuoteFileAccessError("context_changed")
                    require_content_integrity(c)
                    facts = await self._facts.read(
                        tenant_id, tuple(f.approval_id for f in bindings)
                    )
                    if quote_approval_facts_hash(facts) != quote_approval_facts_hash(
                        initial_facts
                    ):
                        raise QuoteFileAccessError("context_changed")
                    versions = await uow.quotes.list_versions(
                        tenant_id, c.opportunity_id
                    )
                    previous = next(
                        (v for v in versions if v.content.version == c.version - 1),
                        None,
                    )
                    if previous is not None:
                        require_content_integrity(previous.content)
                    snapshot = build_quote_approval_snapshot(quote, previous)
                    async with self._policy_lease(
                        tenant_id, business.category
                    ) as selection:
                        policy = await selection.current()
                        now = fact_utc(self._now())
                        require_quote_approval_receipt(
                            snapshot, receipt, bindings, approval.approval_run_id
                        )
                        require_quote_approval_facts(
                            snapshot, facts, approval.approval_run_id
                        )
                        require_quote_approval_bindings(facts, bindings)
                        if quote_approval_facts_hash(facts) != receipt.facts_hash:
                            raise QuoteFileAccessUnavailableError(
                                "storage_inconsistent"
                            )
                        if quote.state not in {QuoteState.APPROVED, QuoteState.SENT}:
                            raise QuoteFileAccessError("quote_inactive")
                        if c.valid_until <= now:
                            raise QuoteFileAccessError("quote_expired")
                        if any(f.state not in {"approved", "applied"} for f in facts):
                            raise QuoteFileAccessError("approval_invalid")
                        require_quote_approved_decisions(
                            facts, now=now, allow_applied=True
                        )
                        require_quote_current_deciders(
                            quote, facts, business, current.deciders
                        )
                        self._need.require_current_unit(business.need_facts)
                        require_quote_current_basis(quote, business, policy, now=now)
                        customer = project_customer(quote)
                        validate_customer_projection(customer, quote)
                        if self._template not in QUOTE_PDF_TEMPLATE_VERSIONS:
                            raise QuoteFileError("template_unsupported")
                        return QuoteFormalFileSnapshot(
                            tenant_id=tenant_id,
                            quote_id=quote_id,
                            opportunity_id=c.opportunity_id,
                            quote_version=c.version,
                            quote_content_hash=c.content_hash,
                            customer_content_hash=customer_quote_hash(customer),
                            approval_run_id=receipt.approval_run_id,
                            approval_facts_hash=receipt.facts_hash,
                            template_version=self._template,
                            customer=customer,
                            checked_at=now,
                        )
