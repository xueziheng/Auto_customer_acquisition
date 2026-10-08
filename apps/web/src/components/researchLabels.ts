import type { components } from "../api/api";

export function laneLabel(lane: string): string {
  return ({ importer: "进口商候选", distributor: "分销商候选", ecommerce: "电商候选" }[lane] ?? "线路待核验");
}

const sourceChannelLabels: Record<components["schemas"]["RunResearchView"]["source_channels"][number], string> = {
  public_web: "公开官网与店铺",
  industry_directory: "行业企业名录",
  association_members: "协会会员",
  trade_show_exhibitors: "展会参展名单",
  public_procurement: "公开采购公告",
  company_news: "企业动态",
  public_linkedin_company: "领英公开公司页面",
  public_trade_records: "公开贸易记录索引",
};

export function sourceChannelLabel(channel: string): string {
  return sourceChannelLabels[channel as keyof typeof sourceChannelLabels] ?? "来源方向待核验";
}

export const researchAccessLabels: Record<components["schemas"]["ResearchAccessView"]["state"], string> = {
  not_configured: "未配置免费研究账户",
  configured_unverified: "已配置，账户尚未核实",
  free_last_verified: "上次核验有可用免费额度，执行前仍须复核",
  usage_unknown: "上次核验用量未知",
  paid_enabled: "上次核验发现付费已开启",
  quota_exhausted: "上次核验免费额度耗尽",
  snapshot_unavailable: "持久快照读取失败，暂不能确认",
};

const researchStopLabels: Record<NonNullable<components["schemas"]["RunResearchView"]["stop_reason"]>, string> & Record<string, string> = {
    plan_completed: "研究计划执行结束", budget_exhausted: "本轮研究预算已用完",
    no_results: "本轮检索未返回结果", page_disallowed: "页面禁止访问",
    no_readable_pages: "没有可读取的原页面", pending_verification: "来源待核验",
    no_supported_signals: "页面未支持可保存的需求信号", quota_exhausted: "免费额度耗尽",
    usage_unknown: "用量未知或读取失败", paid_enabled: "付费已开启，已停止",
    request_uncertain: "请求结果不确定，禁止自动重试", unsupported: "来源不支持",
    model_permission: "当前身份无权调用研究模型",
    model_configuration: "模型配置不可用，请联系管理员",
    model_quota: "本地模型调用额度或并发上限已触发",
    model_authentication: "模型服务认证失败，请联系管理员",
    model_insufficient_balance: "模型服务账户余额不足",
    model_invalid_request: "模型服务未接受请求",
    model_rate_limit: "模型服务请求受限，本轮研究已停止",
    model_provider_error: "模型服务异常，本轮研究已停止",
    model_output_limit: "模型输出达到上限，请缩小研究范围后重新发起",
    model_invalid_response: "模型返回内容无效，本轮研究已停止",
    model_unknown: "模型请求结果不确定，禁止自动重试",
    budget_missing: "缺少明确研究预算", not_configured: "未配置免费研究账户",
    proposal_not_confirmable: "提案当前不可确认",
    snapshot_unavailable: "持久快照读取失败，请只读刷新",
};

export function stopLabel(reason: string | null | undefined): string {
  return researchStopLabels[reason ?? ""] ?? (reason ? "状态待核实" : "尚未结束");
}
