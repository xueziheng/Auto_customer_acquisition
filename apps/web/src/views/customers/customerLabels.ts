export function blockingLabel(code: string): string {
  const labels: Record<string, string> = {
    account_pending: "客户档案待关联，请管理员关联原负责人",
    ownership_conflict: "客户真实负责人与取得记录不一致，请管理员处理",
    contact_unverified: "联系方式待验证；公开邮箱不能用于跟进",
    customer_type_missing: "客户类型待确认",
    product_fit_missing: "产品适配证据待补充",
    automatic_followup_disabled:
      "自动跟进尚未启用，请联系管理员检查发件和收信设置",
    no_active_campaign: "当前公司没有活跃开发任务",
    campaign_boundary_mismatch: "客户国家、类型或假设品类不符合批准范围",
    not_customer_owner: "当前员工不是实际负责人",
    campaign_not_active: "开发任务已不活跃",
    campaign_version_changed: "开发任务版本已变化，请重新读取选项",
    campaign_not_approved: "当前任务版本没有有效审批",
    approval_status_unavailable: "当前审批事实暂不可读取",
    contact_not_eligible: "联系人归属、验证或法律依据不符合任务范围",
    contact_status_unavailable: "当前联系人资格暂不可读取",
    suppressed: "联系人或客户已在抑制名单",
    account_already_enrolled: "客户已有活跃跟进",
    campaign_quota_exceeded: "开发任务今日新联系人数已满",
    sending_identity_unavailable: "没有已认证、可发送且仍有额度的发件邮箱",
    sending_identity_status_unavailable:
      "发件邮箱状态暂时无法确认，请稍后重试",
    reply_status_unavailable: "当前回复事实暂不可读取",
    reply_already_received: "客户已有回复，不能继续冷触达",
  };
  return labels[code] ?? "当前跟进条件尚未满足，请刷新核对";
}
export function enrollmentLabel(state: string): string {
  return (
    (
      {
        enrolled: "已加入跟进",
        in_sequence: "正在跟进",
        replied: "客户已回复",
        completed: "跟进已完成",
        stopped_manual: "已手动停止",
        stopped_suppressed: "已因抑制停止",
        stopped_bounced: "已因退信停止",
        stopped_identity_unavailable: "已因发件邮箱不可用停止",
      } as Record<string, string>
    )[state] ?? "状态待核对"
  );
}
export function safeLink(url: string): string | undefined {
  try {
    const u = new URL(url);
    return ["https:", "http:"].includes(u.protocol) &&
      !u.username &&
      !u.password
      ? u.href
      : undefined;
  } catch {
    return undefined;
  }
}
