"""新报价版本服务门面；旧骨架create/布尔审批/mark_sent从未转调此处。"""

from collections.abc import Callable
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Literal
from pydantic import TypeAdapter, ValidationError as SchemaError

from domains.quotations.errors import (
    QuotationError,
    QuotationPermissionError,
    QuotationUnavailableError,
)
from domains.quotations.permissions import QuotePreparationPolicy
from domains.quotations.schemas import (
    QuotationActor,
    QuoteDetailView,
    QuoteIssuer,
    QuoteIssuerCreate,
    StoredQuoteIssuer,
)
from domains.quotations.schemas import QuoteBusinessContext, QuoteBasis
from domains.quotations.creation import CreationSessionImpl
from domains.quotations.service import QuotationActorReader, QuoteSendReceiptReader
from domains.quotations.version_repository import QuotationUowFactory
from shared.errors import PermissionDenied
from shared.schemas.identifiers import (
    EmployeeId,
    OpportunityId,
    QuoteId,
    TenantId,
    new_id,
)
from shared.schemas.quote_facts import QuoteEmployeeFact, fact_utc
from shared.schemas.quote_creation import QuoteKey, canonical_creation_hash
from shared.schemas.quote_creation import QuoteCreationIntent, QuoteCreationCompletion
from shared.schemas.provenance import Provenance, SourceType


class QuotationServiceImpl:
    """全部依赖显式注入；当前身份不从旧展示名或HTTP标志取得。"""

    def __init__(
        self,
        uow_factory: QuotationUowFactory,
        actor_reader: QuotationActorReader,
        preparation_policy: QuotePreparationPolicy,
        send_reader: QuoteSendReceiptReader,
        *,
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
        except Exception:
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
            values = dict(
                issuer_id=issuer_id,
                name=command.name,
                address=command.address,
                contact=command.contact,
                source_ref=issuer_id,
                confirmed_by=actor.employee_id,
                confirmed_at=now,
                field_provenance=provenance,
            )
            issuer = QuoteIssuer(
                **values,
                content_hash=canonical_creation_hash(
                    {"version": "quote-issuer-v1", "issuer": values}
                ),
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
