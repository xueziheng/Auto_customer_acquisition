/** 页面分组只组织入口；页面读取和操作仍由后端授权。 */
export const workspaceGroups = [
  { id: "workbench", label: "工作台", to: "/crm/handoffs", links: [
    { label: "待跟进", to: "/crm/handoffs" },
    { label: "待审批", to: "/approvals" },
    { label: "承诺", to: "/commitments" },
    { label: "通知", to: "/notifications" },
    { label: "AI 助手", to: "/commands" },
    { label: "工作记录", to: "/work-uploads" },
  ] },
  { id: "products", label: "产品资料", to: "/products", links: [
    { label: "产品与供应", to: "/products" },
  ] },
  { id: "customers", label: "客户", to: "/prospects/accounts", links: [
    { label: "找客户", to: "/prospects/accounts" },
    { label: "开发任务", to: "/campaigns" },
    { label: "商机", to: "/crm/opportunities" },
    { label: "需求证据", to: "/demand" },
    { label: "寻源", to: "/sourcing" },
    { label: "成本与报价", to: "/costing-quotes" },
  ] },
  { id: "messages", label: "消息", to: "/inbox", links: [
    { label: "客户回复", to: "/inbox" },
    { label: "我的邮箱", to: "/inbox/mailbox" },
  ] },
  { id: "settings", label: "企业设置", to: "/settings", links: [
    { label: "设置概览", to: "/settings" },
    { label: "员工与归属", to: "/team" },
    { label: "邮箱连接", to: "/crm/sending-identities" },
    { label: "运行记录", to: "/runs" },
    { label: "发送恢复工具", to: "/crm/outreach" },
  ] },
] as const;

export function workspaceForPath(path: string) {
  return workspaceGroups.find(group => group.links.some(link =>
    path === link.to || path.startsWith(`${link.to}/`),
  )) ?? workspaceGroups[4];
}

export function sectionForPath(path: string) {
  return [...workspaceForPath(path).links]
    .sort((a, b) => b.to.length - a.to.length)
    .find(link => path === link.to || path.startsWith(`${link.to}/`));
}
