"""成本域服务 —— **本域的公共 API**。"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol, runtime_checkable

from domains.costing.errors import EmptyCostSheetError, MissingFxSnapshotError
from domains.costing.models import (
    CostBreakdown,
    CostItemType,
    CostSheet,
    CostSheetVersion,
    MarginRule,
)
from domains.costing.permissions import CostingActor
from domains.costing.schemas import (
    CostItemCreate,
    CostSheetCreate,
    CostSheetView,
    QuoteReadiness,
)
from shared.schemas.identifiers import (
    CostSheetId,
    OpportunityId,
    TenantId,
)
from shared.schemas.money import CurrencyCode, Money, PriceBasis, convert


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


def compute_breakdown(
    sheet: CostSheet, margin_rule: MarginRule
) -> CostBreakdown:
    """确定性成本计算。**纯函数，模块级，不在 Protocol 里。**

    放在服务接口之外是刻意的：计算不依赖任何 IO，测试直接构造
    CostSheet 就能验证每个数字。这是硬边界 2 的执行点。

    实现要求：
    - 全程 Decimal，任何中间值出现 float 都是缺陷
    - 单件成本 = 逐项换算到 base_currency 后按 is_per_unit 折算再合计
    - 换算用 sheet 绑定的汇率快照，**不查当前汇率**
    - 舍入只在最终展示值上做，中间值保留精度
    - ``inputs_hash`` 覆盖全部输入（成本项、数量、汇率、规则），
      用于验证可复现性
    - 成本项为空时抛错，不返回零成本——零成本的报价是事故
    """
    raise NotImplementedError


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
