/** 深链归属只组织入口；所有读写仍由后端授权。 */
export const workspaceGroups = [
  {
    id: "discovery",
    label: "找客户",
    to: "/prospects/accounts",
    capability: "can_search",
    links: [{ label: "搜索与待分配", to: "/prospects/accounts" }],
  },
  {
    id: "customers",
    label: "我的客户",
    to: "/customers",
    links: [
      { label: "我的客户", to: "/customers" },
      { label: "待跟进", to: "/crm/handoffs", hidden: true },
      { label: "审批", to: "/approvals", hidden: true },
      { label: "承诺", to: "/commitments", hidden: true },
      { label: "通知", to: "/notifications", hidden: true },
      { label: "助手", to: "/commands", hidden: true },
      { label: "工作记录", to: "/work-uploads", hidden: true },
      { label: "开发任务", to: "/campaigns", hidden: true },
      { label: "商机", to: "/crm/opportunities", hidden: true },
      { label: "需求证据", to: "/demand", hidden: true },
      { label: "寻源", to: "/sourcing", hidden: true },
      { label: "成本与报价", to: "/costing-quotes", hidden: true },
    ],
  },
  {
    id: "messages",
    label: "消息",
    to: "/inbox",
    links: [
      { label: "业务来信", to: "/inbox/business-mail" },
      { label: "客户邮件", to: "/inbox" },
      { label: "我的邮箱", to: "/inbox/mailbox" },
    ],
  },
  {
    id: "products",
    label: "产品资料",
    to: "/knowledge",
    links: [
      { label: "企业资料库", to: "/knowledge" },
      { label: "供应卡", to: "/products", hidden: true },
    ],
  },
  {
    id: "settings",
    label: "企业管理",
    to: "/settings",
    capability: "can_assign",
    links: [
      { label: "企业配置", to: "/settings" },
      { label: "团队", to: "/team" },
      { label: "邮箱连接", to: "/crm/sending-identities" },
      { label: "运行记录", to: "/runs", hidden: true },
      { label: "发送恢复工具", to: "/crm/outreach", hidden: true },
    ],
  },
] as const;
export function workspaceForPath(path: string) {
  return (
    workspaceGroups.find((group) =>
      group.links.some(
        (link) => path === link.to || path.startsWith(`${link.to}/`),
      ),
    ) ?? workspaceGroups[1]
  );
}
export function sectionForPath(path: string) {
  return [...workspaceForPath(path).links]
    .sort((a, b) => b.to.length - a.to.length)
    .find((link) => path === link.to || path.startsWith(`${link.to}/`));
}
