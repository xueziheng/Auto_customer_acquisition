"""唯一报价创建/CAS规则；已持外层lease与报价机会锁，不自行另开事务。"""
from collections.abc import Awaitable, Callable
from copy import deepcopy
from datetime import datetime

from domains.quotations.content import build_quote_content, validate_quote_basis, validate_terms
from domains.quotations.context import QuoteBusinessContext
from domains.quotations.errors import QuotationError, QuotationPermissionError
from domains.quotations.schemas import QuoteBasis, QuoteDetailView, QuoteState, QuoteStateEvent, QuotationActor
from domains.quotations.version_repository import QuotationVersionRepository
from shared.schemas.identifiers import OpportunityId, QuoteId, TenantId, new_id
from shared.schemas.quote_creation import QuoteCreationIntent, quote_creation_request_hash
from shared.schemas.quote_facts import QuoteEmployeeFact, fact_utc

ACTIVE_STATES=frozenset({QuoteState.DRAFT,QuoteState.PENDING_APPROVAL,QuoteState.APPROVED,QuoteState.SENT})


class CreationSessionImpl:
    """同一session记住完整预检载荷；不接受无预检或修改后basis写入。"""
    def __init__(self, tenant_id: TenantId, opportunity_id: OpportunityId, context: QuoteBusinessContext,
        actor: QuotationActor, repository: QuotationVersionRepository,
        current_actor: Callable[[],Awaitable[QuoteEmployeeFact]], *, now: Callable[[],datetime]) -> None:
        """仅服务持锁后构造；绑定事实防御拷贝，业务时钟显式注入。"""
        self.tenant_id,self.opportunity_id=tenant_id,opportunity_id
        self.context,self.actor=deepcopy(context),actor
        self._repo,self._current,self._now=repository,current_actor,now
        self._checked: QuoteCreationIntent | None=None
        self._operation_id: str | None=None
        self._version: int | None=None
        self._previous: QuoteDetailView | None=None
        self._closed=False

    def close(self) -> None:
        """上下文退出后禁止持有旧session重新打开SQL会话写入。"""
        self._closed=True

    async def _fresh(self, intent: QuoteCreationIntent) -> datetime:
        """锁后读取当前身份/时钟，完整上下文不能由客户自证。"""
        fact=await self._current()
        c=self.context
        if fact!=c.runtime.current_actor:
            raise QuotationPermissionError("permission_denied")
        if intent.prepared_by!=self.actor.employee_id:
            raise QuotationPermissionError("permission_denied")
        if (intent.tenant_id,intent.opportunity_id,intent.prepared_by,intent.expected_context_hash)!=(
            self.tenant_id,self.opportunity_id,c.prepared_by,c.context_hash) or (
                c.tenant_id,c.opportunity_id)!=(self.tenant_id,self.opportunity_id):
            raise QuotationError("context_changed")
        issuer=await self._repo.get_issuer(self.tenant_id,c.issuer.issuer_id)
        if issuer is None or issuer!=c.issuer:
            raise QuotationError("context_changed")
        validate_terms(intent)
        now=fact_utc(self._now())
        if intent.valid_until<=now:
            raise QuotationError("quote_expired")
        return now

    async def _historical(self, intent: QuoteCreationIntent, operation_id: str | None) -> QuoteDetailView | None:
        """已持久报价优先恢复，不再要求当前内容有效期或latest状态。"""
        if operation_id is None:
            return None
        quote=await self._repo.get_by_operation(self.tenant_id,operation_id)
        if quote and (quote.content.intent!=intent or quote.content.request_hash!=quote_creation_request_hash(intent)
            or quote.content.opportunity_id!=self.opportunity_id or quote.content.operation_id!=operation_id):
            raise QuotationError("idempotency_conflict")
        return quote

    async def _revision(self, intent: QuoteCreationIntent) -> tuple[int,QuoteDetailView | None]:
        """E2仅允许latest expired；终态新事实可开下一版，不修改accepted/rejected。"""
        versions=await self._repo.list_versions(self.tenant_id,self.opportunity_id)
        latest=versions[0] if versions else None
        active=next((q for q in versions if q.state in ACTIVE_STATES),None)
        if intent.replaces_quote_id is None:
            if active:
                raise QuotationError("active_quote_exists")
            if latest and latest.state==QuoteState.EXPIRED:
                raise QuotationError("revision_conflict")
            if latest and latest.state in {QuoteState.ACCEPTED,QuoteState.REJECTED} and any(
                old.content.basis.cost_sheet_id==intent.cost_sheet_id or
                old.content.intent.scope_confirmation_id==intent.scope_confirmation_id for old in versions):
                raise QuotationError("revision_conflict")
            return (latest.content.version+1 if latest else 1),None
        if (latest is None or latest.content.quote_id!=intent.replaces_quote_id
            or latest.content.version!=intent.expected_quote_version
            or not (latest==active or active is None and latest.state==QuoteState.EXPIRED)):
            raise QuotationError("revision_conflict")
        return latest.content.version+1,latest

    async def preflight(self, intent: QuoteCreationIntent, *, operation_id: str | None) -> QuoteDetailView | None:
        """成本freeze之前先持报价机会锁检CAS；只把同载荷存于本session。"""
        if self._closed:
            raise QuotationError("invalid_state")
        historical=await self._historical(intent,operation_id)
        if historical:
            return historical
        await self._fresh(intent)
        version,previous=await self._revision(intent)
        self._checked,self._operation_id=deepcopy(intent),operation_id
        self._version,self._previous=version,previous
        return None

    async def create_from_basis(self, intent: QuoteCreationIntent, basis: QuoteBasis, *, operation_id: str) -> QuoteDetailView:
        """共享唯一预检/第二道门/CAS写入，不再取锁或另开服务session。"""
        if self._closed:
            raise QuotationError("invalid_state")
        if self._checked is None:
            raise QuotationError("invalid_input")
        if self._checked!=intent or self._operation_id not in {None,operation_id}:
            raise QuotationError("idempotency_conflict")
        if operation_id!=basis.operation_id:
            raise QuotationError("basis_mismatch")
        historical=await self._historical(intent,operation_id)
        if historical:
            if historical.content.basis!=basis:
                raise QuotationError("basis_mismatch")
            return historical
        now=await self._fresh(intent)
        version,previous=await self._revision(intent)
        if version!=self._version or previous!=self._previous:
            raise QuotationError("revision_conflict")
        validate_quote_basis(intent,basis,self.context,now=now)
        quote_id=QuoteId(new_id("quo"))
        if previous and previous.state in ACTIVE_STATES:
            target=QuoteState.EXPIRED if now>=previous.content.valid_until else QuoteState.SUPERSEDED
            event=QuoteStateEvent(event_id=new_id("qev"),quote_id=previous.content.quote_id,
                from_state=previous.state,to_state=target,actor_id=self.actor.employee_id,reason="revision",at=now,
                reference_id=quote_id)
            if not await self._repo.transition(self.tenant_id,previous.content.quote_id,previous.state,target,event):
                raise QuotationError("revision_conflict")
        content=build_quote_content(quote_id,version,intent,basis,self.context,created_at=now,
            replaced_quote_version=previous.content.version if previous else None)
        detail=QuoteDetailView(content=content,state=QuoteState.DRAFT)
        await self._repo.add(self.tenant_id,detail)
        return detail
