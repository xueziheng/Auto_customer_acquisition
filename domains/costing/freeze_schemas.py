"""成本适用性与冻结的本域内部契约；含敏感事实，禁止自动HTTP序列化。"""

from __future__ import annotations

from typing import Annotated, Self

from pydantic import AfterValidator, Field, model_validator

from shared.schemas.identifiers import (
    CostSheetId,
    EmployeeId,
    OpportunityId,
    ProspectAccountId,
    TenantId,
    ValidatedNeedId,
)
from shared.schemas.money import FxRate
from shared.schemas.provenance import Provenance
from shared.schemas.quote_creation import (
    QuoteDTO,
    QuoteKey,
    QuoteTerm,
    QuoteText,
    QuoteTime,
)
from shared.schemas.quote_facts import (
    FactHash,
    NeedQuoteFacts,
    QuoteRuntimeFacts,
    fact_identity,
    fact_text,
)


class _FreezeDTO(QuoteDTO):
    """coverage_id沿用T2的64位内容身份，其他记录ID保留现有长度边界。"""

    @model_validator(mode="after")
    def _identities(self) -> Self:
        for name in type(self).model_fields:
            value = getattr(self, name)
            if value is not None and (
                name.endswith("_id") or name in {"prepared_by", "confirmed_by"}
            ):
                if name == "coverage_id":
                    fact_text(value)
                    if len(value) > 64:
                        raise ValueError("完整性清单身份过长")
                else:
                    fact_identity(value)
        return self


class CostingContext(_FreezeDTO):
    """由报价公共context显式投影，不重算另一套context hash。"""

    tenant_id: TenantId
    opportunity_id: OpportunityId
    need_id: ValidatedNeedId
    account_id: ProspectAccountId
    opportunity_state: QuoteText
    owner_id: EmployeeId
    prepared_by: EmployeeId
    category: QuoteText
    specification: QuoteText
    unit: Annotated[str, Field(min_length=1, max_length=64), AfterValidator(fact_text)]
    destination: QuoteText
    quantity: Annotated[int, Field(gt=0)]
    need_facts: NeedQuoteFacts
    need_facts_hash: FactHash
    specification_hash: FactHash
    runtime: QuoteRuntimeFacts
    context_hash: FactHash


class CostScopeEvidenceBinding(_FreezeDTO):
    """人工说明该持久依据如何对应当前完整需求，不冒充供应商原话。"""

    evidence_id: str
    evidence_hash: FactHash
    applicability_note: QuoteText


class CostScopeAccess(_FreezeDTO):
    """仅限可信应用本次调用的来源授权投影，不是持久凭证或布尔许可。"""

    tenant_id: TenantId
    actor_id: EmployeeId
    evidence_bindings: tuple[CostScopeEvidenceBinding, ...]


class CostScopeConfirmationCommand(_FreezeDTO):
    """人工确认完整Need、条款、期限与每一项成本来源映射。"""

    coverage_id: str
    expected_sheet_hash: FactHash
    expected_coverage_hash: FactHash
    expected_need_facts_hash: FactHash
    terms: tuple[QuoteTerm, ...]
    valid_until: QuoteTime
    evidence_bindings: tuple[CostScopeEvidenceBinding, ...]


class CostScopeConfirmationView(_FreezeDTO):
    """不可改的完整人工适用性确认；旧scope不会因freeze自动重新确认。"""

    tenant_id: TenantId
    confirmation_id: str
    opportunity_id: OpportunityId
    need_id: ValidatedNeedId
    cost_sheet_id: CostSheetId
    sheet_hash: FactHash
    coverage_id: str
    coverage_hash: FactHash
    need_facts: NeedQuoteFacts
    need_facts_hash: FactHash
    specification: QuoteText
    specification_hash: FactHash
    terms: tuple[QuoteTerm, ...]
    terms_hash: FactHash
    valid_until: QuoteTime
    evidence_bindings: tuple[CostScopeEvidenceBinding, ...]
    content_hash: FactHash
    provenance: Provenance


class StoredCostScope(_FreezeDTO):
    """scope与原始幂等请求身份的内部存储记录。"""

    view: CostScopeConfirmationView
    idempotency_key: QuoteKey
    request_hash: FactHash


class FrozenCostBasis(_FreezeDTO):
    """完整成本/需求/适用性快照，不允许裁掉原始供应商specification。"""

    tenant_id: TenantId
    basis_id: str
    operation_id: str
    request_hash: FactHash
    opportunity_id: OpportunityId
    cost_sheet_id: CostSheetId
    context_hash: FactHash
    sheet_hash: FactHash
    basis_hash: FactHash
    policy_id: str
    quantity: Annotated[int, Field(gt=0)]
    specification: QuoteText
    unit: Annotated[str, Field(min_length=1, max_length=64), AfterValidator(fact_text)]
    destination: QuoteText
    need_facts: NeedQuoteFacts
    scope_confirmation: CostScopeConfirmationView
    policy: PricingPolicyView
    coverage: CostCoverageView
    calculation: CalculationSnapshot
    price_evidence: tuple[PriceEvidenceView, ...]
    pricing_options: PricingOptions
    cost_fx_rates: tuple[FxRate, ...]
    quote_fx: QuoteFxView | None
    valid_until: QuoteTime
    frozen_at: QuoteTime


# 延后解析已有DTO以保持schemas显式重导出及直接导入本模块都无循环。
from domains.costing.schemas import (
    CalculationSnapshot,
    CostCoverageView,
    PriceEvidenceView,
    PricingOptions,
    PricingPolicyView,
    QuoteFxView,
)

FrozenCostBasis.model_rebuild()
