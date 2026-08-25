# Task 6C 实施报告

日期：2026-08-26
基线提交：`df9e57410a10ae6e3afb182f4453df0a4c8ca34e`
实现提交：本报告与实现同一提交；精确 SHA 由提交后 handoff 回报（Git 提交无法在自身内容中稳定记录自身 SHA）。

## 结论

Task 6C 已完成受控本地的 Phase 1 前端与真实浏览器验收。新的 Playwright E2E 在真实
PostgreSQL、Alembic、Uvicorn、Vite 与 Chromium 上，将浏览器确认的 discovery proposal
产生的 durable Demand Signal / Need Hypothesis / Account，继续连接到浏览器启动的 verified
contact discovery、精确 Campaign v1 审批与激活、Enrollment、受控 outbound correlation，
再经 production `EMAIL_RAW` ingress 和 reply qualification composition 产生 Validated Need、
Opportunity 与 pending Handoff。最终三类对象不是直接 seed；测试逐 ID 证明 hypothesis →
need → opportunity → handoff 属于同一 durable chain。

真实 provider/model/mail/外部网络状态：**`not_run`**。本任务只使用本地合成数据和受控
search/page/model/enricher/verifier/transport；没有真实凭证、Hunter/Gmail/customer/provider
网络动作、外部发送、部署或 push。本结果不等于真实 provider 验收，也不宣称 Phase 1 已可运营。

## 既有覆盖盘点与复用

- 复用现有 `/commands`、Approval Center、Validated Need detail、Opportunity/Handoff 的
  frontend tests 与 E2E PostgreSQL/Vite/Chromium fixture，不复制其 API schema 或数据库搭建。
- `apps/web/tests/smart-inbox.test.ts` 已覆盖 route、metadata-only/raw artifact、effective category
  与 correction endpoint；本任务在原测试上增加 correction 后的 append-only audit 重读。
- 历史目标 `customer-discovery.test.ts`、`demand-radar.test.ts`、`campaign-center.test.ts` 原来不存在；
  本任务分别新增 focused behavioral coverage，并全部通过 generated OpenAPI component types 或
  真实 typed client 边界，没有手写重复 API model。
- E2E 复用 6B3 的 production workflow builders、public domain/application services、
  `RawArtifactStoreImpl`、reply composition 与 real PostgreSQL fixture。只 seed Campaign、审批、
  Playbook 和一个 oldest-first 排序哨兵等受控前置；不把它们写成由 proposal/reply 导致的结果。

## 实际 UI/API 缺口与修复

1. Demand Radar 有 URL 时隐藏了 durable `source_ref`。现在事实证据同时展示可点击来源与来源记录；
   hypothesis 仍明确显示 `推断` 和离散 confidence tier，绝不显示模型概率。
2. Campaign Center 的版本语义不足。现在可见 `不可变版本 vN`、exact approval/version、Enrollment
   的 Campaign version、pause reason，并明确“暂停只阻止新发送；入站回复仍继续处理”。
3. Smart Inbox 提交人工纠正后没有重读详情，因此 append-only correction audit 不会立即出现。
   现在成功提交后重读同一 conversation，同时保留成功提示并显示 effective category/auditor。
4. Handoff Queue 的全局 `body min-width: 1080px` 与固定三栏导致 390×844 关键页面横向溢出。
   现在移动端把 workspace、summary、facts、context 与 packet lists 收为单栏，并保持桌面布局。
5. 未新增 test-only endpoint。受控浏览器 route 只替换不可调用的真实外部 model/provider，内部仍
   调用 production proposal confirmation、workflow engine、Campaign service、account workflow
   与 reply ingress/composition 边界；浏览器 body 不包含 tenant/employee。

## TDD RED / GREEN

前端 focused RED 命令：

```text
npm run test -- --run tests/demand-radar.test.ts tests/customer-discovery.test.ts tests/campaign-center.test.ts tests/smart-inbox.test.ts
```

首次结果为 `4 files, 6 tests, 3 failed / 3 passed`：

- Demand Signal 有 URL 时不显示 artifact/source ref；
- Campaign 未显示 immutable v3 / exact approval-version / Enrollment version 与 pause 语义；
- Smart Inbox correction 后不重读 audit，页面没有 `emp-boss`。

最小修复后同命令为 `4 files, 6 passed`。

真实浏览器 E2E 的 production-relevant RED 最终出现在 Handoff Queue：390×844 下
`document.documentElement.scrollWidth <= innerWidth` 为 false，根因是全局 1080px 最小宽度与
固定三栏。移动端 CSS 修复后为 GREEN。搭建期还先发现本地 composition 缺
`SecretResolver`，该问题只修正测试依赖装配，没有放宽生产边界。

最终 E2E：

```text
TRADEOS_REQUIRE_E2E=1 .../python3 -m pytest tests/e2e/test_phase1_browser.py -q -vv
1 passed in 11.55s
```

## 真实浏览器链与安全证明

- `/commands`：浏览器填写并确认 exact proposal；confirmation route 调用 public
  `DirectiveService.confirm_proposal`，随后用 production demand workflow definition/handlers
  执行同一 proposal。receipt run ID、queue hypothesis ID 与 PostgreSQL rows 精确一致。
- `/demand`：浏览器查看事实 evidence、artifact/source ref、`推断` 与 `low_mid` 离散档位。
- `/approvals` + `/campaigns`：浏览器批准 exact Campaign v1，再通过 production
  `OutreachService.activate_campaign` 激活同一版本；真实 provider adapter 以受控本地 adapter
  替代，因此没有外部动作。
- `/prospects/accounts`：浏览器点击启动 production account workflow；请求 body 精确为
  `hypothesis_id/campaign_id/role_hints/assessment_ref` 四字段。页面查看来源 signal、
  `legitimate_interest` 与 verification state；数据库证明只有 verified contact 进入 exact
  Campaign v1 Enrollment，且 `source_hypothesis_id` 对应上述同一 hypothesis。
- system reply ingress：`RawArtifactStoreImpl.put(EMAIL_RAW)` →
  `ConversationService.ingest_inbound` → production `build_reply_qualification_handlers` +
  `ComposedReplyActionPorts` / `DurableReplyOpportunityIntake`。这是没有人工 UI endpoint 时使用的
  production 边界；不是直接 SQL 或最终对象 seed。
- `/inbox`、`/demand/needs/:needId`、`/crm/handoffs`：浏览器查看 exact effective category、
  immutable raw artifact ref、bounded customer quote、field provenance、两条 oldest-first handoff，
  用键盘选中 reply-created row、打开来源 dialog、验证焦点落在 dialog 内并用 Escape 关闭。
- 桌面为 1440×900；Need 与 Handoff 关键页面为 390×844，均断言无横向 overflow。
- 浏览器 console warning/error 为 0。UI、持久化 ID summary 以及 API/Vite stdout/stderr 均断言
  不含受控 reply body、邮箱地址或完整数据库 secret URL。

受控 E2E 截图保存在仓库外：

```text
/var/folders/t2/6_w0ct0s04l68z693_h2fzkw0000gn/T/tradeos-task-6c-run_01M0WWV6R5ZP4MDWMGM21W4G7K/
```

包含 command confirmed、demand evidence、discovery enrollment、Need mobile 与 Handoff mobile
五张截图。合成 `.example.test` 地址只存在于受控原始夹具/截图，不是真实 PII。

## Browser 插件 QA

Browser 插件状态：**available；未 fallback**。命名 session 为
`TradeOS Phase 1 Task 6C QA`，复用同一 Browser/tab binding，在本地
`http://127.0.0.1:4173/commands` 验证 URL、`TradeOS` title、`指挥中心` heading、真实表单输入、
完整页面截图与 console（0 warning/error）。Browser 插件截图保存在仓库外：

```text
/tmp/tradeos-task-6c-browser-plugin/commands-rendered.png
```

第一次连接发生在 Vite 尚未启动时，得到预期 `ERR_CONNECTION_REFUSED`；启动受控本地 Vite 后
新建同一 browser session 内的 tab 并成功完成 QA，因此不构成 Browser fallback。

## 最终 GREEN 与门禁

- frontend typecheck：通过。
- frontend ESLint：`0 errors, 133 warnings`；133 条为基线已有 warnings，本任务修改文件没有新增 warning。
- frontend Vitest：`19 files / 151 tests passed in 10.10s`。
- frontend build：通过，Vite `96 modules transformed`。
- final real PostgreSQL/Uvicorn/Vite/Chromium E2E：`1 passed in 11.55s`。
- changed Python Ruff format/check：通过。
- `scripts/check_boundaries.py`：七项全部通过。
- `scripts/scan_sensitive.py`：exit 0。
- `git diff --check`：exit 0。
- 后端公共契约未改，因此没有扩大执行与 Task 6C 无关的 backend suite；production boundaries 由
  上述真实 PostgreSQL E2E 覆盖。完整仓库 acceptance 留给 Task 6D。

## 剩余风险

1. 浏览器流程中的外部 search/model/contact enrichment/verification/sender 均为受控 adapter；真实
   provider/model/mail 仍为 `not_run`，不能证明其 SLA、配额、认证或实际邮件 threading。
2. 为保证 verified-only Enrollment，浏览器实际在 exact Campaign v1 批准/激活后启动 account
   workflow；这与 brief 中把 `/prospects/accounts` 写在 `/approvals` 前的展示顺序不同。该差异来自
   production Enrollment 对 active Campaign 的 fail-closed 前置，不应通过 test-only endpoint 或
   绕过 Campaign 状态解决；若产品必须严格采用文档顺序，应另行设计“先发现、激活后再入组”的
   production orchestration。
3. E2E 的 oldest-first 第一条是通过 public Opportunity service 创建的受控排序哨兵；它只证明
   队列顺序，不被宣称为 confirmed proposal/reply 的因果产物。第二条 handoff 及其 Need/
   Opportunity 才是 system reply ingress 生成并逐 ID 绑定的最终闭环。
4. Task 6C 不执行真实部署、push 或 6D 全仓集成/最终 review；这些状态仍为 pending。

预存未跟踪的 `apps/web/node_modules` symlink 未纳入暂存区或提交。
