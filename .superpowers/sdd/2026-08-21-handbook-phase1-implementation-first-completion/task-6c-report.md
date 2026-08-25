# Task 6C 实施报告

日期：2026-08-26
基线提交：`df9e57410a10ae6e3afb182f4453df0a4c8ca34e`
初次实现：`f7cd72435cdf94ba073b7f9355136ab66d6df82e`
修复轮：本报告与修复实现同一提交；精确 SHA 在提交后 handoff 回报。

## 结论

Task 6C fix round 1/5 已按 Handbook 顺序完成受控本地闭环：浏览器先确认 discovery
proposal，production demand workflow 生成 Signal/Hypothesis/Account；浏览器随后启动 contact
discovery，verified contact 耐久停在 exact Campaign v1 的 `WAITING_EVENT`，此时数据库为零
Enrollment；老板经真实 Approval API 批准，再经真实 Campaign activation API 激活；dedicated
scheduler 的 production OutboxDeliverer 和 `AccountDiscoveryCampaignEventHandlers` 恢复同一 run，
Outreach 在行锁内复核 exact version 后创建 Enrollment。之后受控 outbound correlation 与
production `EMAIL_RAW` reply ingress 生成 Validated Need、Opportunity 和 pending Handoff。

E2E 逐 ID 证明 proposal/run、signal、hypothesis、account、enrollment、outbound/inbound message、
need、opportunity、handoff 属于同一 durable chain。Need、Opportunity、Handoff 不是 seed；唯一
seed 的 handoff 是 oldest-first 排序哨兵，不被写成 proposal/reply 的因果产物。

真实 provider/model/mail/customer/network 状态：**`not_run`**。只使用合成 `.example.test` 数据、
受控 model/search/enricher/verifier/transport；没有真实凭证、Hunter/Gmail 调用、外部发送、部署或
push。本结果不能替代真实 provider 的认证、配额、SLA 和邮件 threading 验收。

## 六项评审发现处置

1. **真实 Campaign 审批/激活：已关闭。** 删除 activation route interception 与
   `FakeApprovals` shadow path。浏览器点击真实 `/crm/campaigns/{id}/activate`；API composition
   总是以 production `_ServiceBackedCampaignApprovalProvider` 包装 fallback，使用 durable
   employee ID，真实执行 Approval permission/application/`mark_applied`。E2E 断言 approval
   最终 `applied` 且 `decided_by_employee` 为老板。
2. **逐 ID durable chain：已关闭。** Demand Radar、Customer Discovery、Campaign Center、
   Smart Inbox、Validated Need 与 Handoff Packet 增加安全 typed ID 展示；不展示隐藏 body/PII。
   PostgreSQL 断言 hypothesis.signal_ids、account、source_hypothesis、outbound correlation、
   validated_need_id、opportunity.need_id、handoff.opportunity_id 全部精确一致。
3. **approval 前 contact discovery + durable waiting：已关闭。** `account_discovery` v2 新增
   bind/wait steps；verified contact 在审批前完成并等待，零 Enrollment。metadata-only
   `CampaignStateChanged` 与 Campaign 状态同 UoW 发布；scheduler active 唤醒、cancel/revise/
   reject 取消，handler 再读 Campaign，Enrollment compare-and-create exact version。重放使用
   workflow 事件指纹和 contact-bound idempotency key。无 test endpoint、无直接 Enrollment、无
   schema migration；设计见 ADR 0015。
4. **长正文与隐私：已关闭。** raw fixture 的正文超过 500 字符，尾部含 sentinel 和 email-shaped
   marker；两者只在 immutable raw artifact bytes 中出现。模型输出和 UI 只保留 69 字符的
   `CUSTOMER_QUOTE`。测试直接扫描 workflow contexts、outbox payloads、Outreach action ledger、
   所有已验页面 body、console 与 API/Vite logs，确认不含 sentinel、email marker 或完整正文。
   报告明确区分“原始正文”和“有界客户原话摘录”。
5. **逐 surface 身份/交互：已关闭。** `/commands`、`/demand`、`/prospects/accounts`、
   `/approvals`、`/campaigns`、`/inbox`、`/demand/needs/:id`、`/crm/handoffs` 均断言 exact URL、
   `TradeOS` title、page heading、非空 body、无 Vite overlay、console 无 warning/error；每页执行
   至少一个真实交互。Inbox 做分类组过滤与 append-only 人工纠正审计；Need 用键盘展开字段级
   Provenance 且焦点保留；Handoff 用键盘选包、打开来源 dialog、验证焦点并 Escape 关闭。
6. **演示标签：已关闭。** Handoff Queue、Packet 及其 Provenance 弹层不再对真实记录硬编码
   `演示数据`；RED/GREEN frontend test 和真实 E2E 均断言缺失该标签。既有 mobile overflow
   修复保留，390×844 Need/Handoff 无横向溢出。

另外，真实浏览器 RED 暴露 `/prospects/discoveries` 的 strict Pydantic tuple 无法接收 JSON
array，导致 production API 400。公共 JSON DTO 改为 typed `list[str]`，OpenAPI 仍是 array，新增
focused regression；没有扩大接受字段，也没有放松长度/内容校验。

## TDD RED / GREEN

- Backend 首个 behavioral RED：3 failures——manual-send 直连 fallback approval、approval adapter
  使用 display name 而非 durable employee ID、Enrollment request 不能表达 Campaign version。
  最小生产修复后相关 focused suite GREEN。
- Frontend RED：6 failures——Signal/Hypothesis/Account/Enrollment/outbound/Need/Opportunity/Handoff
  ID 不完整、Need provenance 无交互、Handoff 错标演示数据；修复后 7 个 focused 文件 22 tests
  GREEN。
- E2E 首轮 rerun RED：受控 Gmail transport 漏实现 runtime-checkable `send`，fixture composition
  在任何业务动作前 fail closed；只修 fixture 协议形状。
- 第二类 E2E RED：真实 API 创建的 workflow `due_at` 与固定测试时钟不一致；改为 API run 创建后
  同步受控时钟，不改变生产代码。
- production-relevant E2E RED：`POST /prospects/discoveries` 返回 400，根因是 strict tuple/
  JSON array；增加 focused regression 后 GREEN。
- 最后 RED：把业务时钟人为推进一小时导致新 handoff 的 wait_seconds 为负并 fail closed；移除
  未来业务时间，改为仅在 API run 后同步时钟。最终 E2E `1 passed in 11.38s`。
- Frontend lint 首次 RED 为本次新增 DOM 类型的 3 个 `no-undef`；补明确 global 声明后 0 error，
  且把本次新增模板 warnings 收敛，维持基线 133 warnings。

相关安全/竞态覆盖：workflow waiting/exact activation/reject-cancel-revise/replay 5 条定向测试，
Outreach lifecycle event/idempotency 与 stale-version 门禁 5 条定向测试；scheduler 真实 PostgreSQL
装配测试 1 条验证 account_discovery v2 及 CampaignStateChanged/ApprovalDecided handler 注册。

## 真实浏览器链与安全证明

- `/commands` 的 proposal POST 仍用受控已创建 proposal（避免真实 model/network）；确认与启动
  走真实 API。其余 contact-discovery、approval 和 activation 请求均无 route interception。
- account discovery 请求体精确为 `hypothesis_id/campaign_id/role_hints/assessment_ref`；production
  steps 在 provider 成本前绑定 Campaign v1，联系人 verified 后 durable waiting，审批前零 Enrollment。
- 浏览器真实 approve 后产生 durable ApprovalDecided；真实 activate 通过 service-backed provider，
  发布 durable CampaignStateChanged。E2E 把 production account handler 注册到 fixture 正在运行的
  scheduler OutboxDeliverer，显式 drain 一次避免 1 秒周期竞态，再由 production handler 投递事件；
  最终 Outreach service 创建 exact v1 Enrollment。没有调用 test-only endpoint 或直接写行。
- reply ingress：`RawArtifactStoreImpl.put(EMAIL_RAW)` → `ConversationService.ingest_inbound` →
  production reply workflow/`ComposedReplyActionPorts`/`DurableReplyOpportunityIntake`；不是最终对象 seed。
- UI 只展示 bounded quote；raw bytes 才含完整正文/tail。所有 API/UI ID 都是业务安全标识，不包含
  联系地址、正文、凭证或 provider secret。

Playwright E2E 截图在仓库外：

```text
/private/var/folders/t2/6_w0ct0s04l68z693_h2fzkw0000gn/T/tradeos-task-6c-run_01M0WZWTZGMQC3NQ8V43HKED80/
```

包含 command confirmed、demand evidence、discovery waiting、campaign enrolled、Need mobile 和
Handoff mobile 六张截图。

## Browser 插件 QA

先尝试 Browser in-app backend；当前环境返回 unavailable，按 Browser troubleshooting 在同一运行时
选择已连接 Chrome extension，未使用独立 Playwright/Computer Use 代替插件。命名 session 为
`🧪 TradeOS Task 6C QA`，在受控本地完整栈验证：

- URL `http://127.0.0.1:59603/commands`、title `TradeOS`、heading `指挥中心`；
- viewport 1249×670、body 232 字符、无 Vite overlay；
- DOM page identity/safe banner 可见；填入 32 字受控指令后按钮由 disabled 变 enabled；未点击提交，
  因而没有 model/provider/外部动作；
- console warning/error 0。

Browser 插件截图保存在仓库外：

```text
/tmp/tradeos-task-6c-browser-plugin-fix1/commands-rendered.jpg
```

## 最终 GREEN 与门禁

- frontend ESLint：0 errors / 133 baseline warnings；本次没有新增 warning。
- frontend typecheck：通过。
- frontend Vitest：19 files / 151 tests passed（12.55s）。
- frontend build：通过，96 modules transformed。
- related backend unit：134 passed（7.46s）。
- scheduler PostgreSQL registration：1 passed（6.11s）。
- targeted mypy：12 source files，0 issues。
- changed Python Ruff check：通过；Ruff format 未作为仓库门禁声明（全文件仍有既有格式差异）。
- real PostgreSQL/Uvicorn/Vite/Chromium E2E：1 passed（11.38s）。
- `scripts/check_boundaries.py`：七项全部通过；`scripts/scan_sensitive.py` 与 `git diff --check`：exit 0。
- migration：无新增；真实 provider/model/mail/network：`not_run`；push/deploy：未执行。

## 剩余风险

1. 外部 search/model/Hunter/Gmail/sender 仍是受控 adapter；不能证明真实 provider 的认证、配额、
   SLA、成本、邮件送达和 threading。
2. E2E fixture 的基础 scheduler composition 不启用 Hunter/account discovery；测试在启动联系流程后
   把 production handler 注册到同一个真实 scheduler OutboxDeliverer。单独 PostgreSQL runtime test
   已证明正式 account composition 会自动注册 v2 definition 与两个 handler，但生产部署仍必须通过
   provider readiness 配置后才能运行真实 Hunter。
3. proposal generation POST 为受控 route fixture；确认、真实 workflow、contact-discovery、审批、
   激活和 reply ingress 均为生产边界。该选择只隔离 model/network，不宣称真实指令解析已验收。
4. Task 6D 全仓 acceptance、最终独立复审、真实部署和 push 仍为 pending。

预存未跟踪的 `apps/web/node_modules` symlink 未纳入暂存区或提交。
