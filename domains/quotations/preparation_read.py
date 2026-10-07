"""用途独立的准备读取，缺项不是故障兜底，更不是正式报价许可。"""

from collections.abc import Callable
from datetime import datetime
from typing import Literal, Protocol

from pydantic import ValidationError

from domains.quotations.context import (
    QuoteBusinessContext,
    QuoteContextProvider,
    canonical_quote_specification,
    quote_specification,
    quote_specification_hash,
)
from domains.quotations.errors import QuoteContextError
from domains.quotations.http_projection import project_issuer, project_need
from domains.quotations.http_schemas import (
    QuotePreparationBlocker,
    QuotePreparationPublicView,
)
from domains.quotations.permissions import QuotePreparationPolicy
from shared.schemas.identifiers import EmployeeId, OpportunityId, TenantId
from shared.schemas.quote_facts import (
    NeedQuoteFacts,
    NeedQuotePreparationAssessment,
    canonical_fact_hash,
)


class QuoteNeedPreparationProjector(Protocol):
    """由上层注入需求域唯一分类，不在报价域复制其业务规则。"""

    def project(self, facts: NeedQuoteFacts) -> NeedQuotePreparationAssessment:
        """返回精确绑定本次事实的只读评估。"""
        ...


class QuotePreparationReadService(Protocol):
    """内部四成本角色的只读准备摘要，不接受客户端起草人。"""

    async def get(
        self,
        tenant_id: TenantId,
        opportunity_id: OpportunityId,
        *,
        actor_id: EmployeeId,
    ) -> QuotePreparationPublicView:
        """在同一租约内授权、评估和投影，不产生持久写或原件读取。"""
        ...


class QuotePreparationReadServiceImpl:
    """依赖均显式注入，原完整context只在同lease内以原契约构造。"""

    def __init__(
        self,
        context_provider: QuoteContextProvider,
        policy: QuotePreparationPolicy,
        need_projector: QuoteNeedPreparationProjector,
        now: Callable[[], datetime],
    ) -> None:
        self._contexts, self._policy, self._needs, self._now = (
            context_provider,
            policy,
            need_projector,
            now,
        )

    async def get(
        self,
        tenant_id: TenantId,
        opportunity_id: OpportunityId,
        *,
        actor_id: EmployeeId,
    ) -> QuotePreparationPublicView:
        """正常缺项按固定顺序呈现；错误typed事实始终失败关闭。"""
        async with self._contexts.open_preparation_facts(
            tenant_id, opportunity_id, actor_id, prepared_by=actor_id
        ) as f:
            self._policy.require(tenant_id, f.runtime.current_actor, action="prepare")
            if (
                f.tenant_id,
                f.opportunity_id,
                f.prepared_by,
                f.runtime.current_actor.employee_id,
                f.need_facts.tenant_id,
                f.need_facts.account_id,
            ) != (
                tenant_id,
                opportunity_id,
                actor_id,
                actor_id,
                tenant_id,
                f.account_id,
            ):
                raise QuoteContextError("facts_corrupt")
            a = self._needs.project(f.need_facts)
            try:
                digest = canonical_fact_hash(
                    {"version": "need-quote-facts-v1", "facts": f.need_facts}
                )
                spec = quote_specification(f.need_facts)
            except (ValueError, TypeError, AttributeError, ValidationError):
                raise QuoteContextError("facts_corrupt") from None
            if (a.tenant_id, a.need_id, a.need_facts_hash) != (
                tenant_id,
                f.need_facts.need_id,
                digest,
            ):
                raise QuoteContextError("facts_corrupt")
            blockers: list[QuotePreparationBlocker] = []
            quantity_codes: dict[
                str, Literal["facts_missing", "quantity_invalid", "fact_unconfirmed"]
            ] = {
                "missing": "facts_missing",
                "non_positive": "quantity_invalid",
                "unconfirmed": "fact_unconfirmed",
            }
            unit_codes: dict[
                str, Literal["unit_missing", "fact_unconfirmed", "unit_stale"]
            ] = {
                "missing": "unit_missing",
                "unconfirmed": "fact_unconfirmed",
                "stale": "unit_stale",
            }
            if a.quantity_status != "current":
                blockers.append(
                    QuotePreparationBlocker(
                        field="quantity",
                        code=quantity_codes[a.quantity_status],
                    )
                )
            elif a.unit_status != "current":
                blockers.append(
                    QuotePreparationBlocker(
                        field="unit",
                        code=unit_codes[a.unit_status],
                    )
                )
            if f.need_facts.destination is None:
                blockers.append(
                    QuotePreparationBlocker(field="destination", code="facts_missing")
                )
            if (
                spec.material is None
                and spec.size_spec is None
                and spec.application is None
            ):
                blockers.append(
                    QuotePreparationBlocker(field="specification", code="facts_missing")
                )
            if f.issuer is None:
                blockers.append(
                    QuotePreparationBlocker(field="issuer", code="issuer_missing")
                )
            context_hash = None
            if not blockers:
                facts = f.need_facts
                assert (
                    facts.unit is not None
                    and facts.quantity is not None
                    and facts.destination is not None
                    and f.issuer is not None
                )
                context = QuoteBusinessContext(
                    tenant_id=tenant_id,
                    opportunity_id=opportunity_id,
                    need_id=facts.need_id,
                    account_id=f.account_id,
                    opportunity_state=f.opportunity_state,
                    owner_id=f.owner_id,
                    prepared_by=f.prepared_by,
                    account_name=f.account_name,
                    country=f.country,
                    category=spec.product_category,
                    specification=canonical_quote_specification(spec),
                    unit=facts.unit.value,
                    destination=facts.destination.value,
                    quantity=facts.quantity.value,
                    need_facts=facts,
                    need_facts_hash=digest,
                    specification_hash=quote_specification_hash(spec),
                    issuer=f.issuer,
                    runtime=f.runtime,
                )
                context_hash = context.context_hash
            return QuotePreparationPublicView(
                opportunity_id=opportunity_id,
                account_id=f.account_id,
                owner_id=f.owner_id,
                prepared_by=f.prepared_by,
                account_name=f.account_name,
                country=f.country,
                need=project_need(f.need_facts),
                need_facts_hash=digest,
                specification_hash=quote_specification_hash(spec),
                specification=spec,
                issuer=project_issuer(f.issuer) if f.issuer is not None else None,
                quantity_fact_hash=a.quantity_fact_hash,
                quantity_status=a.quantity_status,
                unit_status=a.unit_status,
                context_hash=context_hash,
                blockers=tuple(blockers),
                checked_at=self._now(),
            )
