# Slice 3 CRM 设计规范：Evidence Ledger / 证据账本

> 适用任务：S3-18 机会看板、S3-19 人工接管队列
> 桌面基准：1440 × 900；最小验收宽度：1180 px
> 数据声明：原型和实现示例中的每条记录都必须显示 `演示数据`，公司、人员和标识符均为虚构。

## 1. 方向决策

### 1.1 选定方向：Evidence Ledger / 证据账本

采用「证据账本」作为两个页面的统一视觉与交互系统。界面把机会和接管事项呈现为可核查、可追溯、可行动的业务记录：左侧是保持后端顺序的记录列表，右侧是当前记录的详情与行动区，关键字段旁固定提供 `ProvenancePopover`。视觉层级优先回答四个问题：

1. 哪些内容是客户确认过的事实；
2. 哪些内容只是系统或员工的推断；
3. 结论来自哪里、由谁确认；
4. 当前真人下一步应该做什么。

选择理由：该方向最直接落实事实/推断分离与 Provenance 硬边界，同时能让等待顺序、状态变化和操作结果保持清楚。它不过度强化分数、排名或“作战感”，因此不会诱导员工绕过 `等待最久优先`，也不会把推断伪装成测量结果。

### 1.2 设计原则

- **证据先于结论**：影响决策的字段旁均可展开来源摘要。
- **事实与推断结构分离**：两者使用不同容器、边界、图标和文字标签，不只依靠颜色。
- **服务顺序即呈现顺序**：前端不按分数重排机会，也不按价值重排接管队列。
- **行动有边界**：前端只发起操作并呈现后端结果；API 与域服务判权始终是最终裁决。
- **安全失败**：错误文案固定、简短、可恢复，不回显异常、客户原话、证据链接或被拒绝的资源 ID。
- **克制的信息密度**：列表用于扫描，详情用于核查与操作；不在列表卡片展示客户原话。

### 1.3 未采用方向与取舍

| 方向 | 优点 | 未采用原因 |
|---|---|---|
| Operations Tower / 运行塔台 | 状态密度高，适合监控大量运行指标 | 过强的排行榜和告警语义容易让分数替代证据，并诱导高价值项目插队，不适合作为 Phase 1 接管队列的主隐喻。 |
| Audit Canvas / 审计画布 | 证据链表达最完整，适合深度复核 | 画布式自由布局降低扫描效率和键盘可预测性；两页核心仍是队列处理，不是自由调查。 |
| Calm Trade Desk / 清晰协作台 | 友好、低压、学习成本低 | 对 SLA、来源与冲突状态的强调不足，容易把严格业务边界弱化成普通任务列表。 |

## 2. 视觉系统

### 2.1 色彩 tokens

| Token | 值 | 用途 |
|---|---:|---|
| `color.canvas` | `#F5F7F7` | 页面底色 |
| `color.surface` | `#FFFFFF` | 面板、卡片、弹层 |
| `color.text.primary` | `#172323` | 主要文本 |
| `color.text.secondary` | `#526363` | 次要文本 |
| `color.border` | `#CBD7D5` | 普通边界 |
| `color.fact` | `#0F766E` | 已验证事实标识、实线边界 |
| `color.fact.soft` | `#E7F5F2` | 已验证事实背景 |
| `color.inference` | `#9A6700` | 推断标识、图标、虚线边界 |
| `color.inference.soft` | `#FFF5D6` | 推断背景 |
| `color.action` | `#155EEF` | 主要操作与链接 |
| `color.danger` | `#B42318` | 失败/破坏性动作 |
| `color.danger.soft` | `#FEF0EC` | 409/400 错误背景 |
| `color.warning` | `#B54708` | 到期、缺信息、503 提示 |
| `color.focus` | `#7C3AED` | 键盘焦点环，区别于状态颜色 |

正文与背景对比度不得低于 4.5:1。状态始终同时使用文字、图标或边界样式，禁止只用颜色表达。

### 2.2 字体、间距、圆角与层级

- 字体栈：`-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif`。
- 基础字号：14 px；正文行高 1.55；辅助文字 12 px；页面标题 24 px/1.25；区域标题 16 px/1.4。
- 字重：正文 400，标签/按钮 600，页面标题 700；不使用全大写英文制造噪声。
- 间距 tokens：`space.1=4`、`space.2=8`、`space.3=12`、`space.4=16`、`space.5=24`、`space.6=32` px。
- 圆角：标签 6 px，卡片/面板 10 px，弹层 12 px。
- elevation：普通面板 `0 1px 2px rgba(20,35,35,.08)`；浮层 `0 12px 32px rgba(20,35,35,.18)`。不靠阴影区分事实与推断。
- 焦点：所有可交互元素使用 `3px solid color.focus` 外环并保留 2 px 间隙；不得移除浏览器焦点而无替代。
- 动效：仅允许 120–180 ms 的透明度/位移动效；`prefers-reduced-motion: reduce` 时取消过渡、平滑滚动和自动动画。

## 3. 页面壳与共享组件

两个页面共享 `CrmWorkspaceShell`：顶部统一使用 dark-teal 产品导航，包含产品标识、机会看板/人工接管队列/失败原因分析切换与当前演示身份。页面根节点固定为视口高度且 `overflow: hidden`，只允许内部 pane 滚动，1440 × 900 与 1180 × 800 均不得产生 document 纵向滚动。机会看板主体为列表 + 详情双栏；接管页主体为最久等待队列 + 完整接管包 + 操作状态三栏。

共享组件职责：

- `DemoDataBadge`：每条合成记录显示 `演示数据`，不得只在页面顶部统一声明。
- `FactField`：实线中性/teal 容器，显示 `已验证事实`、字段名、值和 provenance 入口。
- `InferencePanel`：amber 背景、虚线边界，标题固定含 `推断`；解释为什么系统这样判断，但不显示概率数字。
- `RankBucketTag`：唯一绑定 `ScoreExplanation.rank_bucket`；固定显示 `排序桶：高/中/低`，不把其他证据描述当成排序桶，也不在前端重算。
- `StatusTag`：文字 + 图标 + 颜色三重表达。
- `SafeErrorBanner`：固定安全文案、恢复动作和 `role="alert"`；不渲染异常原文或动态资源 ID。
- `ActionBar`：操作按钮、进行中状态及结果提示；前端隐藏按钮仅用于减少干扰，不构成授权。

### 3.1 `ProvenancePopover`

`ProvenancePopover` 是决策字段旁的一等控制，不藏在二级设置中。触发按钮可见文案为 `查看来源`，并带来源图标；按钮使用 `aria-haspopup="dialog"`、`aria-expanded`、`aria-controls`。

弹层标题为 `字段来源`，开放示例必须完整显示以下安全摘要：

| UI 文案 | DTO 字段 |
|---|---|
| 来源类型 | `ProvenanceSummary.source_type` |
| 来源标识 | `source_id` |
| 提取者 | `extracted_by` |
| 提取时间 | `extracted_at` |
| 确认人 | `confirmed_by` |
| 确认时间 | `confirmed_at` |

`source_url` 仅在后端返回安全可访问值且产品规则允许时显示为 `打开来源`；不得在列表直接展开 URL。`page_hash` 可在审计详情中显示为截断后的只读校验标识，不作为用户操作入口。

焦点模型：打开后焦点进入弹层标题/首个控件；Tab 保持在弹层内；`Escape`、关闭按钮或点击弹层外关闭；关闭后焦点返回原触发按钮。弹层使用 `role="dialog"`、可访问名称和明确的 `关闭来源` 按钮。移动到窄宽度时改为右侧抽屉，但语义和焦点规则不变。

## 4. Opportunity Board / 机会看板

### 4.1 组件树

```text
OpportunityBoardView
└── CrmWorkspaceShell
    ├── TopProductNavigation
    │   ├── ProductBrand
    │   ├── CrmPageTabs
    │   └── DemoIdentity
    ├── OpportunityFilterBar
    │   ├── PageTitle("机会看板")
    │   ├── BackendOrderNotice("按后端顺序显示")
    │   ├── StateFilter
    │   ├── PageLimitControl
    │   └── RefreshButton
    ├── OpportunityListRegion
    │   ├── LoadingSkeleton
    │   ├── OpportunityEmptyState
    │   ├── SafeErrorBanner
    │   └── OpportunityList
    │       └── OpportunityListItem[]
    │           ├── DemoDataBadge
    │           ├── StatusTag
    │           ├── AccountSummary
    │           ├── OwnerSummary
    │           ├── CountryCategorySummary
    │           ├── NextActionSummary
    │           └── RankBucketTag
    └── OpportunityDetailRegion
        ├── DetailPlaceholder
        └── OpportunityDetailPanel
            ├── DemoDataBadge
            ├── OpportunityIdentityHeader
            ├── FactSection
            │   └── FactField[]
            │       └── ProvenancePopover
            ├── InferencePanel("推断")
            │   └── ScoreExplanationView
            ├── AmountSummary
            ├── LossSummary
            └── ActionBar
                ├── TransitionAction
                └── MarkLostAction
```

### 4.2 API 与生成类型绑定

实现必须从 OpenAPI 生成类型和 `openapi-fetch` client 取类型，不手写 `Opportunity`、`Money`、`ScoreExplanation` 或 provenance 的重复接口。

| 用户意图 | Endpoint | 生成 DTO / 请求类型 | UI 行为 |
|---|---|---|---|
| 加载机会列表 | `GET /crm/opportunities` | `OpportunityView[]`；query `states`、`limit` | 按响应数组原顺序渲染；筛选/分页不得改写授权语义。 |
| 读取详情 | `GET /crm/opportunities/{opportunity_id}` | `OpportunityView` | 替换详情区；请求期间保留列表上下文。 |
| 推进状态 | `POST /crm/opportunities/{opportunity_id}/transition` | `OpportunityTransitionBody` | 提交期间禁用 transition 与 mark-lost 写操作并忽略重复点击；成功后重新获取详情与当前列表，不在本地猜测最终状态。 |
| 标记失败 | `POST /crm/opportunities/{opportunity_id}/mark-lost` | `OpportunityMarkLostBody` | `reason` 必填，`detail` 可选；提交期间禁用两个写操作并忽略重复点击，成功后重新取数。 |
| 人工录入机会 | `POST /crm/opportunities` | `OpportunityIntakeBody`，嵌套域 DTO | 不属于 S3-18 主流程；若后续挂入口，仍复用生成类型。204 表示未通过硬门槛。 |

### 4.3 `OpportunityView` 到 UI 的映射

| UI 区域 | DTO 字段 |
|---|---|
| 标识与账户 | `opportunity_id`、`account_id`、`account_name`、`need_id` |
| 状态与时间 | `state`、`created_at`、`next_action`、`next_action_due` |
| 负责人 | `owner`、`owner_name` |
| 市场/品类 | `country`、`product_category` |
| 已验证需求字段 | `quantity`、`spec_summary`、`destination`、`required_by`、`current_supply_problem`、`can_source` |
| 证据与解释 | `score`、`provenance` |
| 金额摘要 | `target_price`、`estimated_cost`、`estimated_profit` |
| 失败闭环 | `loss_reason`、`died_at_state` |
| 接管提示 | `has_pending_handoff` |

`score` 仅作为解释区呈现 `ScoreExplanation.rank_bucket`、通过/失败门槛和原因；UI 对 `high`、`mid`、`low` 固定显示 `排序桶：高`、`排序桶：中`、`排序桶：低`。禁止拿证据描述替代 `rank_bucket`，禁止将其转换成概率或在客户端重新计算。关键字段依据 `provenance[].field_name` 关联 `ProvenancePopover`；找不到来源时显示 `来源摘要暂不可用`，不得补造来源。

金额边界：`Money.amount` 始终按 OpenAPI JSON string 处理和展示，不转为 JavaScript `number`，不使用浮点计算价格、成本或利润；币种来自 `currency`。前端不得生成最终金额或自行换汇。

### 4.4 详情与操作状态机

```text
未选择 ──选择列表项──> 详情加载中 ──200──> 详情就绪
                              ├─403──> 无权限
                              ├─503──> 暂不可用（可重试）
                              └─其他──> 安全错误

详情就绪 ──提交 transition──> 提交中 ──200──> 刷新详情+列表
                                      ├─409──> 当前状态不允许此操作
                                      ├─400──> 请求参数无效
                                      ├─403──> 没有权限
                                      └─503──> 服务暂时不可用（按 Retry-After 重试）

详情就绪 ──打开 mark-lost──> 填写原因 ──提交──> 提交中
                                                   ├─200──> 关闭表单并刷新
                                                   ├─400──> 请求参数无效
                                                   ├─403──> 没有权限
                                                   └─503──> 保留已填内容并允许重试
```

客户端不得提前将状态改成目标值。提交期间禁用同一操作并显示 `处理中…`；响应后以重新获取的 DTO 为准。

状态与失败原因必须使用公共枚举值，不得在前端造同义值。`OpportunityState` 仅允许 `qualified`、`assigned`、`contacted`、`sourcing`、`quoted`、`negotiating`、`won`、`lost`；中文只作为显示标签。`LossReason` 选择器精确覆盖全部 12 个公共值：`unreachable`、`no_reply`、`need_not_real`、`no_supply_found`、`price_too_high`、`lost_to_competitor`、`customer_went_silent`、`timing_mismatch`、`compliance_blocked`、`margin_too_low`、`internal_no_capacity`、`duplicate`。提交值保持英文枚举原值，中文仅作为显示标签。

## 5. Human Handoff Queue / 人工接管队列

### 5.1 组件树

```text
HandoffQueueView
└── CrmWorkspaceShell
    ├── TopProductNavigation
    │   ├── ProductBrand
    │   ├── CrmPageTabs
    │   └── DemoIdentity
    ├── HandoffQueueRegion
    │   ├── PageTitle("人工接管队列")
    │   ├── FairnessRule("等待最久优先")
    │   ├── RefreshButton
    │   ├── QueueOrderExplanation
    │   │   └── text("requested_at 升序 / wait_seconds 降序；不按分数排序")
    │   ├── LoadingSkeleton
    │   ├── HandoffEmptyState
    │   ├── SafeErrorBanner
    │   └── HandoffQueueList
    │       └── HandoffQueueItem[]
    │           ├── DemoDataBadge
    │           ├── WaitTimeBadge
    │           ├── TriggerTag
    │           ├── AccountCountrySummary
    │           ├── AssignedOwnerSummary
    │           ├── WhyValuableSummary
    │           ├── SuggestedNextStep
    │           ├── MissingInformationList
    │           └── EvidenceEntry
    │               └── ProvenancePopover
    ├── HandoffPacketRegion
        ├── PacketPlaceholder
        └── HandoffPacketPanel
            ├── DemoDataBadge
            ├── PacketHeader
            ├── FactSection
            ├── ContextSection
            ├── MissingInformationSection
            ├── AlreadySentSection
            ├── CommitmentsMadeSection
            ├── EvidenceSection
            │   └── ProvenancePopover[]
    └── HandoffOperationStatusRegion
        ├── AcceptHandoffButton("接受接管")
        ├── OperationLiveRegion
        ├── RecoveryControls
        └── SafeStateExamples
```

队列首项必须是后端返回的第一项，并在视觉上以位置和等待时长突出；不得提供按分数、金额或“价值”排序的控件。可以按后端分页或筛选，但不得对当前响应数组重新排序。

### 5.2 API、DTO 与 UI 绑定

| 用户意图 | Endpoint | 生成 DTO | UI 行为 |
|---|---|---|---|
| 加载队列 | `GET /crm/handoffs?limit=50` | `HandoffQueueItemView[]` | 原序渲染；显示 `等待最久优先`。 |
| 读取完整接管包 | `GET /crm/handoffs/{handoff_id}` | `HandoffPacketView` | 打开/更新中间 packet 区，不由 queue 摘要重建完整包。 |
| 补充关联机会字段与 provenance | `GET /crm/opportunities/{opportunity_id}`，其中 ID 必须取自同一个 `HandoffPacketView.opportunity_id` | `OpportunityView` | 仅用于 packet 未携带的 `product_category` 与关键字段 `provenance`；禁止从 `evidence_links`、账户名或列表位置推断。两响应 ID 不一致时失败关闭并清空组合详情。 |
| 接受接管 | `POST /crm/handoffs/{handoff_id}/accept` | 无 request body；204 无响应体 | 成功后从当前列表移除并立即重新加载队列；以刷新结果为准。 |
| 失败原因分析 | `GET /crm/analytics/loss-reasons?since_days=30` | `Record<string, Record<string, number>>` | boss-only 后续分析视图；S3-19 不在队列页客户端聚合。 |

`HandoffQueueItemView` 映射：

- 身份：`handoff_id`、`opportunity_id`；
- 等待与状态：`requested_at`、`wait_seconds`、`state`；
- 触发与账户：`trigger`、`account_name`、`country`；
- 负责人：`assigned_to` 是员工 ID；列表卡片显示该 ID，不将其当成员工姓名；
- 行动上下文：`why_valuable` 与 `suggested_next_step` 同在 amber 虚线的 `推断 / 价值与建议` 区，分别标记 `价值说明` 与 `建议下一步`，明确两者都不是事实；`missing_information` 只显示条目计数；
- 证据入口：`evidence_links` 只显示条目计数，不在列表展开原始入口。列表也不展示 `customer_verbatim`，即使 DTO 带有该字段。

`HandoffPacketView` 与同机会 `OpportunityView` 组合映射：

- 基本信息：`handoff_id`、`opportunity_id`、`trigger`、`state`、`requested_at`、`wait_seconds`；
- 账户与归属：packet 的 `account_name`、`country`、`assigned_to_name`；仅完整 packet 使用 `assigned_to_name` 展示负责人姓名，它与 queue item 的 `assigned_to` 员工 ID 不可互换；
- 关联机会补充：以 packet 的 `opportunity_id` 请求 `OpportunityView`，仅从该响应读取 `product_category`、关键字段及 `provenance`；
- 来路与事实：`how_we_found_them`、`validated_need_summary`；
- 价值推断：`why_valuable` 必须位于 amber 虚线 `推断` 区并使用 `价值说明` 标签；
- 完整上下文：`customer_verbatim`、`conversation_summary`、`already_sent`、`commitments_made`；
- 下一步：`suggested_next_step` 必须位于同一 `推断` 区并使用 `建议下一步` 标签；`missing_information` 独立列出；
- 证据：`evidence_links`。

客户原话只在已通过 packet 权限检查后的详情区显示，使用 `客户原话` 明确标签；不得复制进列表卡片、错误提示或日志。`evidence_links` 只是后端给出的证据入口，不等同于完整 `ProvenanceSummary`，也不得用于推断品类或来源字段。原型中的关键字段 provenance 明确模拟来自同 `opportunity_id` 的 `GET /crm/opportunities/{id}`；任一组合请求 403 时清空全部受保护组合内容。

### 5.3 接受接管状态机

```text
未选择 ──选择队列项──> packet 加载中 ──200──> packet 就绪
                                       ├─403──> 没有权限
                                       ├─503──> 服务暂时不可用
                                       └─其他──> 无法加载接管信息

packet 就绪 ──接受接管──> 接受中
                           ├─204──> 宣告“已接受接管”
                           │         └──立即刷新队列──> 移除/更新当前项并选择下一项
                           ├─409──> “已被接受”
                           │         └──刷新队列，不重试同一写操作
                           ├─403──> “没有权限”
                           ├─503──> “服务暂时不可用，请稍后重试”
                           └─其他──> “请求未完成，请刷新后重试”
```

204 后不得读取响应 JSON。409 是并发正常结果，不显示异常文本、客户内容、actor 或 handoff ID，也不自动重复接受请求。503 若带 `Retry-After`，倒计时只用于提示何时可手工重试，禁止后台无限自动重试写操作。

## 6. 状态与安全文案

| 状态 | 机会看板 | 接管队列 | 行为 |
|---|---|---|---|
| Loading | `正在加载机会…` | `正在加载接管队列…` | 使用保持布局的 skeleton；`aria-busy="true"`。 |
| Empty | `暂无符合条件的机会` | `当前没有待接管事项` | 不把空结果描述成系统错误。 |
| 400 | `请求参数无效` | `请求参数无效` | 保留用户可修正输入；不显示后端异常原文。 |
| 403 | `没有权限` | `没有权限` | 清空受保护详情；不提供猜测性原因。 |
| 409 | `当前状态不允许此操作` | `已被接受` | 刷新对应详情/队列；接管不自动重试。 |
| 503 | `服务暂时不可用` | `服务暂时不可用，请稍后重试` | 显示手工 `重试`；尊重 `Retry-After`。 |
| Unexpected | `请求未完成，请刷新后重试` | `请求未完成，请刷新后重试` | 记录安全遥测代码时不包含 payload、客户内容、证据 URL 或凭证。 |

刷新失败时保留最近一次已成功渲染的列表，但用 `数据可能已过期` 明确标记；403 时例外，必须清空受保护内容。所有错误区域使用 `role="alert"`，但重复轮询失败不得反复抢夺屏幕阅读器焦点。

接管原型持久保存 `ready`、`loading`、`empty`、`forbidden`、`unavailable` 五种 UI 状态。任何非 `ready` 状态都禁用列表选择与接受写操作，只有显式恢复动作返回 `ready`；403 清空 packet，503 保留最近成功内容并标记过期，loading 使用保持队列卡片布局的静态 skeleton 且维持 `aria-busy="true"`。

队列 header 的 `刷新队列` 只在 `ready` 可用；它按当前后端顺序重新渲染仍存在的事项，不恢复已移除项，也不伪造网络结果。操作 live region 在正常、loading、204 与刷新成功时使用 `role="status"` / `aria-live="polite"`，在 409、403、503 与 unexpected 时动态切换为 `role="alert"` / `aria-live="assertive"`。

## 7. 事实、推断与 Provenance 边界

- `已验证事实` 仅用于后端 DTO 明确提供、且有 provenance/客户确认语义的字段；使用实线、teal 标签和事实图标。
- `推断` 用 amber、虚线边界和文字标签；包括 score explanation、价值解释或建议，不得混入事实字段容器。
- 不显示任何数值置信概率，不把 `ScoreExplanation.rank_bucket` 转成百分比、星级或“准确率”，也不以证据描述冒充排序桶。
- `ProvenancePopover` 只呈现 API 提供的摘要，不访问原始数据库、私有模型或第三方链接进行补全。
- 缺 provenance 时显示缺失状态，不生成占位来源；来源缺失不能被视觉上伪装成已确认事实。
- `customer_verbatim` 只在已授权的完整详情中显示，不进入列表、toast、错误文案、分析事件或客户端日志。

## 8. 权限、隐私与数据边界

- API 第一层 role/action gate 和机会域第二层 RBAC/ABAC 是唯一权威。前端可根据已知能力隐藏按钮，但服务端 403 必须覆盖任何客户端状态。
- 前端不得从 role header、query、localStorage 或手写 scope 推导最终权限；身份沿用既有受断言请求机制。
- 页面只消费生成的 OpenAPI public DTO，不 import/复制域内部 model、repository 或 service implementation。
- 所有示例均为 `演示数据`；不得使用真实客户、联系人、价格、认证、背书、凭证或可识别证据链接。
- 不使用 CDN、外部图片/字体、analytics、tracking 或网络素材；禁止任何 data URL 或其他内联编码资源，且不设置外部 favicon。原型在 `<head>` 同步创建本地空 SVG `Blob` object URL 作为 favicon，并在 `pagehide` 撤销该 URL。
- 任何日志与前端遥测只记录固定事件名、HTTP 状态和安全错误 code；禁止客户原话、动态 ID、证据链接、header、异常文本和 credentials。

## 9. 响应式与可访问性

### 9.1 1440 × 900

- 顶部 dark-teal 导航固定占一行；下方工作区占满剩余视口高度，document 本身不滚动。
- 机会看板主体约为 360 px 列表 + 自适应详情；详情内部再分为主记录区与 provenance 侧栏。
- 接管页主体约为 330 px 最久等待队列 + 自适应完整 packet + 286 px 操作状态栏。
- 列表、详情/packet 和操作状态各自独立滚动；ActionBar 随详情内部滚动可达且不被裁切，不要求固定在首屏。
- 长文本在所属 pane 内换行，列表项高度由内容决定，不截断状态、等待时长、下一步或缺失信息标签。

### 9.2 1180 × 800

- 机会看板约为 318–320 px 列表 + 自适应详情；详情内部主记录区保持至少约 430 px，provenance 侧栏约 250 px。
- 接管页约为 280–292 px 队列 + 至少 450 px packet + 约 240–250 px 操作状态栏。
- document 不出现纵向或水平滚动；各 pane 继续独立滚动，长文本换行，metadata 可由三列降为两列。
- ActionBar 可换行但操作顺序不变，并始终能通过详情 pane 内部滚动到达；不得被容器裁切。
- provenance 保持同一详情侧栏语义与焦点规则；宽度收窄时字段网格可降为单列。

### 9.3 语义与键盘

- 页面使用 `main`、`header`、`nav`、`section`、有序/无序列表和真实 `button`；不可用 `div` 模拟按钮。
- 列表项支持键盘选择，当前项以 `aria-current` 或等价语义标识。
- 原生 `details/summary` 可用于静态原型证据披露；Vue 实现仍须保持按钮/弹层语义。
- 所有图标有文本标签或 `aria-label`；装饰图标 `aria-hidden="true"`。
- 点击目标至少 44 × 44 px；错误和状态变化通过 `aria-live` 适度播报。
- DOM 顺序与视觉顺序一致；不得用 CSS 重排破坏阅读与 Tab 顺序。

## 10. S3-18 实现验收清单

- [ ] 页面使用生成 OpenAPI types 与类型化 client，无手写重复 `OpportunityView`/`Money`/provenance 类型。
- [ ] `GET /crm/opportunities` 的数组保持原顺序；筛选/分页不构成重新授权或分数排序。
- [ ] 列表显示 state、owner、country/category、next action/due；排序标签只绑定 `ScoreExplanation.rank_bucket`，固定文案为 `排序桶：高/中/低`，前端不重算。
- [ ] 详情完整映射 `OpportunityView`，事实与推断使用不同结构和明确标签。
- [ ] 至少一个决策字段可打开完整 `ProvenancePopover`，关闭后焦点回到触发按钮。
- [ ] `Money.amount` 作为 string 使用，不转 float、不在前端计算最终金额。
- [ ] transition 与 mark-lost 使用真实端点；提交期间两个写按钮均禁用且重复点击无效，成功后重新取数，不乐观猜测状态。
- [ ] loading、empty、400、403、409、503、unexpected 和 Retry-After 状态均有固定安全文案。
- [ ] 403 清空受保护详情；错误/日志不含客户原话、动态 ID、证据链接或异常文本。
- [ ] 所有合成记录显示 `演示数据`；无真实客户、价格、认证或私密联系人。
- [ ] 1440 × 900 与 1180 × 800 无裁切、无整页水平滚动，文本对比度至少 4.5:1。
- [ ] 键盘可完成选择、查看来源、transition/mark-lost；焦点可见且 reduced-motion 生效。

## 11. S3-19 实现验收清单

- [ ] 页面使用生成 `HandoffQueueItemView`、`HandoffPacketView` 与类型化 client，无重复域类型。
- [ ] 页面明确显示 `等待最久优先`，严格保持后端 `requested_at ASC` / `wait_seconds DESC` 顺序。
- [ ] 最老事项位于第一项；不存在按分数、价值或金额排序入口。
- [ ] 队列项显示 trigger、account、country、`assigned_to` 员工 ID；`why_valuable` 与 `suggested_next_step` 只在 amber 虚线的 `推断 / 价值与建议` 区显示；missing info 和 evidence 入口只显示计数，且不展开原始 evidence link。
- [ ] 列表不显示 `customer_verbatim`；完整客户原话只在成功取得且已授权的 packet 详情出现。
- [ ] packet 详情映射全部公共字段，列表字段/证据入口不被前端重建或伪造。
- [ ] `接受接管` 无请求体；点击时捕获选中 index 与 handoff ID、锁定列表选择，204 不解析 body，并只移除所捕获事项后聚焦下一项。
- [ ] 并发 409 固定显示 `已被接受`，同步移除所捕获事项、刷新队列且不自动重复写请求。
- [ ] `ready/loading/empty/forbidden/unavailable` 持久状态完整；非 ready 不得因列表点击恢复 packet 或写权限，只有手工恢复返回 ready；错误不泄漏动态 ID 或客户内容。
- [ ] `刷新队列` 在 ready 时仅按当前后端顺序重渲染未移除项，非 ready 禁用；live region 对正常结果 polite，对 409/403/503/unexpected 动态使用 assertive alert。
- [ ] 前端隐藏控制不被当成权限边界；API/domain gate 始终权威。
- [ ] 所有合成记录显示 `演示数据`；无外部资源、analytics、tracking 或敏感数据。
- [ ] 1440 × 900 与 1180 × 800 可读；键盘可选择事项、查看来源、接受接管，并有可见焦点。
