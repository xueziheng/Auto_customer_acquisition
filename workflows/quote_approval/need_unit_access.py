"""客户单位用途当前授权：组合两个域的公共矩阵，不复用客户文件权限。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from domains.costing.service import require_pricing_source_access
from domains.demand.service import (
    NeedUnitAccess,
    NeedUnitAction,
    NeedUnitError,
    NeedUnitPermissionError,
    NeedUnitScopeReader,
    NeedUnitUnavailableError,
)
from domains.opportunities.service import (
    Actor,
    OpportunityAction,
    OpportunityAuthorizer,
    OpportunityScope,
    ScopeLevel,
)
from shared.errors import PermissionDenied
from shared.evidence_read import QuoteEvidenceContextReader
from shared.schemas.evidence_read import QuoteEvidenceError
from shared.schemas.identifiers import EmployeeId, TenantId, ValidatedNeedId
from shared.schemas.quote_facts import QuoteEmployeeFact, fact_identity


class CurrentNeedUnitAuthorizer:
    """当前在职C与现机会读取权交集；guard不是可跨请求缓存的票据。"""

    def __init__(
        self,
        contexts: QuoteEvidenceContextReader,
        scopes: NeedUnitScopeReader,
        opportunity_authorizer: OpportunityAuthorizer,
    ) -> None:
        self._contexts, self._scopes, self._opportunities = (
            contexts,
            scopes,
            opportunity_authorizer,
        )

    def _require(
        self, tenant_id: TenantId, actor_id: EmployeeId, fact: QuoteEmployeeFact | None
    ) -> str:
        """对初检和锁内事实应用完全相同的公开权限规则。"""
        if fact is None or (fact.tenant_id, fact.employee_id, fact.is_active) != (
            tenant_id,
            actor_id,
            True,
        ):
            raise NeedUnitPermissionError("permission_denied")
        try:
            require_pricing_source_access(tenant_id, fact)
        except QuoteEvidenceError as exc:
            if exc.code == "permission_denied":
                raise NeedUnitPermissionError("permission_denied") from None
            raise NeedUnitUnavailableError("dependency_unavailable") from None
        scope = OpportunityScope(level=ScopeLevel.TENANT)
        try:
            return self._opportunities.require(
                Actor(str(actor_id), scope, role=fact.role),
                OpportunityAction.OPPORTUNITY_READ,
                scope,
                tenant_id,
            )
        except PermissionDenied:
            raise NeedUnitPermissionError("permission_denied") from None

    async def check(
        self,
        tenant_id: TenantId,
        need_id: ValidatedNeedId,
        actor_id: EmployeeId,
        *,
        action: NeedUnitAction,
    ) -> NeedUnitAccess:
        """完整进入并退出guard，只提供本次即时事实，不声称返回后持锁。"""
        async with self.guard(tenant_id, need_id, actor_id, action=action) as access:
            return access

    @asynccontextmanager
    async def guard(
        self,
        tenant_id: TenantId,
        need_id: ValidatedNeedId,
        actor_id: EmployeeId,
        *,
        action: NeedUnitAction,
    ) -> AsyncIterator[NeedUnitAccess]:
        """来源IO前完成初检；持锁到调用方Need UoW提交，不读取任何原件。"""
        try:
            for identity in (tenant_id, need_id, actor_id):
                fact_identity(identity)
            if action not in {"read", "confirm"}:
                raise ValueError
        except (TypeError, ValueError):
            raise NeedUnitError("invalid_input") from None
        consumer_error: BaseException | None = None
        try:
            current = await self._contexts.read_actor(tenant_id, actor_id)
            self._require(tenant_id, actor_id, current)
            async with self._scopes.open(tenant_id, need_id, actor_id) as facts:
                if (
                    facts.tenant_id,
                    facts.need_id,
                    facts.actor.tenant_id,
                    facts.actor.employee_id,
                ) != (tenant_id, need_id, tenant_id, actor_id):
                    raise NeedUnitError("facts_corrupt")
                rule = self._require(tenant_id, actor_id, facts.actor)
                access = NeedUnitAccess(
                    tenant_id=tenant_id,
                    need_id=need_id,
                    actor_id=actor_id,
                    account_id=facts.account_id,
                    opportunity_id=facts.opportunity_id,
                    authorization_ref=rule,
                )
                try:
                    yield access
                except BaseException as exc:
                    consumer_error = exc
                    raise
        except (NeedUnitError, NeedUnitPermissionError, NeedUnitUnavailableError):
            raise
        except Exception as exc:
            if exc is consumer_error:
                raise
            raise NeedUnitUnavailableError("dependency_unavailable") from None
