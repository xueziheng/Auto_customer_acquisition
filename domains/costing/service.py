"""成本域服务 —— **本域的公共 API**。"""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
from typing import Protocol, runtime_checkable

from domains.costing.approval_policy import (
    CostingApprovalPolicyReader,
    CostingPolicySelection,
)
from domains.costing.calculation import canonical_pricing_hash, compute_breakdown
from domains.costing.errors import (
    CostingQuoteNotFoundError,
    CostSheetNotFoundError,
    EmptyCostSheetError,
    MissingFxSnapshotError,
)
from domains.costing.freeze_repository import CostingFreezeUowFactory
from domains.costing.freeze_schemas import (
    CostingContext,
    CostScopeAccess,
    CostScopeConfirmationCommand,
    CostScopeConfirmationView,
    CostScopeEvidenceBinding,
    FrozenCostBasis,
)
from domains.costing.http_projection import (
    project_coverage as project_coverage,  # noqa: PLC0414 - 公共纯投影
)
from domains.costing.http_projection import (
    project_policy as project_policy,  # noqa: PLC0414 - 公共纯投影
)
from domains.costing.http_projection import (
    project_price_evidence as project_price_evidence,  # noqa: PLC0414 - 公共纯投影
)
from domains.costing.http_projection import (
    project_quote_fx as project_quote_fx,  # noqa: PLC0414 - 公共纯投影
)
from domains.costing.http_projection import (
    project_scope as project_scope,  # noqa: PLC0414 - 公共纯投影
)
from domains.costing.http_schemas import (
    CostCalculationCommand as CostCalculationCommand,  # noqa: PLC0414 - 同类型公开重导出
)
from domains.costing.http_schemas import (
    CostCoveragePublicView as CostCoveragePublicView,  # noqa: PLC0414 - 同类型公开重导出
)
from domains.costing.http_schemas import (
    CostScopePublicView as CostScopePublicView,  # noqa: PLC0414 - 同类型公开重导出
)
from domains.costing.http_schemas import (
    ExpenseEvidencePublicView as ExpenseEvidencePublicView,  # noqa: PLC0414 - 同类型公开重导出
)
from domains.costing.http_schemas import (
    PriceEvidencePublicView as PriceEvidencePublicView,  # noqa: PLC0414 - 同类型公开重导出
)
from domains.costing.http_schemas import (
    PricingPolicyPublicView as PricingPolicyPublicView,  # noqa: PLC0414 - 同类型公开重导出
)
from domains.costing.http_schemas import (
    PricingSourceSummary as PricingSourceSummary,  # noqa: PLC0414 - 同类型公开重导出
)
from domains.costing.http_schemas import (
    QuoteFxPublicView as QuoteFxPublicView,  # noqa: PLC0414 - 同类型公开重导出
)
from domains.costing.http_schemas import (
    SupplierPriceEvidencePublicView as SupplierPriceEvidencePublicView,  # noqa: PLC0414 - 同类型公开重导出
)
from domains.costing.models import (
    CostItemType,
    CostSheet,
    CostSheetVersion,
)
from domains.costing.permissions import CostingActor, CostingActorReader, CostingScope
from domains.costing.quote_lock import cost_scope_hash
from domains.costing.repository import CostingUnitOfWorkFactory
from domains.costing.schemas import (
    CalculationSnapshot,
    CostCoverageCreate,
    CostCoverageView,
    CostItemCreate,
    CostSheetCreate,
    CostSheetView,
    PriceEvidenceCreate,
    PriceEvidenceView,
    PricingOptions,
    PricingPolicyCreate,
    PricingPolicyView,
    QuoteFxCreate,
    QuoteFxView,
    QuoteReadiness,
    SourceEvidence,
    SourcingEstimateCreate,
)
from domains.costing.source_access import require_pricing_source_access
from shared.schemas.identifiers import (
    CostSheetId,
    EmployeeId,
    OpportunityId,
    TenantId,
)
from shared.schemas.money import CurrencyCode, Money, PriceBasis, convert
from shared.schemas.provenance import FactualField
from shared.schemas.quote_creation import (
    QuoteCreationCompletion,
    QuoteCreationIntent,
    QuoteCreationOperationView,
)
from shared.schemas.quote_facts import NeedQuoteFacts

__all__ = (
    "CostScopeAccess",
    "CostScopeConfirmationCommand",
    "CostScopeConfirmationView",
    "CostScopeEvidenceBinding",
    "CostScopeSourceAccess",
    "CostSheetNotFoundError",
    "CostingActor",
    "CostingActorReader",
    "CostingApprovalPolicyReader",
    "CostingContext",
    "CostingFreezeService",
    "CostingFreezeUowFactory",
    "CostingPolicySelection",
    "CostingQuoteNotFoundError",
    "CostingQuoteService",
    "CostingScope",
    "CostingService",
    "CostingUnitOfWorkFactory",
    "FrozenCostBasis",
    "NeedFactsValidator",
    "PricingEvidenceReader",
    "QuoteCreationCompletionReader",
    "assess_quote_readiness",
    "compute_breakdown",
    "compute_unit_full_cost",
    "cost_item_type_values",
    "cost_scope_hash",
    "cost_sheet_content_hash",
    "require_pricing_source_access",
)


def cost_sheet_content_hash(sheet: CostSheet) -> str:
    """以真实成本/来源/汇率生成身份；锁定状态和读取时间不改变内容。"""
    payload = asdict(sheet)
    payload.pop("locked_at")
    payload.pop("created_at")
    return canonical_pricing_hash(payload)


def cost_item_type_values() -> tuple[str, ...]:
    """返回成本项公共词表，供上层校验建议而不导入域内部模型。"""
    return tuple(item_type.value for item_type in CostItemType)


def assess_quote_readiness(
    sheet: CostSheet,
    expected_item_types: list[CostItemType] | None = None,
) -> QuoteReadiness:
    """在锁定前一次返回全部可确定的报价阻断原因。

    本函数只检查结构化事实，不执行锁定，也不推断业务场景应有哪些成本
    项。调用方必须显式传入场景清单；参考价风险只有已留痕的人工接受记录
    才能解除阻断。
    """
    confirmed_items = [item for item in sheet.items if item.entered_by is not None]
    blockers: list[str] = []
    if sheet.version_type is not CostSheetVersion.QUOTED:
        blockers.append("只有 QUOTED 版本可以锁定用于客户报价")
    if not confirmed_items:
        blockers.append("成本表没有已确认成本项")
    if not isinstance(sheet.fx_snapshot_id, str) or not sheet.fx_snapshot_id.strip():
        blockers.append("QUOTED 成本表必须绑定汇率快照")

    indicative_items = list(
        dict.fromkeys(
            item.item_type.value
            for item in confirmed_items
            if item.price_basis == PriceBasis.INDICATIVE
        )
    )
    if indicative_items and sheet.risk_acceptance is None:
        blockers.append("含参考价成本项，必须取得供应商实报价或完成人工风险接受")
    unsupported_bases = sorted(
        {
            item.price_basis
            for item in confirmed_items
            if item.price_basis not in {PriceBasis.QUOTED, PriceBasis.INDICATIVE}
        }
    )
    blockers.extend(
        f"含 {basis} 成本基准；客户报价只允许 quoted，indicative 仅可经人工风险接受"
        for basis in unsupported_bases
    )

    available_rates = {rate.base for rate in sheet.fx_rates}
    missing_currencies = sorted(
        {
            item.amount.currency
            for item in confirmed_items
            if item.amount.currency != sheet.base_currency
            and item.amount.currency not in available_rates
        }
    )
    blockers.extend(
        f"汇率快照缺少 {currency} 到 {sheet.base_currency} 的直连汇率"
        for currency in missing_currencies
    )

    missing_items = [
        item_type.value
        for item_type in sheet.missing_item_types(expected_item_types or [])
    ]
    if missing_items:
        blockers.append("成本表缺少业务场景要求的成本项")
    return QuoteReadiness(
        ready=not blockers,
        blockers=blockers,
        indicative_items=indicative_items,
        missing_items=missing_items,
    )


def compute_unit_full_cost(sheet: CostSheet) -> Money:
    """把已确认成本项按锁定汇率快照归一为单位完整成本。

    未经人工确认的模型建议不参与任何金额计算；整单成本按成本表数量
    分摊。跨币种只接受快照中的显式直连汇率，不自动取倒数或拼接汇率，
    以免隐藏历史报价实际使用的换算路径。
    """
    confirmed_items = [item for item in sheet.items if item.entered_by is not None]
    if not confirmed_items:
        raise EmptyCostSheetError("成本表没有已确认成本项，不能计算单位成本")

    rates = {rate.base: rate for rate in sheet.fx_rates}
    total = Money(Decimal(0), CurrencyCode(sheet.base_currency))
    for item in confirmed_items:
        amount = item.amount
        if amount.currency != sheet.base_currency:
            rate = rates.get(amount.currency)
            if rate is None:
                raise MissingFxSnapshotError(
                    f"汇率快照缺少 {amount.currency} 到 {sheet.base_currency} 的直连汇率"
                )
            amount = convert(amount, CurrencyCode(sheet.base_currency), rate)
        if not item.is_per_unit:
            amount = amount.multiply(Decimal(1) / Decimal(sheet.quantity))
        total = total.add(amount)
    return total


@runtime_checkable
class CostingService(Protocol):
    """成本服务。"""

    async def create_sheet(
        self,
        tenant_id: TenantId,
        opportunity_id: OpportunityId,
        command: CostSheetCreate,
        *,
        actor: CostingActor,
    ) -> CostSheetId:
        """创建成本表。

        QUOTED 类型必须同时锁定汇率快照（调 connectors/fx 的结果
        由上层传入）。
        """
        ...

    async def create_sourcing_estimate(
        self,
        tenant_id: TenantId,
        command: SourcingEstimateCreate,
        *,
        actor: CostingActor,
    ) -> CostSheetId:
        """从可信主供给快照幂等创建唯一 ESTIMATED 表和采购成本项。"""
        ...

    async def add_item(
        self,
        tenant_id: TenantId,
        cost_sheet_id: CostSheetId,
        command: CostItemCreate,
        *,
        actor: CostingActor,
    ) -> None:
        """添加成本项。

        实现要求：
        - 已锁定（``locked_at`` 非 None）的表拒绝修改
        - 模型建议的成本项（``entered_by`` 为 None）只能进待确认区，
          由人确认后才成为正式项——模型不产出参与计算的数字
        """
        ...

    async def assess_for_quote(
        self,
        tenant_id: TenantId,
        cost_sheet_id: CostSheetId,
        expected_item_types: tuple[str, ...],
        *,
        actor: CostingActor,
    ) -> QuoteReadiness:
        """只读检查可报价性，不执行锁定或风险接受。

        锁定与参考价风险接受必须接入审批事实后才能公开，不能把只读检查
        伪装成已获准报价。
        """
        ...

    async def get_sheet(
        self,
        tenant_id: TenantId,
        cost_sheet_id: CostSheetId,
        *,
        actor: CostingActor,
    ) -> CostSheetView: ...

    async def list_versions(
        self,
        tenant_id: TenantId,
        opportunity_id: OpportunityId,
        *,
        actor: CostingActor,
    ) -> list[CostSheetView]:
        """一个机会的全部成本表版本，按类型和版本号排序。"""
        ...


class PricingEvidenceReader(Protocol):
    """可信 reader 必须核验租户、不可变内容 hash 和原文定位；不能仅返回 URL。

    生产装配的原文 IO 必须经过 Tool Gateway。本域不打开文件或访问网络。
    政策/汇率使用固定根定位 `$`；人工逐字段确认不代表模型验证价款真实性。
    """

    async def read_verified(
        self,
        tenant_id: TenantId,
        source_ref: str,
        locator: str,
        *,
        actor_id: EmployeeId,
    ) -> SourceEvidence:
        """返回经核验的安全来源投影，未知来源或不可读取时拒绝。"""
        ...


class CostingQuoteService(Protocol):
    """新报价路径的人工确认契约；不执行报价、冻结或客户发送。"""

    async def list_price_evidence(self, tenant_id: TenantId, opportunity_id: OpportunityId,
        *, actor: CostingActor) -> tuple[PriceEvidenceView, ...]:
        """先核当前C，存在但无依据返回空；缺对象/跨租户抛固定错误。"""
        ...

    async def get_coverage(self, tenant_id: TenantId, cost_sheet_id: CostSheetId,
        *, actor: CostingActor, content_hash: str | None = None) -> CostCoverageView | None:
        """按原hash恢复确认，未指定则读最近确认；不补写或冻结。"""
        ...

    async def confirm_policy(
        self,
        tenant_id: TenantId,
        command: PricingPolicyCreate,
        *,
        actor: CostingActor,
        idempotency_key: str,
    ) -> PricingPolicyView:
        """只允许当前在职老板追加已确认政策。"""
        ...

    async def get_policy(
        self, tenant_id: TenantId, category: str | None, *, actor: CostingActor
    ) -> PricingPolicyView:
        """读取当前已确认政策，绝不把旧 margin_rules 当作确认事实。"""
        ...

    async def confirm_price(
        self,
        tenant_id: TenantId,
        command: PriceEvidenceCreate,
        *,
        actor: CostingActor,
        idempotency_key: str,
    ) -> PriceEvidenceView:
        """区分供应商价格和实际费用，逐字段保存来源。"""
        ...

    async def confirm_coverage(
        self,
        tenant_id: TenantId,
        cost_sheet_id: CostSheetId,
        command: CostCoverageCreate,
        *,
        actor: CostingActor,
        idempotency_key: str,
    ) -> str:
        """确认全部费用类型与真实明细绑定，返回不可变清单内容 hash。"""
        ...

    async def confirm_quote_fx(
        self,
        tenant_id: TenantId,
        command: QuoteFxCreate,
        *,
        actor: CostingActor,
        idempotency_key: str,
    ) -> QuoteFxView:
        """确认独立报价换算方向，不更改已有成本汇率。"""
        ...

    async def get_quote_fx(
        self, tenant_id: TenantId, fx_id: str, *, actor: CostingActor
    ) -> QuoteFxView:
        """按当前身份和租户读取已确认报价汇率。"""
        ...


class NeedFactsValidator(Protocol):
    """上层适配demand的公共纯校验，不复制单位有效性规则。"""

    def require_current_unit(self, facts: NeedQuoteFacts) -> FactualField[str]:
        """返回有效客户单位，拒绝缺失、未确认或旧数量来源绑定。"""
        ...


class CostScopeSourceAccess(Protocol):
    """仅核实本次员工可读持久来源，不决定商业适用性。"""

    async def require(
        self,
        tenant_id: TenantId,
        evidence: tuple[PriceEvidenceView, ...],
        *,
        actor_id: EmployeeId,
    ) -> None:
        """在所有context/成本锁外核验当前员工阅读全部持久依据的权限。"""
        ...


class QuoteCreationCompletionReader(Protocol):
    """只接受真实持久quote到receipt的可信投影，T3B仅受控实现。"""

    async def read(
        self, tenant_id: TenantId, operation_id: str, *, actor_id: EmployeeId
    ) -> QuoteCreationCompletion | None:
        """零锁读取真实报价完成事实；不存在返回None，不接收客户自证回执。"""
        ...


class CostingFreezeService(Protocol):
    """scope、确定性计算及可恢复冻结，不负责报价CRUD或审批。"""

    async def list_scopes(self, tenant_id: TenantId, cost_sheet_id: CostSheetId,
        *, actor: CostingActor) -> tuple[CostScopeConfirmationView, ...]:
        """当前成本角色只读发现同表历史scope，不等于当前适用。"""
        ...

    async def prepare_scope_access(
        self,
        tenant_id: TenantId,
        cost_sheet_id: CostSheetId,
        command: CostScopeConfirmationCommand,
        *,
        actor: CostingActor,
    ) -> CostScopeAccess:
        """短读事务结束后做来源授权，返回仅限本次调用的精确绑定。"""
        ...

    async def confirm_scope(
        self,
        tenant_id: TenantId,
        cost_sheet_id: CostSheetId,
        command: CostScopeConfirmationCommand,
        context: CostingContext,
        *,
        actor: CostingActor,
        idempotency_key: str,
        source_access: CostScopeAccess,
    ) -> CostScopeConfirmationView:
        """在外层lease内重验并持久人工适用性，旧确认永不改写。"""
        ...

    async def get_scope(
        self, tenant_id: TenantId, confirmation_id: str, *, actor: CostingActor
    ) -> CostScopeConfirmationView:
        """按当前成本读取权限返回内部历史确认。"""
        ...

    async def calculate(
        self,
        tenant_id: TenantId,
        cost_sheet_id: CostSheetId,
        options: PricingOptions,
        context: CostingContext,
        *,
        quote_fx_ref: str | None,
        actor: CostingActor,
    ) -> CalculationSnapshot:
        """当前完整事实测算，不写locked_at或创建操作。"""
        ...

    async def freeze(
        self,
        tenant_id: TenantId,
        cost_sheet_id: CostSheetId,
        options: PricingOptions,
        context: CostingContext,
        *,
        idempotency_key: str,
        intent: QuoteCreationIntent,
        actor: CostingActor,
    ) -> FrozenCostBasis:
        """原子冻结完整意图和依据，同键可恢复，新键不能越过pending。"""
        ...

    async def get_frozen(
        self, tenant_id: TenantId, basis_id: str, *, actor: CostingActor
    ) -> FrozenCostBasis:
        """历史内部读取不代表当前报价可用，禁止直接HTTP序列化。"""
        ...

    async def get_creation(
        self, tenant_id: TenantId, idempotency_key: str, *, actor: CostingActor
    ) -> QuoteCreationOperationView | None:
        """按原键恢复完整意图、依据身份和首次完成状态。"""
        ...

    async def complete_creation(
        self, tenant_id: TenantId, operation_id: str, *, actor: CostingActor
    ) -> QuoteCreationOperationView:
        """从锁外可信reader核验回执后首次完结，不接受调用者手写报价ID。"""
        ...
