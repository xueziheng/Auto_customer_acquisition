# TradeOS Web

这是 TradeOS 的 Vue 3 + TypeScript + Vite 网页应用，已有业务页面、登录会话与后端 API 接线。页面可打开不等于其中的外部服务已配置，也不等于真实获客、发信或成交流程已验收。

默认入口 `/` 转到 `/customers`，显示本人客户；`/customers/:allocationId` 用明确的取得记录 ID 加载详情。`/platform` 继续使用独立平台认证与控制台。

## 页面入口

日常入口为找客户（boss）、我的客户、消息、产品资料，boss 另有企业管理。导航由 `/prospects/customers/access` 的能力响应决定，读取失败关闭管理员入口；单一页面分组不重复显示二级导航。

| 入口 | 路由 | 页面组件 |
|---|---|---|
| 找客户（boss） | `/prospects/accounts` | `customer-discovery/CustomerDiscovery.vue` |
| 我的客户 | `/customers` | `customers/MyCustomers.vue` |
| 客户详情与跟进 | `/customers/:allocationId` | `customers/CustomerDetail.vue` |
| 客户回复 / 我的邮箱 | `/inbox`、`/inbox/mailbox` | `inbox/SmartInbox.vue`、`inbox/MyMailbox.vue` |
| 产品资料 | `/knowledge` | `knowledge/KnowledgeCenter.vue` |
| 企业管理（boss） | `/settings`、`/team`、`/crm/sending-identities` | `settings/SettingsCenter.vue`、`team/TeamCenter.vue`、`SendingIdentityCenter.vue` |
| 独立平台管理 | `/platform` | `components/PlatformConsole.vue` |

保留的上下文和历史深链：`/crm/handoffs`、`/crm/handoffs/:handoffId`、`/approvals`、`/commitments`、`/notifications`、`/commands`、`/work-uploads`、`/products`、`/campaigns`、`/crm/opportunities`、`/demand`、`/demand/needs/:needId`、`/sourcing`、`/sourcing/:caseId`、`/costing-quotes`、`/costing-quotes/quotes/:quoteId`、`/runs`、`/crm/outreach`、`/billing`。这些页面不再作为并列日常入口；客户详情提供回复、接管、证据工作台和管理员开发任务配置入口。

找客户先搜索进入公司待分配，再批量分配给本公司在职员工，分配不重复消耗额度。列表以服务端 `unassigned_only=true` 分页，最多选择100条，仅覆盖已加载范围。员工直达搜索页只读取权限并显示说明，不请求公司搜索任务或平台来源。

我的客户只读取本人范围；详情承接公开来源、真实验证和跟进状态。历史缺档案时由boss显式同原owner关联，公开邮箱不会变成已验证联系人。自动跟进仅使用服务端提供的实际任务版本、合格联系方式和该客户的来源假设，未就绪显示具体原因。批量逐客户提交、显示每条结果；未知结果保留原请求供显式核对，刷新和换账号不会自动发送。默认关闭的自动运行配置不因按钮出现就被视为已启用。

审批页在生产和开发构建中都使用实际审批组件。页面显示哪些数据、能执行哪些操作，由现有后端会话和权限检查决定；导航可见不是操作授权。

## 登录、子界面与功能边界

- `App.vue` 管理会话恢复、登录、退出和导航。`components/LoginPanel.vue` 是全局未登录状态，当前没有独立 `/login` 页面。`ControlledModeBar` 只在显式受控开发配置下显示。
- `crm/OpportunityDetail.vue`、`crm/HandoffPacketView.vue`、Agent 对话、产品策略/候选/培养面板和报价表单由父页面挂载。它们不是独立顶层页面，但都有实际引用，不能因为路由表里没有就删除。
- `/knowledge` 已实现企业资料上传、整理结果审阅、管理员确认和独立资料库写入状态展示；确认资料版本不会自动创建正式 Product、发布供应能力、启动开发任务或形成客户报价。`/products` 仍是内部供应卡与目录候选产品页面；员工工作上传不能替代企业资料流程。
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

`npm run gen:api` 把业务与平台 OpenAPI JSON 通过标准输出传给生成器；提交的生成物为 `src/api/api.d.ts` 和 `src/api/platform-api.d.ts`，两者均不得手写修改。

所有 API 身份由 `src/api/client.ts` 的统一中间件注入，页面组件不得自行设置
`X-Tenant-Id` 或 `X-Employee-Id`。Vite 开发模式只有在 `VITE_TENANT_ID` 与
`VITE_EMPLOYEE_ID` 同时固定配置时才允许使用断言身份；生产模式必须先通过认证
启动流程调用 `configureAuthenticatedIdentity`，否则请求会在发出前失败关闭。
