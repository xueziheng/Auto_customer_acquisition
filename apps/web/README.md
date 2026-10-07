# TradeOS Web

这是 TradeOS 的 Vue 3 + TypeScript + Vite 网页应用，已有业务页面、登录会话与后端 API 接线。页面可打开不等于其中的外部服务已配置，也不等于真实获客、发信或成交流程已验收。

当前路由表有 26 条记录，其中根路径 `/` 转到工作台 `/crm/handoffs`，其余 25 条记录共用 23 个页面组件。接管详情和报价版本分别复用对应列表/工作台组件。

## 页面入口

顶层导航只有五个业务入口，分组页面直接显示在二级导航栏，没有“更多”折叠菜单。需求、寻源、接管和报价详情由页面内的链接打开。分组与导航文案定义在 `src/navigation.ts`，由 `components/WorkspaceNavigation.vue` 渲染；表中“页面组件”均位于 `src/views/`，独立路由以 `src/router.ts` 为准。

| 主入口 | 页面 / 用途 | 路由 | 页面组件 |
|---|---|---|---|
| 工作台 | 待跟进与人工接管 | `/crm/handoffs`、`/crm/handoffs/:handoffId` | `crm/HandoffQueue.vue` |
| 工作台 | 审批 | `/approvals` | `approvals/ApprovalCenter.vue` |
| 工作台 | 承诺与待办 | `/commitments` | `commitments/CommitmentCenter.vue` |
| 工作台 | 本人通知 | `/notifications` | `NotificationCenter.vue` |
| 工作台 | 助手与指挥提案 | `/commands` | `command-center/CommandCenter.vue` |
| 工作台 | 员工工作上传与提取确认 | `/work-uploads` | `work-uploads/WorkUploads.vue` |
| 产品资料 | 内部供应卡、目录候选产品管理 | `/products` | `products/ProductSupplyCenter.vue` |
| 客户 | 客户发现 | `/prospects/accounts` | `customer-discovery/CustomerDiscovery.vue` |
| 客户 | 开发任务与活动状态 | `/campaigns` | `campaigns/CampaignCenter.vue` |
| 客户 | 贸易机会与机会详情 | `/crm/opportunities` | `crm/OpportunityList.vue` |
| 客户 | 需求信号、假设与已验证需求 | `/demand` | `demand-radar/DemandRadar.vue` |
| 客户 | 已验证需求详情 | `/demand/needs/:needId` | `demand-radar/ValidatedNeedDetail.vue` |
| 客户 | 寻源案例列表与准入状态 | `/sourcing` | `sourcing/SourcingCenter.vue` |
| 客户 | 寻源案例计划、候选、审核与恢复 | `/sourcing/:caseId` | `sourcing/SourcingCaseDetail.vue` |
| 客户 | 成本、报价与报价版本 | `/costing-quotes`、`/costing-quotes/quotes/:quoteId` | `costing-quotes/CostingQuotes.vue` |
| 消息 | 已纳入业务流程的客户会话 | `/inbox` | `inbox/SmartInbox.vue` |
| 消息 | 本人邮箱的历史往来 | `/inbox/mailbox` | `inbox/MyMailbox.vue` |
| 企业设置 | 企业配置与服务就绪信息 | `/settings` | `settings/SettingsCenter.vue` |
| 企业设置 | 员工与业务分配 | `/team` | `team/TeamCenter.vue` |
| 企业设置 | 发件身份、认证、预热与入站绑定 | `/crm/sending-identities` | `SendingIdentityCenter.vue` |
| 企业设置 | 运行记录与证据概览 | `/runs` | `runs/RunCenter.vue` |
| 企业设置 | 手工发送与发送结果核对 | `/crm/outreach` | `OutreachWorkbench.vue` |
| 非日常导航 | 订阅计费“未开通”兼容页 | `/billing` | `billing/BillingUnavailable.vue` |

审批页在生产和开发构建中都使用实际审批组件。页面显示哪些数据、能执行哪些操作，由现有后端会话和权限检查决定；导航可见不是操作授权。

## 登录、子界面与功能边界

- `App.vue` 管理会话恢复、登录、退出和导航。`components/LoginPanel.vue` 是全局未登录状态，当前没有独立 `/login` 页面。`ControlledModeBar` 只在显式受控开发配置下显示。
- `crm/OpportunityDetail.vue`、`crm/HandoffPacketView.vue`、Agent 对话、产品策略/候选/培养面板和报价表单由父页面挂载。它们不是独立顶层页面，但都有实际引用，不能因为路由表里没有就删除。
- “产品资料”目前连接现有供应卡和目录候选产品界面。企业上传产品、完善规格、确认发布的完整产品建档流程尚未实现；员工工作上传也不能替代这个流程。
- 团队页目前是员工资料和分配规则的展示；企业自助开户、员工账号邀请与创建界面尚未实现。
- 智能收件箱和本人邮箱使用不同的数据与权限范围；开发任务与手工发送故障恢复也有不同职责。可以统一导航，不能合并后扩大数据可见范围或发送权限。
- 新建开发任务的目标市场与产品类别由当前企业填写，不再预填历史试验的国家和品类；现有发送限额、停止条件及后端门禁仍适用。

## 代码与开发

`src/components/` 放跨页面组件，`src/composables/` 放可复用状态逻辑，`src/api/client.ts` 是手写统一客户端，`src/api/authentication.ts` 管理认证交互。项目没有 Pinia 或 `stores/` 目录。

工程仍声明 Ant Design Vue 依赖，但当前源码没有使用其组件；现有页面主要使用自有组件和样式。本轮导航整理保留依赖与现有锁文件，不把依赖清理混入页面修复。日常开发和 CI 使用 npm。

开发环境使用 Node 24.x；生成 API 类型需要项目 Python 3.12+ 环境。API 类型由后端零参数 `create_app()` 工厂导出的 OpenAPI 契约生成，不能手写重复 DTO。以下命令在 `apps/web/` 执行：

```bash
npm ci
npm run gen:api
npm run typecheck
npm run lint
npm run test
npm run build
```

`npm run gen:api` 只把临时 OpenAPI JSON 通过标准输出传给生成器；仓库中唯一提交的生成物是 `src/api/api.d.ts`。

所有 API 身份由 `src/api/client.ts` 的统一中间件注入，页面组件不得自行设置
`X-Tenant-Id` 或 `X-Employee-Id`。Vite 开发模式只有在 `VITE_TENANT_ID` 与
`VITE_EMPLOYEE_ID` 同时固定配置时才允许使用断言身份；生产模式必须先通过认证
启动流程调用 `configureAuthenticatedIdentity`，否则请求会在发出前失败关闭。
