# Web 核心收口实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 仅在用户另行要求或适用规则明确要求时使用子代理，不默认并行改动。

**Goal:** 完成可在本机浏览器重复验收的 TradeOS Web 核心闭环，保留桌面扩展边界。

**Architecture:** 复用已有业务域、PostgreSQL 工作流、Tool Gateway 与 Vue 页面；补通用 Agent 能力、必要运行装配、邮件正文入口和员工可见范围。不同批次独立设计、实现和验收，避免形成一个不可审查的大改动。

**Tech Stack:** Python 3.12+、FastAPI、Pydantic v2、SQLAlchemy 2.x、PostgreSQL、Vue 3、TypeScript、Vite、Ant Design Vue、pytest、Vitest、Playwright；沿用项目依赖，新增依赖须在对应批次说明理由。

**Spec:** `docs/superpowers/specs/2026-09-05-web-first-completion-design.md`

## Global Constraints

- 根 `AGENTS.md` 的九条硬边界和逐次人工审批要求保持不变；进入实现目录前读所有生效的就近规则。
- 本轮先交付本机受控 Web；不实现 Tauri，不开展真实客户发送、供应商联系、采购或部署。
- 金额只用 Decimal；模型不输出最终金额和概率；事实、推断及客户表达结构分离。
- tenant、actor、scope、Provenance、审批结论、预算不能来自客户端自报或测试绕过。
- 保持 `apps → workflows / agent_runtime → domains → shared`；进程之间不得互导。
- 新外部工具使用 manifest + check + handler 插件；不往 Gateway 核心增加具体业务分支。
- 已有 Catalog 培养 Case 仍止于 queued；本轮不新增培养消费者或默认生产策略。
- 每批次保留有意义的失败测试与聚焦通过证据；全量测试在集成里程碑执行，未改代码且无新疑点时不重复跑。
- 所有新文档用中文；API 类型从 OpenAPI 生成，前端不手写重复业务 DTO。
- 本计划中的“新增路径/接口”为拟议实现，不表示文件或能力已经存在。未来批次先产出该批详细规格，再按测试推进，不凭这份总计划猜测跨域契约。

## 0. 基线与证据

实施基线选最新已验收的 Catalog 分支 `codex/phase2-catalog-product-proposal`，核对时 HEAD 为 `1b760b2`。规划开始时主目录 HEAD 为 `130dc85`，不是完整最新实现；本次规划文档的本地提交不改变该功能基线。

本计划和配套设计保存在主目录，当前未执行分支切换或合并。实施时在已核验最新基线上创建 `codex/web-core-completion` 隔离工作树，并把本任务规划文档带入；不覆盖既有工作树的未跟踪截图、用户改动或共享 `.git/objects`。

目前计划勾选只表示未来实施任务，不能把旧计划未勾选解释为功能未实现。后端 8741 / 前端 335 是历史验收证据，后续以实际同版本结果更新，不能累加各轮测试数量。

## 1. 分批交付与依赖

| 批次 | 内容 | 独立可见的交付结果 | 前置 |
| --- | --- | --- | --- |
| W0 | 固定基线、功能清单、运行与身份边界 | 明确当前版本、缺口、必要进程和测试入口 | 无 |
| W1 | 技能路由、上下文和运行护栏 | 一个受控任务能正确加载技能、裁剪数据并产生受约束 ChangeSet | W0 |
| W2 | 运行装配和本机启动 | 浏览器可访问有数据的工作台；API/必要 Worker 可启动、停止和恢复 | W0；W1 的 Agent 端口 |
| W3 | 邮件正文入站到已验证需求 | 一封受控回复穿过真实持久化流程，产生证据完整的接管包 | W1、W2 |
| W4 | 员工可见范围和 Web 操作体验 | 老板、经理、销售在各自范围内操作，错误与异步状态清晰 | W2、W3 |
| W5 | 既有寻源报价连接与运行观测 | 从需求到已有报价工作台可连续操作；Run/接管状态可定位 | W4 |
| W6 | 同版本集中验收、交付文档、桌面契约 | 可复现的 Web 核心受控版本及完整证据 | W0–W5 |

主依赖：`W0 → W1 → W2 → W3 → W4 → W5 → W6`。Web 状态盘点可提前，但不要在 API/权限契约未稳定时提前改页面。采用串行执行；无需为并行而新建共享状态或重复工厂。

不承诺基于“已完成八成”推算剩余日数。W0 后按实际任务规模估算，W3 入站关联和 W4 权限是主要不确定项。每个任务一个可审查提交；进度以交付结果统计，不以代码量或测试数量计算。

## 2. W0：固定版本与执行边界

### Task 0：基线与 Web 能力清单

**Files:**
- 读取：`README.md`、`ROADMAP.md`、`HANDBOOK.md`、`Makefile`、`.github/workflows/ci.yml`、`docs/acceptance/`。
- 读取：`apps/api/runtime.py`、`apps/api/identity.py`、`apps/scheduler_worker/runtime.py`、各 Worker `main.py`。
- 新增：`docs/operations/web-core-capability-matrix.md`。
- 更新：本计划的基线、任务状态；后续仅按真实状态同步 README/手册。

**Interfaces:** 消费 `create_runtime_app()`、`SchedulerRuntimeFactory` 和现有角色/API 契约；产出“页面动作 → API → 工作流步骤 → 域服务 → 外部能力 → 开启条件”清单。

- [ ] 核对分支、提交、tracked/untracked 改动和最新 migration head，只记录安全元数据。
- [ ] 为指令、发现、Campaign、Inbox、Need、接管、寻源、成本报价、审批、Run、Settings 建立能力清单，每项标为已有可组合、需补组合、需新实现或本轮暂缓，并附源码和验收依据。
- [ ] 为每项任务指定唯一执行者：API 即时调用、scheduler 持久步骤、Agent Worker 或 Browser Worker。没有持久任务来源的 Worker 保持 disabled，不新造队列填空。
- [ ] 明确本机测试身份只能使用隔离数据与 loopback；多人共享使用须真实认证，不放宽当前非 dev 拒绝。
- [ ] 在隔离工作树检查运行时版本及结构边界，记录任何现存失败；未形成干净可解释基线不开始业务改动。

```bash
git rev-parse HEAD
git status --short
python3 --version
node --version
python3 scripts/check_boundaries.py
git diff --check
```

解释器必须先确认 Python 3.12+；当前已验工作树有 `.venv/bin/python`，主目录未必有，不能把裸系统 Python 当作固定运行时。命令只记录版本和安全结果，不读取或打印 `.env`/DSN/密钥。

**Exit gate:** 清单每个缺口有归属与验证场景；源码基线、解释器、必要进程和本机限制一致。文档改动通过链接/路径与 whitespace 检查后单独提交。

## 3. W1：Agent 通用能力

### Task 1：技能注册与版本选择

**Files:**
- 读取/保留：`agent_runtime/skill_router/router.py`、`skills/manifests/schema.yaml`、`skills/canonical/*/manifest.yaml`。
- 新增：`agent_runtime/skill_router/service.py`、`tests/unit/test_skill_router.py`。
- 按实际新增契约更新：`agent_runtime/skill_router/router.py` 与技能 schema；原始上游 prompt 不改。

**Interfaces:** 实现已有 `load_registry(skills_dir: str) -> int`、`select(trigger: str, *, max_skills: int = 3) -> list[SkillManifest]`、`get(skill_id: str, version: str | None = None) -> SkillManifest`。拟新增实现名 `FileSkillRouter`。

- [ ] 写测试证明精确 trigger、最高合法版本、指定历史版本和无匹配空结果。
- [ ] 写拒绝测试：重复同 ID/version、缺字段、非法版本、空 eval 引用、prompt 越出允许目录、非法风险/工具值、非正 max_skills。
- [ ] 运行单文件确认测试击中缺失实现，然后实现纯注册/选择；加载不联网、不调用模型、不写业务表。
- [ ] 核对 schema 与 SkillManifest 的字段差异，尤其 description/upstream_ref/evals，不静默丢失会影响护栏的字段；补正式映射和兼容测试。
- [ ] 运行聚焦测试、结构检查，提交本任务。

拟议行为测试（`skill_registry_dir` 是本测试文件创建的受控 manifest 目录 fixture，包含 demand.infer_buyer_need 的 1.0.0 和 1.1.0，以及其受控 prompt/eval 文件）：

```python
def test_registry_selects_exact_trigger_and_preserves_old_version(skill_registry_dir):
    from agent_runtime.skill_router.service import FileSkillRouter

    router = FileSkillRouter()
    assert router.load_registry(str(skill_registry_dir)) == 2
    assert router.get("demand.infer_buyer_need").version == "1.1.0"
    assert router.get("demand.infer_buyer_need", "1.0.0").version == "1.0.0"
    assert router.select("unregistered_trigger") == []
```

```bash
python3 -m pytest tests/unit/test_skill_router.py -q
python3 scripts/check_boundaries.py
```

### Task 2：权限约束的上下文与 Worker 适配

**Files:**
- 更新：`agent_runtime/context_builder/builder.py`、`apps/agent_worker/main.py` 的窄装配位置。
- 新增：`agent_runtime/context_builder/service.py`、`apps/agent_worker/context_adapter.py`。
- 测试：新增 `tests/unit/test_context_builder.py`；复用 `tests/unit/test_agent_worker.py`、`tests/unit/test_guardrail_checker.py`。

**Interfaces:** 实现现有 `ContextBuilder.build(tenant_id, acting_user, task_objective, entity_refs, skill_tool_requirements, token_budget) -> BuiltContext`；适配现有 worker 的 `build(task: AgentTask) -> object`，不新建第三套 Context 类型。

- [ ] 在本批子规格中定义身份映射、只读事实加载和工具授权的窄 Protocol；先固定类型，读数据只能经公开服务，不让 agent_runtime 导入 repository。
- [ ] 用两个租户、两个员工的受控事实写测试：sales 只见自身授权数据，工具为权限交集；禁用项即使被技能声明仍不启用。
- [ ] 写预算与输入测试：必需规则不会截断、超预算优先丢背景并记录截断、负数/布尔预算拒绝、疑似凭证在模型调用前拒绝。
- [ ] 实现 builder 和 worker adapter；AgentTask 的 UserId 明确映射为受信员工，不能仅强制类型转换。
- [ ] 证明 ChangeSet 仍经 guardrails/审批分流，并做 worker 失败、取消与资源清理回归；提交。

**Exit gate:** 一个受控 AgentTask 能完成技能选择、上下文构建与受约束输出；无外部调用，无越权事实，无模型概率/金额落库。

```bash
python3 -m pytest tests/unit/test_context_builder.py tests/unit/test_agent_worker.py tests/unit/test_guardrail_checker.py -q
```

## 4. W2：运行装配与本机启动

### Task 3：必要运行工厂

**Files:**
- 复用：`apps/api/runtime.py`、`apps/api/composition/runtime.py`、`apps/scheduler_worker/runtime.py`、`apps/scheduler_worker/main.py`。
- 新增候选：`apps/scheduler_worker/bootstrap.py`、`apps/agent_worker/bootstrap.py`；只有 Task 0 确认需要的进程才新增。
- 测试：`tests/unit/test_api_runtime.py`、`tests/unit/test_agent_worker.py`、`tests/unit/test_scheduler_worker_config.py`、`tests/integration/test_api_runtime.py`。
- 新增：`tests/integration/test_web_core_runtime.py`。

**Interfaces:** 保留 `create_runtime_app() -> FastAPI`、`main(runtime_factory=...)` 和 `SchedulerDomainDependencies`。配置由各进程入口解析，组合工厂只消费 typed settings 和端口。

- [ ] 定义必须配置、可选 disabled 和配置错误三种状态；研究、联系人、发信、回复、寻源、报价分别投影可用性。
- [ ] 写进程组合测试：缺字段/未知迁移/未注册步骤不能 ready；缺可选功能不影响不依赖它的只读页面。
- [ ] 接入已有工厂、真实 repositories 与现有 scheduler 锁；模型经已有 StructuredJsonModelClient 注入。不得从 apps.api 导入 scheduler 或反向导入。
- [ ] 验证启动失败和取消时按所属关系关闭资源；锁未取得/丢失均不推进业务。
- [ ] 聚焦运行工厂与历史集成回归，提交。

### Task 4：受控启动入口与停止说明

**Files:**
- 新增：`scripts/run_web_core_controlled.py`、`docs/operations/web-core-local.md`、`tests/integration/test_web_core_launcher.py`。
- 更新：`Makefile`、`README.md`；复用已有隔离数据库、受控 transport 和测试生命周期支持。

**Interfaces:** 拟新增安全入口 `python3 scripts/run_web_core_controlled.py`。只启动拥有明确资源归属的本机受控环境；默认不解析真实 Provider 凭证、不访问公网、不接管已有服务。

- [ ] 先写启动/失败测试：端口占用不会杀现有进程，依赖缺失给固定错误，初始化失败清理自有资源，重复启动不会复用另一任务的数据。
- [ ] 实现 loopback API/Web、独立测试数据与安全角色选择；禁止生产数据库回退或通过临时 API 伪造审批/业务结果。
- [ ] 提供停止信号和完成结果；只清理持有且可核实的资源，未知清理结果返回非零并留诊断。
- [ ] 用真实子进程验证启动、SIGTERM、worker 异常退出和再次启动；文档记录操作步骤，提交。

**Exit gate:** 不改源码、不手工直插业务行，可以按说明启动 Web；受控模式标识、能力缺失原因和进程健康真实可见。

## 5. W3：邮件正文到已验证需求

### Task 5：入站正文插件与不可变归档

**Files:**
- 读取：`connectors/gmail/AGENTS.md`、`artifact_store/AGENTS.md`、`tool_gateway/AGENTS.md`、`domains/conversations/AGENTS.md`。
- 新增候选：`connectors/gmail/inbound.py`、`tool_gateway/handlers/email_inbound.py`、`workflows/reply_qualification/inbound.py`。
- 复用：`domains/conversations/service.py`、`service_impl.py`、`artifact_store/store.py` 和既有出站关联读取端口。
- 新增测试：`tests/unit/test_email_inbound.py`、`tests/integration/test_email_inbound_gateway.py`。
- 新迁移仅在持久 cursor / 去重 / 待核对事实确需新增表时创建；执行时读取最新 head，不能硬编码迁移编号。

**Interfaces:** 调用现有 `ConversationService.ingest_inbound(tenant_id, conversation_id, account_id, raw_artifact_ref, external_message_id, sent_at, *, outbound_message_id=None) -> MessageId`。新 Gateway 工具拟名 `email.inbound.fetch`，使用 typed 一次性结果槽，ledger 只存安全引用。

- [ ] 先完成该批子规格：Gmail 正文读取与 feedback 的边界、cursor 提交点、页面/消息大小上限、关联规则、隔离待核对和恢复语义；只把批准的设计写成新契约。
- [ ] 建受控消息矩阵：正常回复、自动回复、退订、DSN/ARF、缺 Message-ID、未知关联、跨租户关联、同 ID 异内容、超大 MIME、正文含凭证标记。
- [ ] 写失败测试：重复读取不能重复 ingest；未知关联不得伪造 account/outbound；落库失败 cursor 不越过尚未持久化消息；异常不回显 MIME/地址/凭证。
- [ ] 按插件点实现读取→Artifact→可信关联→ingest；不恢复 Gmail 旧 free-dict 接口，不让 feedback worker 承担正文业务。
- [ ] 经真实 Gateway、PostgreSQL 和原始资料端口验证整页重放/部分失败/取消；提交。

### Task 6：回复组合、下一问和接管

**Files:**
- 复用/更新：`apps/scheduler_worker/runtime.py`、`reply_actions.py`、`reply_events.py`、`adapters/reply_customer_evidence.py`、`adapters/reply_business_facts.py`、`adapters/reply_opportunity_intake.py`。
- 复用：`agent_runtime/qualification_agent/openai_port.py`、`workflows/reply_qualification/flow.py`、`steps.py`。
- 测试：`tests/integration/test_scheduler_reply_trigger.py`、`test_reply_qualification_workflow.py`、`test_conversation_reply_work_actions.py`、`test_phase1_closed_loop.py`、`tests/evals/`。

**Interfaces:** 装配现有 `ReplyQualificationComposition`，输入事件为 `InboundMessageStored`，分类结果仍由 conversations 发布 `ReplyReceived`；不改变事件时序。

- [ ] 先盘点现有动作端口和下一问逻辑，只为尚未装配的路径写失败测试。
- [ ] 注入受控 StructuredJsonModelClient，经真实证据 verifier 和域服务推进；禁止直接生成已验证事实或接管行。
- [ ] 验证退订停止、自动回复不误停、字段逐项来源、缺少采购信息只产生下一问/待补全、达到合法门槛才接管。
- [ ] 追问只创建已有合规流程允许的草稿/步骤；任何价格、交期等承诺仍逐次审批，经 Gateway 发送。
- [ ] worker 重启、事件重投、取消后保持幂等；模型/prompt 发生改动时跑对应评估并明确受控与真实模型结果；提交。

**Exit gate:** 测试邮件通过 Task 5 的真实入口进入，不直插 Message/Need/Opportunity。浏览器可追溯从原文到已验证需求及人工接管的全过程。

## 6. W4：员工权限与 Web 操作

### Task 7：收件箱服务层可见范围

**Files:**
- 更新：`domains/conversations/service.py`、`schemas.py`、`service_impl.py`、`repository.py`、`infra/db/conversations_uow.py`、`infra/db/repositories/conversations.py`。
- 更新：`apps/api/identity.py`、`apps/api/routers/inbox.py`；跨域员工/负责人读取通过上层公开端口映射。
- 测试：`tests/unit/test_inbox_api.py`、`test_conversation_inbox_views.py`；新增 `tests/integration/test_inbox_access.py`。

**Interfaces:** 在 conversations 公共契约中显式传入受信访问范围，覆盖列表、详情、纠正和证据读取；沿用现有 EmployeeView / RequestIdentity，不能仅把 `_INBOX_ROLES` 加上 sales。

- [ ] 先为每个动作建立 boss/manager/sales/其他角色 × 自己/直属员工/其他人/其他租户的期望矩阵。
- [ ] 写拒绝与竞态测试：员工停用、归属改变、旧链接、直接 HTTP 访问均重新判权；缺归属事实不开放整租户。
- [ ] 服务与 repository 实现一致过滤，API 再按安全投影返回；所需新关联通过独立 ADR/迁移落地，不由前端回传 owner 决定权限。
- [ ] 生成 OpenAPI 类型，跑既有 Inbox 与 CRM 权限回归，提交。

### Task 8：Web 核心操作闭环

**Files:**
- 更新：`apps/web/src/views/command-center/CommandCenter.vue`、`inbox/SmartInbox.vue`、`demand-radar/ValidatedNeedDetail.vue`、`crm/HandoffQueue.vue`、`crm/HandoffPacketView.vue`、`apps/web/src/router.ts`。
- 更新：`apps/web/src/api/client.ts`、`apps/web/tests/api-client-identity.test.ts`、`apps/web/tests/smart-inbox.test.ts`。
- 复用：`tests/e2e/test_phase1_browser.py`；新增受控核心场景纳入 Task 12。

**Interfaces:** 只消费 Task 7 生成的 API DTO；身份切换通过已有 identity generation 和订阅机制清空旧请求/数据。

- [ ] 确认每一项主要按钮具有真实 API 动作、明确成功结果和可访问深链；未实现动作不展示成可用按钮。
- [ ] 写 deferred 请求测试：身份或路由变化后旧请求不能覆盖新页面；权限撤销即时清理受限内容。
- [ ] 接通提案确认、会话证据、需求详情和接受接管；角色许可由后端决定。
- [ ] 验证桌面与 390px 下的长 ID、证据摘要、主要按钮和确认对话框，运行组件测试后提交。

### Task 9：错误、暂停与恢复体验

**Files:**
- 更新：`apps/web/src/views/runs/RunCenter.vue`、`campaigns/CampaignCenter.vue`、`settings/SettingsCenter.vue`、`sourcing/SourcingRecoveryForm.vue` 中各自的请求状态与恢复操作。
- 更新对应 API 安全状态投影；没有后端恢复命令时不新增虚假的重试按钮。
- 新增：`apps/web/tests/web-core-state-recovery.test.ts`。

**Interfaces:** 保留原始 Idempotency-Key 的重试语义；401/403/404/409/503、不可恢复与待核对状态区分显示，不能把错误投影为空列表。

- [ ] 写状态测试覆盖加载、空数据、权限不足、配置缺失、预算不足、pending、paused、stale、queued、reconciliation_required。
- [ ] 对确定失败、可重试失败、执行结果未知分别显示已有合法操作；未知发送结果不提供直接再发。
- [ ] 页面刷新重读 canonical 状态，暂停不抹掉在途事实，历史深链不存在不跳到另一业务对象。
- [ ] 完成组件与相关 API 回归，提交。

**Exit gate:** 合法员工可以独立完成职责内操作；错误、等待和恢复状态可理解且与后端一致。本机角色演练不宣称已有多人登录能力。

## 7. W5：已有业务工作台与观测收口

### Task 10：寻源、成本、审批、报价跨页面连接

**Files:**
- 核对/修复：`apps/web/src/views/sourcing/`、`costing-quotes/`、`products/`、`approvals/`、`runs/` 和对应 API 路由。
- 复用：`docs/acceptance/2026-08-28-phase2-costing-quotation.md`、`2026-08-30-phase2-sourcing-case-product-cards.md`、`2026-09-02-phase2-need-cluster-sourcing-admission.md`、`2026-09-04-phase2-catalog-product-proposal.md` 中已有验收入口。

**Interfaces:** 保留现有 Need、Case、Opportunity、Cost Sheet、Quote 与 Approval ID 和状态；不得为了联调新建简化业务对象。

- [ ] 从真实受控回复生成的 Need/Opportunity 开始验证既有链；价格证据走已实现的来源确认端口，不能生成虚构供应商报价。
- [ ] 校验寻源准入、独立审批、indicative/quoted 边界、单位/数量变更、成本冻结和 PDF 当前授权。
- [ ] 修复实际发现的断链与页面缺口；Catalog queued/stale 如实展示，未有下游消费者不显示“培养成功”。
- [ ] 运行受影响子项目的现有聚焦回归，提交。本任务不重写已验收域服务。

### Task 11：Run、接管和成本输入

**Files:**
- 复用：`infra/db/run_audit.py`、机会域接管统计、Gateway 调用记录和各外部预算记录。
- 更新：`apps/api/routers/runs.py`、`apps/web/src/views/runs/RunCenter.vue`、`crm/HandoffQueue.vue`。
- 新增：`tests/integration/test_web_core_observability.py`、`docs/operations/web-core-metrics.md`。

**Interfaces:** 以 tenant + Run/Need/Opportunity 安全 ID 归因；业务金额复用 Money/Decimal，统计结果带时间窗口和数据完整性，不额外做计费钱包。

- [ ] 记录各阶段唯一实体数、停滞步骤、接管深度/等待时长及模型 token/来源调用/人工耗时输入。
- [ ] 写重放不重复计数、跨租户隔离、未知费用不当作零、缺费率不输出金额的测试。
- [ ] 已有来源不足以计算的指标显示未知及缺项；不通过修改历史账本补造成本。
- [ ] 验证 UI 能从失败 Run 定位有权限的对象，日志/通知只含安全分类和引用；提交。

**Exit gate:** 老板能知道任务停在哪里、谁应处理、有哪些已知成本和缺项；不把测试统计当真实获客成绩。

## 8. W6：集中验收与交付

### Task 12：同版本受控端到端验收

**Files:**
- 新增：`tests/e2e/test_web_core_controlled.py`、`docs/acceptance/2026-09-05-web-core-completion.md`（文件名沿计划日期，正文填写实际验收日期）。
- 复用：既有 pytest、Vitest、PostgreSQL/Browser 生命周期支持；截图使用本次独占目录。

**Interfaces:** 覆盖设计 A1–A10；测试只替换外部端口。受控完整链与 research_only 链分别验证，不能把后者自动提升为 Campaign。

- [ ] 使用 Task 4 入口启动；从浏览器发起任务，按正常 API 生成审批与业务状态。
- [ ] 完成受控发现、单 Provider 验证、已批 Campaign 发送、邮件回复入站、需求验证和员工接管。
- [ ] 完成现有寻源/成本/报价审批/PDF 的适用场景；没有 quoted 证据且没有符合既有规则的人工风险接受记录时，验证正式报价被拦。
- [ ] 注入 worker 重启、数据库短暂不可用、重复消息、旧审批、权限撤销和响应未知；核对幂等和恢复。
- [ ] 停止数据库敏感的并发验收后，按同一源码版本跑完整后端与 Web 门禁；发生修复才重跑受影响范围，最终证据精确标明源码与命令。
- [ ] 人工检查关键桌面/390px 截图、原始证据深链、日志脱敏和自有资源清理结果；记录真实外部调用为未运行。

```bash
env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 TRADEOS_REQUIRE_E2E=1 python3 -m pytest tests/e2e/test_web_core_controlled.py -q -rs
python3 scripts/check_boundaries.py
python3 scripts/scan_sensitive.py
python3 -m ruff check .
python3 -m mypy domains shared tool_gateway apps workflows notification_gateway infra
env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 TRADEOS_REQUIRE_E2E=1 python3 -m pytest -q -rs
npm --prefix apps/web test
npm --prefix apps/web run typecheck
npm --prefix apps/web run lint
npm --prefix apps/web run build
npm --prefix apps/web run gen:api
git diff --exit-code -- apps/web/src/api/api.d.ts
git diff --check
```

测试命令在已核验 Python 3.12+/Node 24 环境内运行；使用项目自建隔离数据库，禁止读取生产连接。新增 E2E 必须执行，不能以 skip 的绿色退出当作通过。已有 lint warning 如仍存在，记录实际数和来源，不宣称零告警。

### Task 13：交付说明与桌面扩展契约

**Files:**
- 更新：`README.md`、`ROADMAP.md`、`HANDBOOK.md`、`docs/operations/web-core-local.md`、`docs/operations/web-core-capability-matrix.md`。
- 新增：`docs/architecture/12-client-capability-boundaries.md`。
- 不新增：Tauri 工程、桌面占位目录、未实现 IPC 路由。

**Interfaces:** Web 与未来桌面复用现有 API/权限/Artifact/Notification/Browser Protocol；只保存文档契约，不暴露密钥、Cookie 或本地绝对文件路径作为模型上下文。

- [ ] 按设计第六节逐项记录文件、凭证解析、通知、浏览器与能力发现的拥有者、权限、输入输出和失败状态。
- [ ] 更新启动/停止、重启、数据备份恢复、预算和不可用能力的说明；备份恢复仅在隔离测试数据上验证。
- [ ] 同步能力清单与真实结果；明确“本机受控 Web 完成”和“多人共享部署未验收”两个状态。
- [ ] 做文档引用/状态一致性自检，提交交付文档；合并、推送、服务器部署与真实业务启用单独记录，不从测试通过推导已执行。

## 9. 多人部署的后续门禁

若用户选择直接公司服务器使用，则把这一节前移为 W2 的硬前置，并独立拆出身份认证子规格：

1. 后端验证认证主体，建立可撤销会话，再映射为现有员工；绝不信任客户端的 role、tenant 或 authenticated 标记。
2. dev 身份入口在共享部署禁用；停用员工、退出登录、会话过期、CSRF 和来源限制有真实回归。
3. 同源部署、TLS、数据库迁移、备份恢复、服务重启、scheduler 单副本和运行告警完成验收。
4. Provider readiness、真实来源预算和逐次审批沿用既有规则。真正外部调用只有在用户明确安排真实测试后进行。

这不是桌面端任务，也不需要多租户、订阅和计费功能。

## 10. 规划自检与执行规则

- [ ] 开始实现前再次读取本计划与设计，确认分支和部署假设没有变化。
- [ ] 每批先补该批的接口/数据迁移详细规格，再写对应失败测试；不能一次同时改七个批次。
- [ ] W0–W6 覆盖设计 A1–A10；每个测试证据归属一个真实代码版本。
- [ ] 发生跨域公共契约、事件字段或共享规则变更时先留 ADR，执行时分配最新编号。
- [ ] 不创建平行 Agent 规则入口；所有长期执行约束仍在 AGENTS.md，规格与计划只描述这次交付。
- [ ] 完成标准按批次的 Exit gate 和 A1–A10 判定，不用百分比猜测完成。

建议从 **Task 0 → Task 1 → Task 2** 开始；本次交付止于规划，所有实施任务保持未勾选。
