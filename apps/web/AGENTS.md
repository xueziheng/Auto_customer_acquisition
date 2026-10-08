# apps/web/ —— TradeOS Web 前端

## 技术

Vue 3 + TypeScript + Vite，使用 Vue Router 和统一 API 客户端。工程依赖保留 Ant Design Vue，当前页面主要由自有 Vue 组件和样式构成；没有 Pinia 或 `stores/` 目录。**类型从 API schema 生成**（openapi-typescript），不手写重复定义——手写的类型会和后端漂移。

## 页面、登录与子组件

当前 `router.ts` 注册 28 条路由记录：根路径重定向到 `/crm/handoffs`，其余 27 条记录复用 25 个页面组件，其中 24 个位于 `views/`，平台控制台位于 `components/PlatformConsole.vue`。接管队列与接管详情共用 `HandoffQueue`，成本报价与报价版本共用 `CostingQuotes`；不要按文件夹数量推断页面数量。

`App.vue` 管理全局会话恢复、登录、退出与导航。`LoginPanel` 是未登录状态，不是独立 `/login` 路由。页面中的机会详情、接管证据包、Agent 对话、产品策略与报价表单属于子组件，不等于新的顶层页面。

导航按以下五个日常入口组织，分组内页面直接显示在二级栏，不使用“更多”折叠菜单。分组和文案统一维护在 `navigation.ts`，由 `WorkspaceNavigation.vue` 渲染；路由与完整页面映射见 [README.md](README.md)：

| 主入口 | 页面归属 |
|---|---|
| 工作台 | 待跟进、审批、承诺、通知、助手、员工工作上传 |
| 产品资料 | 企业资料库、内部供应卡、目录候选产品管理 |
| 客户 | 客户发现、开发任务、贸易机会、需求证据、寻源、成本报价 |
| 消息 | 智能收件箱、本人邮箱 |
| 企业设置 | 企业配置、团队、发件身份、运行记录、手工发送故障恢复 |

`/platform` 是独立平台控制台入口，不属于企业的五组日常导航。`/approvals` 在生产和开发构建中均是实际审批页面，不能再重定向到开发任务。`/billing` 保留“未开通”兼容路由，不作为日常导航入口。导航分组不会授予权限，也不会改变 API 对当前企业和员工的授权范围。

```text
src/
├── App.vue                  全局登录状态、会话恢复与导航
├── router.ts                路由记录与页面组件绑定
├── navigation.ts            主导航与二级页面分组
├── views/
│   ├── command-center/      助手会话、指挥提案与确认
│   ├── demand-radar/        信号/假设/簇（推断必须视觉区分于事实）
│   ├── customer-discovery/
│   ├── campaigns/           开发任务、活动边界与发送进度
│   ├── inbox/
│   ├── crm/                 机会看板 + 接管队列（按等待时长排）
│   ├── knowledge/           企业资料上传、整理结果审阅与管理员确认
│   ├── products/            按角色渲染对应视图（数据已由后端裁剪）
│   ├── sourcing/            寻源案例、计划、审核与恢复
│   ├── costing-quotes/
│   ├── team/
│   ├── work-uploads/        工作文件上传与提取结果确认
│   ├── commitments/
│   ├── approvals/           一屏决定（审批包全文）
│   ├── runs/                Run 记录与证据概览
│   ├── settings/
│   ├── billing/             保留兼容路由，显示“未开通”
│   ├── NotificationCenter.vue
│   ├── SendingIdentityCenter.vue
│   └── OutreachWorkbench.vue  手工发送故障恢复工具
├── components/              跨页面组件（ProvenancePopover 最重要：
│                            任何关键数字旁的"这从哪来"展开）
├── api/                     手写客户端与认证；api.d.ts、platform-api.d.ts 是生成类型
└── composables/             可复用的会话状态与交互逻辑
```

## 两条纪律

1. **前端不做权限判断的最终裁决**——它只按后端给的数据渲染。`can_current_user_decide` 这类判断后端算好传来。
2. **推断与事实的视觉区分是产品要求**：假设卡片必须有明显的「推断」标识与证据展开，不能和已验证需求长一样。

## 当前功能边界

现有前端包含业务页面和 API 接线，已超出目录骨架；页面存在并不代表相应外部服务已经配置或业务流程已经运营验收。

- `/knowledge` 已实现企业资料上传、整理结果审阅、管理员确认和独立资料库写入状态展示；确认资料版本不会自动创建正式 Product、发布供应能力、启动开发任务或形成客户报价。`/products` 仍是内部供应卡与目录候选产品页面。
- 团队页当前展示员工与业务分配；企业自助开户、员工账号邀请与创建界面尚未实现。
- 员工工作上传与提取确认不等于企业产品建档。不得通过改名混淆两者的数据归属或写入语义。
- 保留现有租户隔离、当前员工可见范围、来源证据与人工审批边界；导航调整不能扩大业务操作权限。
- 历史无引用的 `ManualOperations.vue` 已移除。删除其他页面前必须同时核对路由、父组件、通知深链和测试调用，不能只凭顶栏没有入口判断为废弃代码。

## Sourcing V2 页面

只消费生成的 OpenAPI 类型。计划替换后必须让旧确认失效；展示事实、自述、推断、未知与 `INDICATIVE`/`QUOTED` 的差别。primary 至多一个、alternate 至多两个只是 UX guard，后端仍为最终裁决。`source_only` Product 必须显示来源链和“不可用于客户报价”；Run 页面只能显示安全 counters、quota、stop/reconciliation，不能显示 workflow context、网页原文、联系人或凭证。

## NeedCluster 寻源准入

- Command Center 的准入提案必须显式提交 `cluster_ranked`、自动准入开关和每轮上限；创建或确认提案都不得显示为已启动 Workflow，确认后只展示生效 Directive version。
- Sourcing Center 必须原样保留 API 返回顺序，分开显示“等待准入”和“处理中”；需求簇只解释排序，一个 Need 始终对应一个 Case，不能写成合并订单。
- 四类 admission 响应不是原子快照：跨状态出现重复身份时准入区必须 fail-closed；`/sourcing-cases` 只能作为独立、中性的 Case 工作台展示，不得用 admission 差集推断 Case 是历史记录或正在处理。
- 人工准入只按后端 `can_current_user_manual_start` 渲染；确认对话框中的原始 Idempotency-Key 只随请求发送，失败恢复时不得换键，也不得在 UI 或日志展示。
- admission 详情只显示安全不可变排序快照和 admitted actor/time；禁止渲染 claim token、租约、`requested_by`、完整 Workflow context 或底层异常。

## Catalog Product Proposal 页面

- 没有活动策略必须明示“未配置即关闭”；受控验收的三客户和 Kenya 只是合成场景，不得进前端默认值。
- 策略与提案只链接后端返回的精确 `approval_id`；决策请求体只发 `decision`/拒绝原因，不发 tenant、actor、facts hash、Provenance、owner 或 approver。
- 六条确定性规则必须保留固定顺序并区分“通过 / 未通过 / 未知 / 不要求”；非硬门槛的未知不得渲染成已知或 `not_required`。
- 提案与培养区都要显示固定风险提示。页面不得提供概率、自动批准、正式 Product 创建、供应商联系或客户报价动作。
- `pending_review`、`cultivation_queued`、`stale` 及其他终态必须按 API 事实分组；旧快照 stale 后不得伪装成已培养。桌面和 390px 都不能水平溢出或出现错误遮罩。
