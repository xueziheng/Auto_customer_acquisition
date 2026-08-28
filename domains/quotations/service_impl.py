"""新报价版本服务门面；旧骨架create/布尔审批/mark_sent从未转调此处。"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Literal

from pydantic import TypeAdapter
from pydantic import ValidationError as SchemaError

from domains.quotations.approval_schemas import (
    QuoteApprovalAccessResult,
    QuoteApprovalApplicationReceipt,
    QuoteApprovalSnapshot,
    QuoteApprovalSubject,
    QuoteWorkflowExecutor,
)
from domains.quotations.approval_service import QuoteApprovalServiceImpl
from domains.quotations.creation import ACTIVE_STATES, CreationSessionImpl
from domains.quotations.errors import (
    QuotationError,
    QuotationPermissionError,
    QuotationUnavailableError,
)
from domains.quotations.permissions import QuotePreparationPolicy
from domains.quotations.schemas import (
    QuotationActor,
    QuoteBasis,
    QuoteBusinessContext,
    QuoteDetailView,
    QuoteIssuer,
    QuoteIssuerCreate,
    QuoteSendReceipt,
    QuoteState,
    QuoteStateEvent,
    StoredQuoteIssuer,
)
from domains.quotations.service import (
    QuotationActorReader,
    QuoteApprovalPolicyReader,
    QuoteApprovalSession,
    QuoteContextProvider,
    QuoteSendReceiptReader,
    QuoteWorkflowRunReader,
)
from domains.quotations.version_repository import QuotationUowFactory
from shared.errors import PermissionDenied
from shared.schemas.identifiers import (
    EmployeeId,
    OpportunityId,
    QuoteId,
    TenantId,
    new_id,
)
from shared.schemas.provenance import Provenance, SourceType
from shared.schemas.quote_creation import (
    QuoteCreationCompletion,
    QuoteCreationIntent,
    QuoteKey,
    canonical_creation_hash,
)
from shared.schemas.quote_facts import QuoteEmployeeFact, fact_utc


class QuotationServiceImpl:
    """全部依赖显式注入；当前身份不从旧展示名或HTTP标志取得。"""

    def __init__(
        self,
        uow_factory: QuotationUowFactory,
        actor_reader: QuotationActorReader,
        preparation_policy: QuotePreparationPolicy,
        send_reader: QuoteSendReceiptReader,
        *,
        context_provider: QuoteContextProvider,
        approval_policy_reader: QuoteApprovalPolicyReader,
        workflow_run_reader: QuoteWorkflowRunReader,
        now: Callable[[], datetime],
    ) -> None:
        """不提供缺省角色、发送许可或业务时钟。"""
        self._uows, self._actors, self._policy, self._send_reader, self._now = (
            uow_factory,
            actor_reader,
            preparation_policy,
            send_reader,
            now,
        )
        self._approvals = QuoteApprovalServiceImpl(uow_factory, context_provider,
            approval_policy_reader, workflow_run_reader, self._actor, now=now)

    async def approval_snapshot(self, tenant_id: TenantId, quote_id: QuoteId, *, actor: QuotationActor) -> QuoteApprovalSnapshot:
        """内部用途当前授权后生成安全审批快照。"""
        return await self._approvals.approval_snapshot(tenant_id,quote_id,actor=actor)

    async def approval_target(self, tenant_id: TenantId, quote_id: QuoteId, *, executor: QuoteWorkflowExecutor) -> QuoteApprovalSnapshot:
        """受信worker通过统一真实run绑定检查。"""
        return await self._approvals.approval_target(tenant_id,quote_id,executor=executor)

    @asynccontextmanager
    async def open_approval_access(self, tenant_id: TenantId, subject: QuoteApprovalSubject, *, actor_id: EmployeeId,
        action: Literal["read","decide"]) -> AsyncIterator[QuoteApprovalAccessResult]:
        """只委托唯一审批业务规则，保持guard到调用者提交。"""
        async with self._approvals.open_approval_access(tenant_id,subject,actor_id=actor_id,action=action) as result:
            yield result

    @asynccontextmanager
    async def open_approval(self, tenant_id: TenantId, quote_id: QuoteId, *, executor: QuoteWorkflowExecutor) -> AsyncIterator[QuoteApprovalSession]:
        """原子报价应用session，不恢复旧单包布尔入口。"""
        async with self._approvals.open_approval(tenant_id,quote_id,executor=executor) as session:
            yield session

    async def get_approval_application(self, tenant_id: TenantId, quote_id: QuoteId,
        *, executor: QuoteWorkflowExecutor) -> QuoteApprovalApplicationReceipt | None:
        """只查询真实持久成功，不借当前角色或状态伪造回执。"""
        return await self._approvals.get_approval_application(tenant_id,quote_id,executor=executor)

    async def _actor(
        self,
        tenant_id: TenantId,
        actor: QuotationActor,
        *,
        action: Literal["prepare", "read_internal"],
    ) -> QuoteEmployeeFact:
        """每次入口和锁后重读tenant/在职/role，并与调用方声明严格相合。"""
        try:
            fact = await self._actors.read_current(tenant_id, actor.employee_id)
        except Exception:  # noqa: BLE001 -- 身份依赖异常只输出固定错误，不能泄露底层数据
            raise QuotationUnavailableError("dependency_unavailable") from None
        if (
            fact is None
            or fact.employee_id != actor.employee_id
            or fact.role != actor.role
            or fact.tenant_id != tenant_id
            or not fact.is_active
        ):
            raise QuotationPermissionError("permission_denied")
        try:
            self._policy.require(tenant_id, fact, action=action)
        except PermissionDenied:
            raise QuotationPermissionError("permission_denied") from None
        return fact

    @asynccontextmanager
    async def open_creation(
        self,
        tenant_id: TenantId,
        opportunity_id: OpportunityId,
        context: QuoteBusinessContext,
        *,
        actor: QuotationActor,
    ) -> AsyncIterator[CreationSessionImpl]:
        """调用者须保持context lease；报价机会锁跨freeze持至显式commit成功。"""
        await self._actor(tenant_id, actor, action="prepare")
        async with self._uows(tenant_id) as uow:
            await uow.quotes.lock_opportunity(tenant_id, opportunity_id)
            fact = await self._actor(tenant_id, actor, action="prepare")
            if fact != context.runtime.current_actor:
                raise QuotationPermissionError("permission_denied")
            session = CreationSessionImpl(
                tenant_id,
                opportunity_id,
                context,
                actor,
                uow.quotes,
                lambda: self._actor(tenant_id, actor, action="prepare"),
                now=self._now,
            )
            try:
                yield session
                await uow.commit()
            finally:
                session.close()

    async def create_from_basis(
        self,
        tenant_id: TenantId,
        intent: QuoteCreationIntent,
        basis: QuoteBasis,
        context: QuoteBusinessContext,
        *,
        operation_id: str,
        actor: QuotationActor,
    ) -> QuoteDetailView:
        """便利入口仅委托同一session预检/创建，无第二套规则。"""
        async with self.open_creation(
            tenant_id, intent.opportunity_id, context, actor=actor
        ) as session:
            existing = await session.preflight(intent, operation_id=operation_id)
            if existing:
                return existing
            return await session.create_from_basis(
                intent, basis, operation_id=operation_id
            )

    async def get(
        self, tenant_id: TenantId, quote_id: QuoteId, *, actor: QuotationActor
    ) -> QuoteDetailView:
        """先授权再判断存在性，历史读取不依赖最新上下文或当前期限。"""
        await self._actor(tenant_id, actor, action="read_internal")
        async with self._uows(tenant_id) as uow:
            value = await uow.quotes.get(tenant_id, quote_id)
        if value is None:
            raise QuotationError("quote_not_found")
        return value

    async def list_versions(
        self,
        tenant_id: TenantId,
        opportunity_id: OpportunityId,
        *,
        actor: QuotationActor,
    ) -> tuple[QuoteDetailView, ...]:
        """内部授权后按存储版本降序返回，包括过期历史。"""
        await self._actor(tenant_id, actor, action="read_internal")
        async with self._uows(tenant_id) as uow:
            return await uow.quotes.list_versions(tenant_id, opportunity_id)

    async def get_by_operation(
        self, tenant_id: TenantId, operation_id: str, *, actor: QuotationActor
    ) -> QuoteDetailView | None:
        """内部授权后只返回真实报价，不因operation存在而构造成功结果。"""
        await self._actor(tenant_id, actor, action="read_internal")
        async with self._uows(tenant_id) as uow:
            return await uow.quotes.get_by_operation(tenant_id, operation_id)

    async def creation_completion(
        self, tenant_id: TenantId, operation_id: str, *, actor: QuotationActor
    ) -> QuoteCreationCompletion | None:
        """完成事实只来自当前授权下读出的真实不可变报价，不接受调用方成功标志。"""
        quote = await self.get_by_operation(tenant_id, operation_id, actor=actor)
        if quote is None:
            return None
        c = quote.content
        return QuoteCreationCompletion(
            tenant_id=c.tenant_id,
            operation_id=c.operation_id,
            request_hash=c.request_hash,
            basis_id=c.basis.basis_id,
            quote_id=c.quote_id,
            quote_version=c.version,
            quote_content_hash=c.content_hash,
            replaces_quote_id=c.replaces_quote_id,
            replaced_quote_version=c.replaced_quote_version,
        )

    async def get_confirmed_issuer(self, tenant_id: TenantId) -> QuoteIssuer:
        """仅可信内部context reader使用，不装配为无鉴权HTTP。"""
        async with self._uows(tenant_id) as uow:
            issuer = await uow.quotes.current_issuer(tenant_id)
        if issuer is None:
            raise QuotationError("issuer_not_found")
        return issuer

    async def expire_overdue(self, tenant_id: TenantId, *, limit: int) -> int:
        """先读候选机会，逐机会锁后重读时钟/状态；不先锁报价行而反转锁序。"""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise QuotationError("invalid_input")
        async with self._uows(tenant_id) as uow:
            opportunities = await uow.quotes.overdue_opportunities(
                tenant_id, now=fact_utc(self._now()), limit=limit
            )
        changed = 0
        for opportunity_id in opportunities:
            async with self._uows(tenant_id) as uow:
                await uow.quotes.lock_opportunity(tenant_id, opportunity_id)
                now = fact_utc(self._now())
                for quote in await uow.quotes.list_versions(tenant_id, opportunity_id):
                    if (
                        quote.state not in ACTIVE_STATES
                        or quote.content.valid_until > now
                    ):
                        continue
                    event = QuoteStateEvent(
                        event_id=new_id("qse"),
                        quote_id=quote.content.quote_id,
                        from_state=quote.state,
                        to_state=QuoteState.EXPIRED,
                        actor_id=None,
                        reason="expiry",
                        at=now,
                        reference_id=None,
                    )
                    if not await uow.quotes.transition(
                        tenant_id,
                        quote.content.quote_id,
                        quote.state,
                        QuoteState.EXPIRED,
                        event,
                    ):
                        raise QuotationError("invalid_state")
                    changed += 1
                await uow.commit()
        return changed

    async def record_verified_send(
        self,
        tenant_id: TenantId,
        quote_id: QuoteId,
        receipt: QuoteSendReceipt,
        *,
        actor: QuotationActor,
    ) -> QuoteDetailView:
        """可信reader核实实际发送，锁后只将未过期approved与回执/事件原子记为sent。"""
        quote = await self.get(tenant_id, quote_id, actor=actor)
        try:
            receipt = QuoteSendReceipt(
                **{n: getattr(receipt, n) for n in QuoteSendReceipt.model_fields}
            )
        except (SchemaError, ValueError, TypeError, AttributeError):
            raise QuotationError("receipt_invalid") from None
        try:
            actual = await self._send_reader.read(
                tenant_id, receipt.attempt_id, actor_id=actor.employee_id
            )
        except Exception:  # noqa: BLE001 -- 发送reader异常不能透传原文或被当成验证成功
            raise QuotationUnavailableError("dependency_unavailable") from None
        now = fact_utc(self._now())
        if (
            actual != receipt
            or receipt.tenant_id != tenant_id
            or receipt.quote_id != quote_id
            or receipt.content_hash != quote.content.content_hash
            or not quote.content.created_at <= receipt.sent_at <= now
        ):
            raise QuotationError("receipt_invalid")
        async with self._uows(tenant_id) as uow:
            await uow.quotes.lock_opportunity(tenant_id, quote.content.opportunity_id)
            await self._actor(tenant_id, actor, action="read_internal")
            current = await uow.quotes.get(tenant_id, quote_id, for_update=True)
            now = fact_utc(self._now())
            if (
                current is None
                or current.content != quote.content
                or not current.content.created_at <= receipt.sent_at <= now
            ):
                raise QuotationError("receipt_invalid")
            existing = await uow.quotes.send_receipt(tenant_id, receipt.attempt_id)
            if existing is not None:
                if existing != receipt:
                    raise QuotationError("idempotency_conflict")
                return current
            if current.content.valid_until <= now:
                raise QuotationError("quote_expired")
            if current.state != QuoteState.APPROVED:
                raise QuotationError("approval_missing")
            await uow.quotes.add_send_receipt(tenant_id, receipt)
            event = QuoteStateEvent(
                event_id=new_id("qse"),
                quote_id=quote_id,
                from_state=QuoteState.APPROVED,
                to_state=QuoteState.SENT,
                actor_id=actor.employee_id,
                reason="verified_send",
                at=now,
                reference_id=receipt.attempt_id,
            )
            if not await uow.quotes.transition(
                tenant_id, quote_id, QuoteState.APPROVED, QuoteState.SENT, event
            ):
                raise QuotationError("invalid_state")
            await uow.commit()
            return current.model_copy(update={"state": QuoteState.SENT})

    async def confirm_issuer(
        self,
        tenant_id: TenantId,
        command: QuoteIssuerCreate,
        *,
        actor: QuotationActor,
        idempotency_key: str,
    ) -> QuoteIssuer:
        """当前老板在租户issuer锁内逐字段确认，不声称外部原件已核验。"""
        await self._actor(tenant_id, actor, action="prepare")
        if actor.role != "boss":
            raise QuotationPermissionError("permission_denied")
        try:
            TypeAdapter(QuoteKey).validate_python(idempotency_key, strict=True)
            command = QuoteIssuerCreate.model_validate(
                command.model_dump(mode="python")
            )
            digest = canonical_creation_hash(
                {
                    "version": "quote-issuer-request-v1",
                    "tenant_id": tenant_id,
                    "employee_id": actor.employee_id,
                    "command": command,
                }
            )
        except (ValueError, TypeError, SchemaError):
            raise QuotationError("invalid_input") from None
        async with self._uows(tenant_id) as uow:
            await uow.quotes.lock_issuer(tenant_id)
            await self._actor(tenant_id, actor, action="prepare")
            stored = await uow.quotes.issuer_by_key(tenant_id, idempotency_key)
            if stored:
                if stored.request_hash != digest:
                    raise QuotationError("idempotency_conflict")
                return stored.issuer
            latest = await uow.quotes.current_issuer_record(tenant_id)
            now = fact_utc(self._now())
            issuer_id = new_id("qis")
            provenance = {
                name: Provenance(
                    source_type=SourceType.EMPLOYEE_INPUT,
                    source_id=issuer_id,
                    extracted_by=actor.employee_id,
                    extracted_at=now,
                    confirmed_by=actor.employee_id,
                    confirmed_at=now,
                    source_url=None,
                    page_hash=None,
                    source_quote=getattr(command, name),
                )
                for name in ("name", "address", "contact")
            }
            values = {
                "issuer_id": issuer_id,
                "name": command.name,
                "address": command.address,
                "contact": command.contact,
                "source_ref": issuer_id,
                "confirmed_by": actor.employee_id,
                "confirmed_at": now,
                "field_provenance": provenance,
            }
            issuer = QuoteIssuer.model_validate(
                values
                | {
                    "content_hash": canonical_creation_hash(
                        {"version": "quote-issuer-v1", "issuer": values}
                    )
                },
            )
            await uow.quotes.add_issuer(
                tenant_id,
                StoredQuoteIssuer(
                    issuer=issuer,
                    version=latest.version + 1 if latest else 1,
                    idempotency_key=idempotency_key,
                    request_hash=digest,
                ),
            )
            await uow.commit()
            return issuer
