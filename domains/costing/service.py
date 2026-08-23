"""成本域服务 —— **本域的公共 API**。"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol, runtime_checkable

from domains.costing.errors import EmptyCostSheetError, MissingFxSnapshotError
from domains.costing.models import (
    CostBreakdown,
    CostItem,
    CostItemType,
    CostSheet,
    MarginRule,
)
from domains.costing.schemas import CostSheetView, QuoteReadiness
from shared.schemas.identifiers import (
    CostSheetId,
    EmployeeId,
    OpportunityId,
    TenantId,
)
from shared.schemas.money import CurrencyCode, Money, convert


def cost_item_type_values() -> tuple[str, ...]:
    """返回成本项公共词表，供上层校验建议而不导入域内部模型。"""
    return tuple(item_type.value for item_type in CostItemType)


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
        version_type: str,
        quantity: int,
        quote_currency: str,
        created_by: EmployeeId | None = None,
    ) -> CostSheetId:
        """创建成本表。

        QUOTED 类型必须同时锁定汇率快照（调 connectors/fx 的结果
        由上层传入）。
        """
        ...

    async def add_item(
        self, tenant_id: TenantId, cost_sheet_id: CostSheetId, item: CostItem
    ) -> None:
        """添加成本项。

        实现要求：
        - 已锁定（``locked_at`` 非 None）的表拒绝修改
        - 模型建议的成本项（``entered_by`` 为 None）只能进待确认区，
          由人确认后才成为正式项——模型不产出参与计算的数字
        """
        ...

    async def lock_for_quote(
        self, tenant_id: TenantId, cost_sheet_id: CostSheetId
    ) -> QuoteReadiness:
        """锁定成本表供报价使用，返回可报价性检查结果。

        **硬边界 7 的门禁执行点：**
        - ``has_indicative_items()`` 为 True 且无 ``risk_acceptance``
          → 拒绝锁定，抛 ``IndicativePriceInQuoteError``
        - 版本类型不是 QUOTED → 拒绝
        - 无汇率快照 → 拒绝

        通过后 ``locked_at`` 置位，此后**不可变**。供应商改价就开
        新版本——历史报价的依据必须永远可查。
        """
        ...

    async def accept_indicative_risk(
        self,
        tenant_id: TenantId,
        cost_sheet_id: CostSheetId,
        accepted_by: EmployeeId,
        justification: str,
    ) -> None:
        """人工接受 INDICATIVE 风险。硬边界 7 的唯一例外通道。

        ``justification`` 必填非空。这个操作本身要走 ``domains/approvals``
        （调用方负责），这里只落记录。
        """
        ...

    async def get_breakdown(
        self, tenant_id: TenantId, cost_sheet_id: CostSheetId
    ) -> CostBreakdown:
        """取计算结果。内部调 ``compute_breakdown``。"""
        ...

    async def check_margin(
        self, tenant_id: TenantId, cost_sheet_id: CostSheetId, proposed_price: str
    ) -> tuple[bool, Decimal]:
        """检查拟议价格是否满足利润底线，返回 ``(通过, 实际利润率)``。

        不通过时调用方必须走审批，且审批人不能是机会负责人。
        ``proposed_price`` 传字符串由本方法解析为 Decimal——
        避免调用方传 float 的可能性。
        """
        ...

    async def get_sheet(
        self, tenant_id: TenantId, cost_sheet_id: CostSheetId
    ) -> CostSheetView: ...

    async def list_versions(
        self, tenant_id: TenantId, opportunity_id: OpportunityId
    ) -> list[CostSheetView]:
        """一个机会的全部成本表版本，按类型和版本号排序。"""
        ...
