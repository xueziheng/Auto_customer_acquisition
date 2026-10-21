# apps/web/ —— TradeOS Web 前端

## 技术

Vue 3 + TypeScript + Vite，使用 Vue Router 和统一 API 客户端。工程依赖保留 Ant Design Vue，当前页面主要由自有 Vue 组件和样式构成；没有 Pinia 或 `stores/` 目录。**类型从 API schema 生成**（openapi-typescript），不手写重复定义——手写的类型会和后端漂移。

## 页面、登录与子组件

`router.ts` 的根路径重定向到 `/customers`（我的客户），客户详情使用 `/customers/:allocationId`，取得记录 ID 不等于 ProspectAccount ID。历史业务深链继续注册，平台控制台仍为独立 `/platform`。

`App.vue` 管理会话恢复、登录、退出与导航；`LoginPanel` 是未登录状态，不是独立 `/login` 路由。导航能力由 `/prospects/customers/access` 读取，生产与受控开发身份使用同一入口，读取失败默认不显示管理员入口。

| 日常主入口 | 页面归属 |
|---|---|
| 找客户（can_search） | 搜索、公司待分配、批量分配 |
| 我的客户 | 本人客户列表、allocation详情、真实验证与逐客户跟进 |
| 消息 | 客户回复、本人邮箱 |
| 产品资料 | 企业资料库 |
| 企业管理（can_assign） | 企业配置、团队、发件邮箱 |

只有一个可见页面的分组不渲染重复二级栏。供应卡、开发任务、商机、需求证据、寻源、报价、运行记录、恢复工具保留路由但不作为日常并列导航。相关操作从客户详情进入，管理员才显示开发任务配置入口。导航不授予权限；平台控制台不能混入企业客户数据或修改认证。

### 客户工作台纪律

- 员工直达 `/prospects/accounts` 必须先读取 access，权限不满足不得挂载 InventoryPanel，不请求公司 status/jobs/allocations/sources。搜索页不请求平台 sources、不显示技术预算和全市场库存数。
- 待分配采用 `unassigned_only=true` 的服务端分页，一次最多100条且只能全选当前已加载范围；目标为同公司在职 boss/manager/sales，首次分配不能覆盖他人归属。ADR0094另在已关联客户详情提供管理员显式转交，须提交原负责人/分配时间及原因，活动跟进须先停止。
- 历史缺 account_id 详情只读；boss 可显式调用同原负责人的幂等 assignment 关联客户档案，不修改 owner/额度，也不伪造联系人或验证。
- 客户读写必须使用请求身份/代际门禁，换账号或403清除资料、选择、表单及待操作，迟到响应不能恢复旧身份资料。未知跟进结果按客户保留原 key 和严格 command，刷新只读，显式恢复才重发；不得显示 key 或保存凭证、公开邮箱、发送内容。
- 跟进逐条使用真实 campaign/contact/source_hypothesis/version，仅 server can_start 和 eligible_contact_point_ids 决定可启动；批量逐项显示结果，不承诺跨客户原子性。公开邮箱仍是未验证，假设仍标推断，自动未启用必须解释实际阻塞；停止还须当前owner的 can_stop 和活跃Enrollment。

```text
src/
├── App.vue                  全局登录状态、会话恢复与导航
├── router.ts                路由记录与页面组件绑定
├── navigation.ts            主导航与二级页面分组
├── views/
│   ├── command-center/      助手会话、指挥提案与确认
│   ├── demand-radar/        信号/假设/簇（推断必须视觉区分于事实）
│   ├── customer-discovery/  管理员搜索和待分配
│   ├── customers/           本人客户、详情、跟进和原请求恢复
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

## ADR0084 网页 Gmail 连接

本人邮箱页提供 Google 授权及固定邮件诊断。回调查询必须在应用初始化前清除，只保存在当前页面内存中；
通过既有认证和 CSRF 客户端完成绑定，不写浏览器持久存储。诊断必须展示原始收件地址与固定英文内容，
未知结果保留原 request_id 核对，不换键重发。Gmail 接受发送不等于收件人已收到。

## ADR0095 阿里企业邮箱

本人邮箱页的阿里连接只接受真实本人会话，提交立即清空密码，身份变化或401/403清空所有私有状态。认证成功不等于已发信或自动回复启用；SMTP接受也不等于客户收到。
