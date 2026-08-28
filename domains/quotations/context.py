"""完整报价业务上下文；只读事实和业务hash，不含持久化或客户输出。"""

from __future__ import annotations

import json
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Annotated, Protocol, Self

if TYPE_CHECKING:
    from domains.quotations.approval_schemas import (
        QuoteApprovalAccessContext,
        QuoteApprovalContext,
    )
    from domains.quotations.file_access_schemas import (
        QuoteFileCurrentFacts,
        QuoteFileScopeFacts,
    )

from pydantic import AfterValidator, Field, computed_field, model_validator

from domains.quotations.errors import QuoteContextError
from shared.schemas.identifiers import (
    EmployeeId,
    OpportunityId,
    ProspectAccountId,
    TenantId,
    ValidatedNeedId,
)
from shared.schemas.provenance import Provenance, SourceType
from shared.schemas.quote_creation import QuoteDTO, QuoteText, QuoteTime
from shared.schemas.quote_facts import (
    FactHash,
    NeedQuoteFacts,
    QuoteRuntimeFacts,
    canonical_fact_hash,
    fact_text,
)

_Text = Annotated[str, AfterValidator(fact_text)]


class QuoteIssuer(QuoteDTO):
    """老板确认的抬头事实，完整Provenance不自动对外序列化。"""

    issuer_id: str
    content_hash: FactHash
    name: QuoteText
    address: QuoteText
    contact: QuoteText
    source_ref: QuoteText
    confirmed_by: EmployeeId
    confirmed_at: QuoteTime
    field_provenance: dict[str, Provenance]

    @model_validator(mode="after")
    def _provenance(self) -> Self:
        """不得把老板输入冒称外部来源已核验，也不得省略单字段来源。"""
        if set(self.field_provenance) != {"name", "address", "contact"}:
            raise QuoteContextError("facts_corrupt")
        for provenance in self.field_provenance.values():
            if (provenance.source_type is not SourceType.EMPLOYEE_INPUT
                or provenance.source_id != self.source_ref
                or provenance.confirmed_by != self.confirmed_by
                or provenance.confirmed_at != self.confirmed_at
                or not provenance.is_human_confirmed):
                raise QuoteContextError("facts_corrupt")
        return self


class QuoteSpecificationFacts(QuoteDTO):
    """保留全部规格维度，不把缺失推断为不适用。"""

    product_category: _Text
    application: _Text | None
    material: _Text | None
    size_spec: _Text | None
    packaging: _Text | None
    certification_required: _Text | None


def quote_specification(facts: NeedQuoteFacts) -> QuoteSpecificationFacts:
    """逐字段确定性投影，不以机会摘要覆盖客户事实。"""
    return QuoteSpecificationFacts(
        product_category=facts.product_category.value,
        application=facts.application.value if facts.application is not None else None,
        material=facts.material.value if facts.material is not None else None,
        size_spec=facts.size_spec.value if facts.size_spec is not None else None,
        packaging=facts.packaging.value if facts.packaging is not None else None,
        certification_required=(facts.certification_required.value
            if facts.certification_required is not None else None),
    )


def canonical_quote_specification(spec: QuoteSpecificationFacts) -> str:
    """版本化完整规格JSON仅标识结构，不能用来猜供应商自由文本等价。"""
    return json.dumps({"version": "quote-specification-v1", "specification": spec.model_dump()},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def quote_specification_hash(spec: QuoteSpecificationFacts) -> str:
    """完整规格身份包含material/packaging与显式None。"""
    return canonical_fact_hash({"version": "quote-specification-v1", "specification": spec})


class QuoteBusinessContext(QuoteDTO):
    """可信内部事实；不接受HTTP输入，不直接model_dump到HTTP或日志。"""

    tenant_id: TenantId
    opportunity_id: OpportunityId
    need_id: ValidatedNeedId
    account_id: ProspectAccountId
    opportunity_state: _Text
    owner_id: EmployeeId
    prepared_by: EmployeeId
    account_name: _Text
    country: _Text
    category: _Text
    specification: _Text
    unit: Annotated[str, Field(min_length=1, max_length=64), AfterValidator(fact_text)]
    destination: _Text
    quantity: Annotated[int, Field(gt=0)]
    need_facts: NeedQuoteFacts
    need_facts_hash: FactHash
    specification_hash: FactHash
    issuer: QuoteIssuer
    runtime: QuoteRuntimeFacts

    @model_validator(mode="after")
    def _binding(self) -> Self:
        """所有冗余标量必须来自同一完整事实，单位有效性仍由demand决定。"""
        facts = self.need_facts
        if (facts.tenant_id, facts.need_id, facts.account_id) != (self.tenant_id, self.need_id, self.account_id):
            raise QuoteContextError("facts_corrupt")
        spec = quote_specification(facts)
        if not any((spec.material, spec.size_spec, spec.application)):
            raise QuoteContextError("facts_missing")
        if facts.quantity is None or facts.quantity.value != self.quantity:
            raise QuoteContextError("quantity_mismatch")
        if facts.unit is None:
            raise QuoteContextError("unit_missing")
        if facts.unit.value != self.unit:
            raise QuoteContextError("unit_stale")
        if facts.destination is None or facts.destination.value != self.destination:
            raise QuoteContextError("destination_mismatch")
        if (self.category != spec.product_category
            or self.specification != canonical_quote_specification(spec)
            or self.specification_hash != quote_specification_hash(spec)):
            raise QuoteContextError("specification_mismatch")
        if self.need_facts_hash != canonical_fact_hash({"version": "need-quote-facts-v1", "facts": facts}):
            raise QuoteContextError("facts_corrupt")
        runtime = self.runtime
        if (runtime.owner.employee_id != self.owner_id
            or any(item.tenant_id != self.tenant_id for item in (
                runtime.current_actor, runtime.owner, runtime.preparer) if item is not None)
            or (runtime.preparer is not None and runtime.preparer.employee_id != self.prepared_by)):
            raise QuoteContextError("facts_corrupt")
        return self

    @computed_field  # type: ignore[prop-decorator]  # Pydantic只读computed property
    @property
    def context_hash(self) -> str:
        """只读派生，不允许临时伪hash作为构造参数。"""
        return quote_context_hash(self)


def quote_context_hash(context: QuoteBusinessContext) -> str:
    """只绑定商业事实，不把本次actor/运行时关系/机会正常状态纳入。"""
    fields = ("tenant_id", "opportunity_id", "need_id", "account_id", "owner_id", "prepared_by",
        "account_name", "country", "category", "specification", "unit", "destination", "quantity",
        "need_facts_hash", "specification_hash")
    return canonical_fact_hash({"version": "quote-business-context-v1",
        **{name: getattr(context, name) for name in fields},
        "issuer_id": context.issuer.issuer_id, "issuer_hash": context.issuer.content_hash})


class QuoteContextProvider(Protocol):
    """保持当前员工/机会/Need事实直到调用者事务完成。"""

    def open_file_scope(self, tenant_id: TenantId, opportunity_id: OpportunityId,
        actor_id: EmployeeId) -> AbstractAsyncContextManager[QuoteFileScopeFacts]:
        """仅排序锁actor/owner再锁机会；不读Need/抬头/政策。"""
        ...

    def open_for_file(self, tenant_id: TenantId, opportunity_id: OpportunityId,
        actor_id: EmployeeId, *, prepared_by: EmployeeId, decider_ids: tuple[EmployeeId, ...]
    ) -> AbstractAsyncContextManager[QuoteFileCurrentFacts]:
        """一次锁齐actor/owner/存在的preparer/全部deciders，再机会与Need。"""
        ...

    def open_approval_access(self, tenant_id: TenantId, opportunity_id: OpportunityId,
        actor_id: EmployeeId, *, prepared_by: EmployeeId, submitted_owner_id: EmployeeId
    ) -> AbstractAsyncContextManager[QuoteApprovalAccessContext]:
        """历史读取只锁员工与机会，不读取Need单位/抬头/政策。"""
        ...

    def open_for_approval(self, tenant_id: TenantId, opportunity_id: OpportunityId,
        actor_id: EmployeeId, *, prepared_by: EmployeeId, decider_ids: tuple[EmployeeId, ...]
    ) -> AbstractAsyncContextManager[QuoteApprovalContext]:
        """锁前一次排序全部决策人；机会锁后不追加员工锁。"""
        ...

    def open(self, tenant_id: TenantId, opportunity_id: OpportunityId, actor_id: EmployeeId,
             *, prepared_by: EmployeeId) -> AbstractAsyncContextManager[QuoteBusinessContext]:
        """返回lease，调用者必须在其内提交报价准备结果。"""
        ...


class QuoteIssuerReader(Protocol):
    """读取已确认抬头，不在context锁内读取外部bytes。"""

    async def get_confirmed(self, tenant_id: TenantId) -> QuoteIssuer:
        """缺真实确认必须失败，不能返回样例抬头。"""
        ...
