# Slice 4 内部运营台设计规范：Evidence Ledger — Operations Desk / 证据账本·运营台

> 适用任务：Task 7 —— OutreachWorkbench（`/crm/outreach`）、SendingIdentityCenter（`/crm/sending-identities`）、NotificationCenter（`/notifications`）+ NotificationBadge
> 桌面基准：1440 × 900；最小验收宽度：1180 px
> 数据声明：原型与示例中的每条记录都必须显示「演示数据」；公司、人员、地址与标识符均为虚构，不得使用真实 PII。

## 1. 方向决策

### 1.1 选定方向：Evidence Ledger — Operations Desk（证据账本·运营台）

在 Slice 3「Evidence Ledger / 证据账本」方向之上做**连续延伸**，不进行视觉重启：三页内部运营套件与机会看板/接管队列属于同一内部运营产品，沿用同一套 palette、字体、`space.1-6`、圆角、focus、motion 与 SafeErrorBanner 规则（见 `docs/design/slice3/spec.md`，本文件只记录增量与差异）。

核心隐喻：**运营台**——左侧保持后端顺序的 ledger（记录列表），右侧详情与行动区；发送编辑器以右侧 overlay drawer 出现，不做第三个常驻栏。列表顺序、状态、数字一律来自后端生成 DTO，前端不重排、不重算、不裁决。

选择理由：
1. **source-preservation 连续性**——Slice 3 的证据先于结论、事实/推断结构分离、服务顺序即呈现顺序、安全失败四项原则直接适用；
2. 三页操作密度不同，ledger + 详情 + 行动区的固定骨架能在 1180px 下不溢出；
3. 不做新品牌、新 logo、新 claim、新数字或新 API 字段；所有 UI 文案/标签/数值/枚举确定性写入 HTML/Vue，**禁止用生成图片承载可读文字**。

### 1.2 未采用方向（与 Slice 3 相同取舍）

| 方向 | 未采用原因 |
|---|---|
| Operations Tower / 运行塔台 | 过强的监控/告警语义不适合记录与行动为主的运营台；且会诱导按状态重排 |
| 每页独立视觉系统 | 破坏同产品连续性，增加认知成本 |
| 发送编辑器常驻第三栏 | 1180px 下横向溢出；改为 440px overlay drawer |

## 2. 全局壳（三页共用）

- **顶栏**：dark-teal 顶栏复用 Slice 3 视觉；主导航含：CRM、触达工作台、发件身份、通知（当前页高亮）。
- **NotificationBadge**：位于右上「通知」入口旁：未读数字 +「未读」文本/可访问名称（不只靠红点）；`99+` 封顶仅视觉；实际列表不在前端重排。
- **键盘顺序**：导航 → 页面标题/刷新 → 主列表 → 详情操作。
- **Focus**：所有可交互元素 3px purple focus ring + 2px offset（`color.focus #7C3AED`）。
- **状态**：status 必须 icon + 文字 + 颜色三重表达，禁止只用颜色。
- **可访问性**：错误与结果使用 `role=alert` / `role=status` live region；`prefers-reduced-motion` 下禁用动效。
- **布局**：1440 内容最大宽度 1360、24px gutter；1180 用 20px gutter。高度固定视口（`100vh`），只允许 pane 内部滚动，根页面不滚动、无横向溢出。

## 3. OutreachWorkbench（`/crm/outreach`）

### 3.1 数据契约（仅生成 DTO 已有字段）

- 列表：`GET /crm/enrollments` → `EnrollmentView[]`（tenant_id/enrollment_id/campaign_id/campaign_version/account_id/contact_point_id/sending_identity_id/state/current_step/next_send_at/enrolled_at/stopped_at/stop_reason）。
- 准备：`POST /crm/enrollments/{enrollment_id}/attempts/prepare` → `MessageAttemptView`。
- 发送：`POST /crm/message-attempts/{attempt_id}/send` → `ManualEmailSendResponse`（tool_call_id/status/duplicate/provider_ref/error_category/retry_after_seconds）或 `ApiErrorResponse`（400/403/409/429/503）。

### 3.2 布局

左侧 enrollment ledger 320px（1180 为 300px）+ 右侧详情/行动区 `minmax(0,1fr)`；发送编辑器为右侧 440px overlay drawer，不做第三个常驻栏。列表保持后端顺序，不按价值/状态重排。

### 3.3 状态精确区分

- loading：「正在加载触达任务…」skeleton + `aria-busy`；
- 200 empty：「当前没有待处理的触达任务」；
- 403-empty/forbidden 合并安全呈现：「当前没有可访问的触达任务」——不猜测对象是否存在，不显示任何 ID；
- 503：「触达服务暂不可用」+ 手工刷新按钮，不自动重试写操作。

### 3.4 交互

- 选择 enrollment 后只显示生成 DTO 已有字段；**prepare 成功后才挂载编辑 drawer**。
- Drawer 内部标签中文；Subject/Body 为客户可见英文，辅助文案「发给客户的内容请使用英文」；**禁止**显示/暗示最终价格、交期、库存等绕过审批的内容。
- prepare/send 双击锁；`AbortController`/请求序号忽略 stale response。
- `Escape` 关闭 drawer、清空草稿并把焦点还给「准备发送」；成功或 403 权限变化后清空 subject/body 并关闭；503 保留草稿供手工重试。

### 3.5 错误/结果固定文案（全部安全，不回显原始 exception/客户内容/provider payload）

| 情形 | 文案 |
|---|---|
| 200 duplicate=true | 已识别为重复请求，未再次发送 |
| idempotency_conflict | 请求标识与内容不一致，请重新准备 |
| reconciliation_required | 发送状态待人工核对，请勿重复发送 |
| in_progress | 发送请求正在处理中，请稍后刷新 |
| 429 | 发送额度暂不可用（显示 Retry-After 倒计时，仅提示何时可手工重试） |
| 503 | 发送服务暂不可用，当前事实可能已变化（保留草稿） |
| 403 | 权限已变化，编辑器已关闭 |

成功后重新 `GET`，以后端事实为准。

## 4. SendingIdentityCenter（`/crm/sending-identities`）

### 4.1 数据契约（仅生成 DTO 已有字段）

- `GET /crm/sending-identities` → `IdentityView[]`（identity_id/address/domain/role/state/created_at/auth/reputation/warmup_day/warmup_complete）；`GET /crm/sending-identities/{identity_id}`；`POST .../authentication-checks`（body 仅 request_key）→ `AuthenticationCheckRequestView`。

### 4.2 布局与状态

- 卡片 ledger：1440 三列、1180 两列。
- 卡片只展示 IdentityView 已有字段：地址（boss-only）、SPF/DKIM/DMARC 文字+icon 状态、warmup_day/complete、daily_capacity、reputation 的 code-derived 展示；**不显示**原始 DNS、凭证、secret、概率或前端重算。
- 403：「当前账号无法查看发件身份」；empty：「暂无可用于 Campaign 的发件身份」。

### 4.3 交互

- 「重新检查认证」按钮：每次用户意图生成一个 `request_key`，重试复用同一个 key；request_key 不提供可编辑输入、不展示。
- 提交后只说「认证检查已提交」，不伪造实时扫描进度；用返回 DTO/重新 `GET` 显示事实。
- 双击锁、stale response 抑制、Escape 关闭详情并还焦；失败固定安全文案。

## 5. NotificationCenter（`/notifications`）+ NotificationBadge

### 5.1 数据契约

- `GET /notifications` → `InAppNotificationView[]`（notification_id/tenant_id/priority/title/context/relative_link/created_at/read_at）；`POST /notifications/{notification_id}/read` → `InAppNotificationView`。`context` 含 kind/primary_id/secondary_id/reason_code/level。

### 5.2 布局与状态

- 桌面两栏：左侧通知 ledger 380px，右侧详情/安全跳转；1180 左 340px。
- 未读必须有「未读」文字 + 粗体/边界，已读有「已读」文字，禁止只靠颜色。
- priority 使用真实枚举映射为 icon + 中文标签（urgent→紧急、normal→普通、low→低），不造新优先级。
- Badge：首次 `GET` 计算未读；失败不清零，保留最近可信计数并标「状态可能已过期」；点击进入 `/notifications`。

### 5.3 交互

- mark-read 不做猜测式 optimistic update；同一 item 写锁，接受后端返回 DTO；只有 `read_at` 从 null→datetime 时 badge 减一；stale 响应不得把已读改回未读（单调）。
- 跨收件人失败固定安全状态，不泄露 notification ID 是否存在。
- `relative_link`：只在字符串以单个「/」开头、非「//」、解析后 same-origin、`router.resolve` 有已注册 matched route 时渲染「前往处理」；否则只显示正文，不作为链接。**绝不 `v-html`，不渲染原始 HTML。**

## 6. 视觉与无障碍验收清单

- 1440×900 与 1180×800 两档截图：无横向溢出、文档不滚动（只允许 pane 内滚动）、关键状态文字不截断、focus 可见、状态非纯色。
- 每条合成记录带「演示数据」；无真实 PII；无 raw secret/DNS/HTML。
- 三页截图必须展示：outreach——选中 enrollment + 打开的发送 drawer + 一个安全 warning 状态；identities——认证/预热/容量状态卡 + auth-check 反馈；notifications——未读/已读、priority、badge、relative-link 合法/不可用两态。
- 键盘顺序：导航 → 页面标题/刷新 → 主列表 → 详情操作；Escape 关闭并还焦；`role=alert/live region`；写控件禁用态；`prefers-reduced-motion` 禁用动效。
