import type { components } from "../api/api";

export function laneLabel(lane: string): string {
  return ({ importer: "进口商候选", distributor: "分销商候选", ecommerce: "电商候选" }[lane] ?? "线路待核验");
}

export const researchAccessLabels: Record<components["schemas"]["ResearchAccessView"]["state"], string> = {
  not_configured: "未配置免费研究账户",
  configured_unverified: "已配置，账户尚未核实",
  free_last_verified: "上次核验为免费账户，执行前仍须复核",
  usage_unknown: "上次核验用量未知",
  paid_enabled: "上次核验发现付费已开启",
  quota_exhausted: "上次核验免费额度耗尽",
  snapshot_unavailable: "持久快照读取失败，暂不能确认",
};

export function stopLabel(reason: string | null | undefined): string {
  return ({
    plan_completed: "研究计划执行结束", budget_exhausted: "本轮研究预算已用完",
    no_results: "本轮检索未返回结果", page_disallowed: "页面禁止访问",
    no_readable_pages: "没有可读取的原页面", pending_verification: "来源待核验",
    no_supported_signals: "页面未支持可保存的需求信号", quota_exhausted: "免费额度耗尽",
    usage_unknown: "用量未知或读取失败", paid_enabled: "付费已开启，已停止",
    request_uncertain: "请求结果不确定，禁止自动重试", unsupported: "来源不支持",
    budget_missing: "缺少明确研究预算", not_configured: "未配置免费研究账户",
    proposal_not_confirmable: "提案当前不可确认",
    snapshot_unavailable: "持久快照读取失败，请只读刷新",
  }[reason ?? ""] ?? (reason ? "状态待核实" : "尚未结束"));
}
