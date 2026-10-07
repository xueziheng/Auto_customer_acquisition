import type { components } from "../../api/api";

export const costItemLabels = [
  ["product_purchase", "产品采购"], ["sample_fee", "样品"], ["mold_fee", "模具"],
  ["customization_fee", "定制"], ["logo_printing", "标志印刷"], ["packaging", "包装"],
  ["quality_inspection", "质检"], ["wastage", "损耗"], ["domestic_freight", "国内运输"],
  ["international_freight", "国际运输"], ["insurance", "保险"], ["customs_clearance", "报关"],
  ["duties_and_taxes", "关税与税费"], ["destination_freight", "目的地运输"], ["warehousing", "仓储"],
  ["payment_fees", "支付手续费"], ["sales_commission", "销售佣金"], ["customer_acquisition", "获客"],
  ["contact_data_cost", "联系人数据"], ["ad_allocation", "广告分摊"], ["agent_api_allocation", "智能助手/接口分摊"],
  ["returns_reserve", "退货售后预留"],
] as const;

export const profitMetricLabels: Readonly<Record<string, string>> = {
  gross_profit: "毛利", contribution_profit: "贡献利润", full_cost_profit: "完整成本利润",
  margin_rate: "利润率（十进制比例）", minimum_price: "最低价格", target_price: "目标价格",
  unit_full_cost: "单位完整成本", discount_headroom: "可折扣空间", additional_acquisition_headroom: "可增加获客成本空间",
};

export function createQuotePriceBody(amount: string, currency: string): components["schemas"]["Money"] {
  if (!amount.trim() || !currency.trim()) throw new Error("请填写金额和币种");
  return { amount: amount.trim(), currency: currency.trim() };
}

export function utf16SelectionToCodepoints(text: string, start: number, end: number): readonly [number, number] {
  const splitsPair = (position: number): boolean => {
    if (position <= 0 || position >= text.length) return false;
    const left = text.charCodeAt(position - 1);
    const right = text.charCodeAt(position);
    return left >= 0xd800 && left <= 0xdbff && right >= 0xdc00 && right <= 0xdfff;
  };
  if (!Number.isInteger(start) || !Number.isInteger(end)
    || start < 0 || end > text.length || start >= end || splitsPair(start) || splitsPair(end)) {
    throw new Error("请选择完整的原文字符");
  }
  return [Array.from(text.slice(0, start)).length, Array.from(text.slice(0, end)).length];
}

export function costItemLabel(value: string): string {
  return costItemLabels.find(([code]) => code === value)?.[1] ?? `待核实（${value}）`;
}
