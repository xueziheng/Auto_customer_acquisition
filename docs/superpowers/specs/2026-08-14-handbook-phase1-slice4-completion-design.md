# Handbook Phase 1 · Slice 4 完成设计

**日期：** 2026-08-14
**状态：** 已批准，等待书面规格复核
**范围：** `HANDBOOK.md` 的 Slice 4「发件身份 + 单封手动发送」
**后续：** Slice 5–7 分别使用独立规格与实施计划，不在本设计内提前实现

## 一、目标与现状

本设计只解决 Handbook Slice 4 尚未闭合的生产链路：员工在内部界面发出一封邮件，
请求必须经过完整 Tool Gateway 管线；投递反馈与投诉必须回流并影响发件身份信誉；
SPF、DKIM、DMARC 必须由受控外部工具检查；信誉越界必须自动熔断；站内与邮件通知
必须由独立 worker 可靠投递。

仓库已经具备以下底座，必须复用而不是重写：

- 发件身份预热、认证事实、滚动信誉窗口、每日额度和熔断状态机；
- Outreach Campaign、Enrollment、MessageAttempt、抑制名单和反馈绑定；
- Tool Gateway 的租户、权限、抑制、审批、幂等和限流管线；
- Gmail 单封发送、确定性 header、只读 reconciliation 和 DSN 读取；
- PostgreSQL Outbox、工作流引擎、scheduler worker、通知路由和持久去重；
- API 正式 runtime composition、机会/接管页面和真实 Browser E2E 基础。

当前缺口是：员工发信 UI、发件身份管理 UI、站内通知、transactional email 渠道、
notification worker、DNS 认证检查、ARF 投诉解析，以及覆盖这些能力的真实 E2E。

## 二、方案选择

### 采用：按 Handbook 逐切片补缺口

保留现有深实现，先闭合 Slice 4，再依次设计和实现 Slice 5、6、7。每个小任务独立
TDD、审查、提交、push，并等待精确 commit SHA 的 CI 成功后再继续。

选择理由：它与 Handbook 的依赖顺序一致；能复用已经验证的权限、事务与持久幂等
边界；每一步都有可运行的验收证据；不会把未完成的 Campaign 或 Agent 能力伪装成
Slice 4 的一部分。

### 不采用：先做 Campaign，再回补 Slice 4

这种顺序可以更早演示自动序列，但会让自动发送建立在不完整的投诉、认证和通知边界
上。失败代价是域名信誉和重复发送风险，因此拒绝。

### 不采用：重构为统一通信平台

统一抽象 Gmail、通知、Campaign 和反馈会扩大核心管线修改面，违反四个插件点和 YAGNI。
Slice 4 只增加 Connector、Tool Handler、通知渠道与必要业务合同，不重做核心。

## 三、架构

```text
Employee Web
  → apps/api（tenant / identity / API first gate）
    → OutreachService（第二次 ABAC 与当前事实）
      → Tool Gateway（唯一外部动作出口）
        → Gmail Connector

Boss Web
  → apps/api
    → SendingIdentityService.request_authentication_check
      → Outbox → scheduler workflow
        → Tool Gateway → DNS Connector
          → typed SPF/DKIM/DMARC facts
            → SendingIdentityService.record_authentication_result

Domain Outbox
  → scheduler event handler
    → notification_job
      → notification-worker
        → NotificationRouter + durable dedup
          ├── in_app → PostgreSQL inbox
          └── Tool Gateway → transactional email → Gmail

Gmail DSN / ARF
  → email-feedback-worker
    → Tool Gateway feedback reader
      → typed feedback page
        → Outreach + SendingIdentity（同一 PostgreSQL 事务）
```

依赖继续保持：`apps → workflows → domains → shared`。领域之间不直接导入；跨域协作只
通过公开 service contract、工作流 adapter 或 shared event。任何网络访问仍只发生在
Connector 内，并且只能由 Tool Gateway handler 调用。

## 四、权限与信任边界

### 4.1 请求身份

- 浏览器只提交员工身份断言；角色、租户、scope 和 SYSTEM 身份均由服务端构造。
- 不接受来自请求头或 body 的角色、scope、租户权限或 SYSTEM actor。
- API 做第一道 typed action/scope 检查；领域服务在加载真实资源后做第二道 ABAC。
- 拒绝审计立即写入固定安全 sink；允许审计只在业务事务提交后写入。

### 4.2 手动发送

- 员工只能操作自己 scope 内的 Campaign enrollment 和 MessageAttempt。
- 发送请求必须在一次 Tool Gateway 调用中重新检查：租户、权限、抑制名单、回复状态、
  审批、发件身份认证与信誉、每日额度和持久幂等。
- `check` 结果不是授权票据；`send` 必须重新读取权威事实。
- 邮件正文和主题只能存在于请求内存、已批准内容来源和 Gmail 请求中；不得进入日志、
  Outbox、通知、异常文本或 Tool Gateway 持久 ledger。
- Gmail 结果不确定时只允许 reconciliation，不自动重发。

### 4.3 DNS 认证检查

- 只有 boss/TENANT 或获准的 manager 能创建认证检查请求。
- scheduler 使用绑定到单个 SendingIdentity 的 SYSTEM actor 记录检查结果。
- DNS Connector 只访问公开 DNS；不读取 Gmail OAuth，不执行自由 URL 或命令。
- domain、selector 和 identity 由持久配置解析并严格校验。
- DNS 原始响应不进入领域层；领域只接收 typed SPF/DKIM/DMARC 布尔事实、固定失败类别、
  UTC 检查时间和安全 check reference。

### 4.4 通知

- 员工只能读取和标记自己的通知；recipient 从服务端身份派生，不能由 body 指定。
- boss 在 Phase 1 也不能以列表接口读取其他员工通知。
- 通知记录不可变；仅 `read_at` 允许由空值单调变为 UTC 时间。
- transactional email 使用独立 `TRANSACTIONAL` 发件身份，不消耗冷开发身份额度。
- 邮件通知由 notification worker 的 SYSTEM actor 发起，并继续经过 Tool Gateway 的
  tenant、permission、idempotency 和 rate-limit 检查。

## 五、数据与状态合同

### 5.1 NotificationJob

通知任务是 scheduler 对领域 Outbox 事件的持久投影，至少包含：

```text
tenant_id / notification_job_id / source_event_fingerprint / event_type
recipient_employee_id / priority / context_kind / context_ids
dedup_key / status / available_at / attempt_count / created_at / completed_at
```

- `source_event_fingerprint` 是对事件类型与规范序列化 payload 的 SHA-256 lower-hex；
  原 payload 不进入通知表、日志或错误文本。`EventHandler` 当前拿不到 Outbox 行 ID，
  因此不得伪造或为了该字段修改 Outbox 核心。
- `(tenant_id, source_event_fingerprint, recipient_employee_id, context_kind)` 唯一。
- `context_ids` 只包含 typed 内部 ID，不包含邮件正文、邮箱地址或客户原话。
- 状态只允许 `pending → processing → completed|rejected`；暂时失败回到 `pending` 并设置
  `available_at`，租约超时可重新认领。
- notification worker 使用 tenant-filtered `FOR UPDATE SKIP LOCKED` 原子认领。

### 5.2 站内通知

```text
tenant_id / notification_id / recipient_employee_id / priority
title / context_kind / context / relative_link / source_job_id
created_at / read_at
```

- `(tenant_id, source_job_id)` 唯一；内容写入后不可编辑。
- `relative_link` 只能是应用内允许路径。
- `context` 使用按通知类型定义的固定 Pydantic schema，不接受自由字典。
- 查询始终同时过滤 tenant 和 recipient；没有 list-all 或 delete 接口。

### 5.3 AuthenticationCheckRequest

- boss 命令只创建 request 与 Outbox，不直接写认证成功。
- 状态为 `requested → running → succeeded|failed`，重复相同 request key 返回原记录。
- provider 暂时错误保留可重试状态；DNS 中不存在记录是有效失败事实。
- 只有 `record_authentication_result` 能写 SPF/DKIM/DMARC 认证事实。

### 5.4 投诉反馈

- `EmailFeedbackKind` 增加 `COMPLAINT`。
- 只解析 `multipart/report; report-type=feedback-report` 与
  `message/feedback-report` 的允许字段。
- 持久 DTO 只保留 provider digest、ordinal、COMPLAINT 分类、UTC 时间和 TradeOS 自有
  correlation header。
- 不持久化原始 MIME、Subject、客户地址、诊断文本或 provider 原文。
- 有 correlation 的 complaint 在同一事务中更新 Outreach 抑制事实与 SendingIdentity
  complaint reputation；无关联或跨租户关联进入固定 quarantine。

## 六、运行时与失败恢复

### 6.1 进程职责

- API 无状态，不运行后台轮询。
- scheduler worker 继续使用 dedicated PostgreSQL advisory lock，负责工作流和唯一 Outbox
  事件 handler。
- email-feedback-worker 使用 mailbox 级 cursor、advisory lock 和有界页面。
- notification-worker 可多副本，依靠 PostgreSQL claim，不使用全局单例锁。
- Worker 之间不互调 HTTP。

### 6.2 Outbox 与通知任务

现有 Outbox 支持同一完整 handler registry 内的 durable per-handler delivery；事件只在
该 registry 的全部 handler 完成后进入终态。但 handler registry 是进程内配置，两个进程
若各自只注册部分 handler，先领取事件的进程仍可能在另一个进程创建 delivery 前把事件
标成 `delivered`。因此 scheduler 与 notification worker 不得用两份 partial registry
争抢同一 Outbox。scheduler 的完整 registry 中注册通知投影 handler。现有
`EventHandler` 不接收 Outbox transaction session，因此投影采用有序、幂等的两次提交：
handler 先独立提交带 source-event-fingerprint 唯一键的 `notification_job`，成功返回后
Outbox 才提交该 handler 的 delivery。若进程在两次提交之间崩溃，事件重投只会命中同一 job；若 job
提交失败，handler 抛错且 delivery 保持 pending。因此不存在「delivery 已完成但 job 丢失」
窗口，也不需要修改 Outbox 核心。notification worker 只消费 notification job。渠道失败
不回滚原业务事务，也不撤销其他已经成功的渠道。

### 6.3 错误分类

- 明确未产生外部副作用的网络失败：可安全重试。
- Gmail 可能已发送但响应丢失：`reconciliation_required`，禁止自动重发。
- Gmail 401/403：永久失败并告警；等待重新授权。
- Gmail 429：仅接受 1–3600 秒的 `Retry-After`。
- DNS provider 暂时失败：有界退避；无记录或验证失败是业务结果。
- 数据库、审计、持久幂等证据失败：发送失败关闭。
- 通知渠道暂时失败：任务重试；策略拒绝：固定终态。

### 6.4 启停与健康检查

每个正式 worker 提供精确 `/health/live` 和 `/health/ready`：

- live 仅证明事件循环运行；
- ready 证明配置、唯一 Alembic head、数据库和 handler registry 可用；
- 其他路径固定 404/405，尾斜杠不重定向；
- `SIGTERM/SIGINT` 只设置 stop event，不取消在途事务或外部调用；
- 所有启动、正常退出和 cancellation 路径都关闭 HTTP transport、释放租约/锁、停止
  health task 并 dispose engine；
- 零参数或缺配置的 production factory 必须 fail closed，不使用本地默认 DSN、租户或空
  handler registry。

### 6.5 安全日志与告警

日志只允许固定中文消息以及 tenant/employee/identity/notification 等 typed ID、phase、
error category、attempt、retry_after 和 counts。禁止记录正文、邮箱、原始 MIME、OAuth、
DNS 原始响应、异常字符串和 DSN。

以下事件必须告警：认证失效、身份降级或熔断、complaint、hard bounce、Gmail
reconciliation、notification backlog、worker 锁丢失或连续失败、审计写入失败。

## 七、内部 UI

沿用现有机会看板和接管队列，不重做导航体系。

### 7.1 `/crm/outreach`

- 左栏列当前员工 scope 内的 enrollment；中栏显示客户、Campaign、可达性、回复和抑制
  事实；右栏编辑并发送单封邮件。
- 发送中锁定表单；成功显示安全状态和时间；失败使用固定 403/409/503 文案。
- 前端不缓存权限结论，不展示 provider 原始响应，不在错误 UI 回显请求 payload。

### 7.2 `/crm/sending-identities`

- 仅 boss 和有权限 manager 可访问。
- 展示身份状态、预热、今日额度、认证结果、7 日发送量、hard bounce/complaint rate、
  熔断原因和恢复条件。
- 「重新检查认证」只创建服务端请求。

### 7.3 `/notifications`

- 只显示当前员工通知；支持未读/已读、优先级、固定上下文和相对任务链接。
- 不提供 recipient 参数、list-all、删除或内容编辑。

### 7.4 Creative Production

UI 任务开始时复用既有 Creative Production board，只探索上述三个页面的信息层级、布局和
交互状态。视觉方案不决定领域权限或状态机。选定一个方向后实现 Vue 3、TypeScript、Vite
和 Ant Design Vue，并在 1440×900、1180×800 做真实页面对照。未选择视觉方案不阻塞
后端任务，但 UI 实施前必须完成选择。

## 八、测试与验收证据

每个功能先取得真实 RED，再写生产实现。

### 8.1 分层测试

- unit：权限矩阵、状态转换、DTO、错误分类和纯决策；
- PostgreSQL integration：租户过滤、事务回滚、并发 claim、dedup、Outbox 和迁移约束；
- Connector：受控 HTTP/DNS transport、协议、超时、凭证隔离和错误分类；
- worker：锁、心跳、取消、退避、健康检查和对称清理；
- ASGI integration：真实 runtime composition、双层鉴权和固定错误响应；
- Vue：loading/success/403/409/503、重复点击、过期响应竞态和焦点恢复；
- Playwright：真实 PostgreSQL、Uvicorn、Vite、Chromium，禁止 fake API；
- security：跨租户、越权、异常文本、credential marker、MIME/正文泄漏；
- migration：upgrade、downgrade、再次 upgrade 和真实数据库约束。

### 8.2 每个小任务门禁

Python 命令固定使用 conda 环境：

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH
```

每个任务至少运行 focused pytest `-W error`、Ruff、mypy、boundary、sensitive scan 和
`git diff --check`。数据库任务追加完整 integration；Web 任务追加 typecheck、lint、Vitest
和 build；用户闭环追加强制 Playwright E2E。

### 8.3 Git 与 CI

每个最小任务完成后：精确 stage allowlist、确保新文件 `100644`、清理本任务 AppleDouble、
普通 commit、不 amend、push 当前 `codex/...` 分支，并等待该精确 commit SHA 的 GitHub CI
成功。CI 未成功不得进入下一任务。

## 九、实施切分

Slice 4 使用独立计划拆为以下可审查任务：

1. notification job、站内通知合同、迁移与 PostgreSQL repository；
2. in-app/transactional-email channel 与 notification worker；
3. DNS Connector、Tool Gateway handler、认证请求与 scheduler workflow；
4. ARF complaint parser 与同事务信誉回流；
5. Outreach、SendingIdentity、Notification API；
6. 三个 Web 页面与 Creative Production 视觉实现；
7. 容器化演示、运行手册和强制 Browser E2E；
8. 真实冷开发域名与 Google Workspace 外部验收。

每项均产生独立 commit、push、精确 CI 和审查结论。任务 8 需要真实域名、邮箱和 OAuth
配置；在这些外部条件未提供前，只能声明代码与容器验收完成，不能声明 Handbook Slice 4
最终完成。

## 十、完成标准

Slice 4 只有同时满足以下条件才完成：

1. 员工能在真实 Web UI 选择可操作 enrollment 并成功发一封邮件；
2. 请求经过 API、领域 ABAC 和完整 Tool Gateway 管线；
3. SPF/DKIM/DMARC 由真实 DNS 检查并形成 typed 认证事实；
4. hard bounce 与 complaint 均能从 Gmail 回流并影响信誉窗口；
5. 超阈值会在同一领域事务中熔断发件身份，后续发送失败关闭；
6. 熔断和高优先级业务事件能可靠进入站内与 transactional email 通知；
7. 跨租户、越权、重复发送、异常泄密、worker 取消和迁移回滚测试均通过；
8. `make check`、完整 integration、Web 四门禁和真实 Browser E2E 全部通过；
9. 每个任务的精确 Git SHA CI 成功；
10. 使用真实独立冷开发域名完成发送、退信和投诉验收。

完成 Slice 4 后，再按相同设计—规格—计划—TDD—CI 流程依次完成 Handbook Slice 5、6、
7，最终执行 Handbook 第八节四项 Phase 1 完成标准。
