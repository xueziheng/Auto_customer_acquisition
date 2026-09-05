# Task3b 交付报告

- 基线：`a3fcd9fa775e9d57848eb5fa49353516c81c0da4`。
- 验收源码 / 实现 HEAD：`22e2fd344c1f02c6f676db6be6ae27915a53a850`。
- 工作树：`/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/web-core-completion`；分支 `codex/web-core-completion`。
- 本报告单独文档提交；最终交付 HEAD 由控制器回执记录，报告后的提交不修改实现。
- 控制器所有的 `docs/superpowers/plans/2026-09-05-web-first-completion.md` 修改明确未纳入本批；未修改总体 Task3 勾选或 controller ledger。

## 已实现的边界与入口

先更新正式 runtime 子规格与 ADR0025、composition_support 的四模块范围，再实现当前事实读取。
Prospecting 的 `OutreachContactFacts` 是无地址的精确 tenant/account/contact/email/法律依据/验证投影；Demand 的 `OutreachHypothesisCategories` 在 SQL 中按 tenant/account/有效状态及证据过滤，distinct 类别最多200，第201个固定失败，不借用行业或 Campaign 范围。Conversation 的 `AccountReplyStatus` 用单 SQL 当前有效分类聚合，unknown 失败关闭，后续 AUTO_REPLY 不覆盖历史真人回复，人工纠正按当前有效记录；采用明确的 account 级保守暂停。

共享库仅新增四个模块：`employee_readers.py`、`campaign_approval_reader.py`、`outreach_fact_readers.py`、`delivery_material_reader.py`。审批使用 canonical `campaign:{id}:v{version}` 和唯一 ApprovalService；sender 只读取当前 get/check_send_permission，测试证明重复读取不 reserve。材料 reader 的可信上游是原 EmailSendHandler/Gateway，先由其验证 canonical preflight/attempt；reader 逐项核 tenant/contact/account/email/sender 材料，不授权任意 Attempt，不重构或二次读取 Outreach。原 Gateway/发送门禁继续生效，地址不进入事实 DTO、模型或 Workflow。

`apps/scheduler_worker/runtime.py` 新增 frozen `SchedulerCoreServices` 与 typed `SchedulerBootstrap`（`build_base` / `build_reply`）。`apps/scheduler_worker/bootstrap.py` 提供 `CanonicalSchedulerBootstrap`、`ResearchRuntimePorts`、`ContactRuntimePorts`、`SourcingRuntimePorts`。顺序是同池 canonical core → 真实 current readers → 唯一 Outreach → 既有窄 reply → handlers/engine/outbox；Demand、SendingIdentity、Conversation 等复用原实例。直接 SchedulerDomainDependencies 路径保留，启用缺项固定拒绝，未请求组不构造外部 Provider。

API 的 `build_phase1_dependencies` / `create_runtime_app_from_settings` 增加 `gmail_transport` 外部端口 seam，可用唯一真实 core 构造 Campaign 当前事实；未启用仍保持 disabled。API 原 employee scope 同类重导出，borrowed model 与 owned 关闭链保持。scheduler main 只增加能力字段与 DTO import，没有改 Task3a 锁连接关闭、observer 或健康判据。

安全 `RuntimeCapability` DTO 只含稳定 name/status/reason，`GET /health/capabilities` 已进入 OpenAPI 并生成 `apps/web/src/api/api.d.ts`。这是各自进程的装配状态，enabled 不代表商业政策、审批、可达性或额度通过。

## 实际身份消费者

- API `/prospects/discoveries` 使用受信 EmployeeView.user_id 创建 Run；缺失拒绝。
- API `/commands/discovery-proposals/{id}/confirm` 从原决定人 `decided_by_id`（EmployeeId）读取当前 active boss 的 user_id；不让重复请求的另一老板替换执行身份，审批记录仍保留 EmployeeId。
- `BossAccountDiscoveryActorResolver` 和 `DirectiveDemandDiscoveryTaskReader` 使用当前 tenant 的唯一 active 持久 user_id 映射，再核 boss 角色；不使用同串 fallback。数据库原有 `(tenant_id,user_id)` 唯一约束仍有效。
- 仍可达的 `research_acceptance` CLI 保留 EmployeeId 输入边界，先用同一 employee scope 取得真实 UserId，再进入运行计划；其原 Playbook actor 仍为 EmployeeId。缺 user reader 的旧直接构造失败关闭。
- 手动发送 `campaigns.py` 的既有 ToolCallContext/授权器使用 EmployeeId，未悄悄改成 UserId。未重写历史 Run。
- 现有研究、后端闭环和 browser fixture 已改用持久 UserId 与显式 reader scope，浏览器真实回归通过。通知受众读取真实 active 管理者及当前归属，不是空集合常量。

## RED 与验证

测试只用私有 Python3.12 / 自有 testcontainers pgvector:pg16。所有 pytest 命令均加前缀：

`env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m pytest`

初始新事实测试 RED：`tests/integration/test_current_outreach_facts.py -q --tb=short`，3 failed，公共联系人/假设类别/回复窄读尚不存在。该文件逐步扩展，最终16项通过。不是宣称所有后续函数均独立录得 RED。

旧来源验收真实 UserId≠EmployeeId 的 RED：`tests/integration/test_research_acceptance.py::test_acceptance_reader_requires_actual_boss_active_confirmed_research -q --tb=short`，1 failed，旧强转拒绝真实老板；补当前映射后相关19项通过，并纳入下面最终50项。

最终相关验证分别运行，不累加成一条虚构的最终总数：

1. `tests/integration/test_current_outreach_facts.py tests/integration/test_research_acceptance.py tests/integration/test_research_discovery.py tests/integration/test_phase1_closed_loop.py tests/unit/test_research_acceptance.py tests/unit/test_research_acceptance_cli.py tests/unit/test_command_center_router.py -q --tb=short`：**50 passed**。覆盖真实公开写入事实、category撤销、unknown/auto/历史真人/当前纠正、单 SQL 回复读、200上限、不可达/phone、sender额度只读、审批版本拒绝、材料绑定、disabled/缺项/完整拓扑、真实 HTTP 两条 Run → worker 身份、通知租户范围及旧调用者。
2. `tests/unit/test_email_send_handler.py tests/integration/test_manual_email_send_api.py tests/integration/test_web_core_runtime.py tests/integration/test_scheduler_worker.py tests/unit/test_scheduler_quotation_activation.py tests/unit/test_scheduler_sourcing_runtime.py -q --tb=short`：**161 passed**。原邮件上游授权/门禁、Task3a lifecycle/锁健康与后续 sourcing 类型修复回归。
3. `tests/e2e/test_phase1_browser.py -q --tb=short`：**1 passed**，真实本机 PostgreSQL/Uvicorn/Vite/Chromium；仅受控外部模型/传输，无真实发送。
4. `tests/integration/test_api_runtime.py::test_runtime_accepts_exact_head_and_rejects_downgraded_schema tests/integration/test_prospecting_repositories.py tests/integration/test_need_hypotheses.py tests/integration/test_conversations_messages.py tests/integration/test_conversations_correction.py tests/integration/test_conversations_classification.py -q --tb=short`：**71 passed**，独立新容器先执行 schema downgrade 验证。

较早一次共享容器组合单列：
`tests/integration/test_current_outreach_facts.py tests/integration/test_web_core_runtime.py tests/integration/test_scheduler_worker.py tests/unit/test_scheduler_quotation_activation.py tests/unit/test_api_runtime.py tests/integration/test_api_runtime.py tests/unit/test_scheduler_worker_config.py tests/integration/test_sourcing_runtime_composition.py tests/unit/test_scheduler_sourcing_runtime.py -q --tb=short`：**202 passed, 1 failed**。失败项为上面 schema downgrade 测试；该轮 scheduler 先在共享容器写入 canonical checkpoints，0058 的 downgrade 明确拒绝删除非空 checkpoint。未修改迁移保护或清空现有库；独立新测试容器先运行该项及领域组得到上面71 passed。该次202项对应后续 acceptance/fixture身份收口前的源码，不能当作最终整组重复通过。最早 direct Hunter 依赖检查次序回归失败也已修复，恢复在解析 fingerprint 之前拒绝缺失直接依赖，由161组覆盖。

静态与生成：变更的 Python 文件 `ruff check` 通过；35个生产 Python 文件 `mypy` 通过；`scripts/check_boundaries.py` 全部通过；`git diff --check` 与 staged diff check通过。使用私有 Python 执行 web export_openapi.py 并通过现有 openapi-typescript7.13生成，现有 Node24 执行 `vue-tsc --noEmit` 通过。Git 的 T7 AppleDouble stderr 由 Python subprocess 捕获，不修复 `.git`。

## 明确未做与后续消费者

Task4 需要使用上述 typed bootstrap/feature ports 装配 Web 控制面。Task5 自动正文入站、Task6 完整回复动作未实现，`inbound_body/full_reply` 明确 disabled；Generic Agent/Browser Worker 仍 disabled。既有窄 reply 仅在完整 factory 显式提供时接线，不能宣传为自动正文同步。

所有当前事实查询只反映查询快照；原发送前重读仍负责下一道门，不承诺阻止读取之后提交的入站。没有真实 Provider、公网研究、供应商联系、真实发送、部署、merge 或 push；未读取 `.env`、已有 DSN 或真实密钥，未修改 Catalog 环境。只到本批交付，等待独立审查，不自动宣称整体 Task3或Web全链完成。
