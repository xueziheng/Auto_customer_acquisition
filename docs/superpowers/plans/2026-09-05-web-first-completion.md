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

用户已确认执行。本计划和配套设计已从主目录带入 `.worktrees/web-core-completion`，执行分支为 `codex/web-core-completion`，功能基线 `1b760b2`、计划带入提交 `ec801a8`。既有工作树的截图、用户改动和共享 `.git/objects` 保持独立；本轮未执行合并。

本计划的勾选按各任务实施与审查证据更新；不能把其他旧计划未勾选解释为功能未实现。后端 8741 / 前端 335 是历史验收证据，后续以实际同版本结果更新，不能累加各轮测试数量。

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

- [x] 核对分支、提交、tracked/untracked 改动和最新 migration head，只记录安全元数据。
- [x] 为指令、发现、Campaign、Inbox、Need、接管、寻源、成本报价、审批、Run、Settings 建立能力清单，每项标为已有可组合、需补组合、需新实现或本轮暂缓，并附源码和验收依据。
- [x] 为每项任务指定唯一执行者：API 即时调用、scheduler 持久步骤、Agent Worker 或 Browser Worker。没有持久任务来源的 Worker 保持 disabled，不新造队列填空。
- [x] 明确本机测试身份只能使用隔离数据与 loopback；多人共享使用须真实认证，不放宽当前非 dev 拒绝。
- [x] 在隔离工作树检查运行时版本及结构边界，记录任何现存失败；未形成干净可解释基线不开始业务改动。

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

**子规格：** `docs/superpowers/specs/2026-09-05-skill-router-contract.md`。实现与修复提交 `143983f`、`ca7827dd`、`5ab99feb`；最终聚焦测试 85 passed，ruff/mypy/结构检查通过，独立审查通过。

**Interfaces:** 实现已有 `load_registry(skills_dir: str) -> int`、`select(trigger: str, *, max_skills: int = 3) -> list[SkillManifest]`、`get(skill_id: str, version: str | None = None) -> SkillManifest`。拟新增实现名 `FileSkillRouter`。

- [x] 写测试证明精确 trigger、最高合法版本、指定历史版本和无匹配空结果。
- [x] 写拒绝测试：重复同 ID/version、缺字段、非法版本、空 eval 引用、prompt 越出允许目录、非法风险/工具值、非正 max_skills。
- [x] 运行单文件确认测试击中缺失实现，然后实现纯注册/选择；加载不联网、不调用模型、不写业务表。
- [x] 核对 schema 与 SkillManifest 的字段差异，尤其 description/upstream_ref/evals，不静默丢失会影响护栏的字段；补正式映射和兼容测试。
- [x] 运行聚焦测试、结构检查，提交本任务。

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

**子规格：** `docs/superpowers/specs/2026-09-05-context-builder-contract.md`。实现提交 `6eb0e14a`；最终聚焦测试 101 passed，ruff/mypy/结构检查通过，独立规格与质量审查通过。保留一项非阻断覆盖建议：新增适配器内部挂起点的取消用例；当前代码未发现吞掉取消的问题，原 Worker 清理已有覆盖。

**Files:**
- 更新：`agent_runtime/context_builder/builder.py`、`apps/agent_worker/main.py` 的窄装配位置。
- 新增：`agent_runtime/context_builder/service.py`、`apps/agent_worker/context_adapter.py`。
- 测试：新增 `tests/unit/test_context_builder.py`；复用 `tests/unit/test_agent_worker.py`、`tests/unit/test_guardrail_checker.py`。

**Interfaces:** 实现现有 `ContextBuilder.build(tenant_id, acting_user, task_objective, entity_refs, skill_tool_requirements, token_budget) -> BuiltContext`；适配现有 worker 的 `build(task: AgentTask) -> object`，不新建第三套 Context 类型。

- [x] 在本批子规格中定义身份映射、只读事实加载和工具授权的窄 Protocol；先固定类型，读数据只能经公开服务，不让 agent_runtime 导入 repository。
- [x] 用两个租户、两个员工的受控事实写测试：sales 只见自身授权数据，工具为权限交集；禁用项即使被技能声明仍不启用。
- [x] 写预算与输入测试：必需规则不会截断、超预算优先丢背景并记录截断、负数/布尔预算拒绝、疑似凭证在模型调用前拒绝。
- [x] 实现 builder 和 worker adapter；AgentTask 的 UserId 明确映射为受信员工，不能仅强制类型转换。
- [x] 证明 ChangeSet 仍经 guardrails/审批分流，并做 worker 失败、取消与资源清理回归；提交。

本项按受控组件口径验收：高风险 ChangeSet 交给既有 gate 端口，尚未验证真实审批落库；通用 Worker disabled，生产 policy/descriptor/模型消费者与真实审批集成均未启用。

**Exit gate:** 一个受控 AgentTask 能完成技能选择、上下文构建与受约束输出；无外部调用，无越权事实，无模型概率/金额落库。

```bash
python3 -m pytest tests/unit/test_context_builder.py tests/unit/test_agent_worker.py tests/unit/test_guardrail_checker.py -q
```

## 4. W2：运行装配与本机启动

### Task 3：必要运行工厂

本任务拆为 3a 运行生命周期与 3b 真实业务装配，均已完成并通过独立复审。3a 提交 `7c640494`、修复 `89d74bd2`，最后修复的锁与运行时回归 93 passed。3b 实现 `22e2fd34`、修复 `85a48f94`，交付文档 HEAD `da41c45d`；已接当前联系人、需求类别、回复、审批和发送身份事实，使用真实 UserId 绑定及同进程唯一服务。初始事实/身份组 50 passed、运行/发送组 161 passed、浏览器组 1 passed；修复涉及的独立聚焦组 75 passed、旧直接入口 2 passed、最后新增断言组 2 passed。各组按其源码和实际命令分别记录，不累加为全量数量。

子规格为 `docs/superpowers/specs/2026-09-05-web-core-runtime-contract.md`，公开事实与共享边界见 ADR0025。两项审查问题（Demand 缺账户事实依赖、重复指令服务）均已复现、修复并通过定向复审；静态和结构检查通过。本项完成的是运行工厂与现有流程装配，Task4 多进程启动、Task5 正文入口和 Task6 完整回复仍待执行，通用 Agent/Browser Worker 保持 disabled。

**Files:**
- 复用：`apps/api/runtime.py`、`apps/api/composition/runtime.py`、`apps/scheduler_worker/runtime.py`、`apps/scheduler_worker/main.py`。
- 新增候选：`apps/scheduler_worker/bootstrap.py`、`apps/agent_worker/bootstrap.py`；只有 Task 0 确认需要的进程才新增。
- 测试：`tests/unit/test_api_runtime.py`、`tests/unit/test_agent_worker.py`、`tests/unit/test_scheduler_worker_config.py`、`tests/integration/test_api_runtime.py`。
- 新增：`tests/integration/test_web_core_runtime.py`。

**Interfaces:** 保留 `create_runtime_app() -> FastAPI`、`main(runtime_factory=...)` 和 `SchedulerDomainDependencies`。配置由各进程入口解析，组合工厂只消费 typed settings 和端口。

- [x] 定义必须配置、可选 disabled 和配置错误三种状态；研究、联系人、发信、回复、寻源、报价分别投影可用性。
- [x] 写进程组合测试：缺字段/未知迁移/未注册步骤不能 ready；缺可选功能不影响不依赖它的只读页面。
- [x] 接入已有工厂、真实 repositories 与现有 scheduler 锁；模型经已有 StructuredJsonModelClient 注入。不得从 apps.api 导入 scheduler 或反向导入。
- [x] 验证启动失败和取消时按所属关系关闭资源；锁未取得/丢失均不推进业务。
- [x] 聚焦运行工厂与历史集成回归，提交。

### Task 4：受控启动入口与停止说明

已完成并通过独立复审。初始源码 `d9a6b03a`，修复源码 `f1632658`，交付文档 HEAD `ed1d8160`。入口启动真实独立 API、scheduler、Vite 和自有 PG/MinIO，只初始化持久员工身份；中文合成提案及 Playbook/国家政策的独立审批、调度激活经过真实 HTTP 验证。桌面和 390px 截图已查看，全新声明依赖 Python 环境实际启动与停止通过。

审查发现的“leader 在首次快照前退出会遗漏子进程”已复现并修复，使用执行前握手登记的进程组 anchor，保留业务 PID、端口和退出语义。修复后的完整 launcher 回归 22 passed；随后追加的强停用例单跑 1 passed，不计为一次 23 项总跑。初始 Web 337 passed、类型与构建通过；修复未改 Web。结构及增量扫描通过，完整敏感扫描四处旧测试形态命中、172 条基线 lint warnings 保留至 Task12 裁定。自有容器、业务进程、anchor、私有配置清理已核验；没有启用真实 Provider、桌面或共享部署。

**Files:**
- 新增：`scripts/run_web_core_controlled.py`、`docs/operations/web-core-local.md`、`tests/integration/test_web_core_launcher.py`。
- 更新：`Makefile`、`README.md`；复用已有隔离数据库、受控 transport 和测试生命周期支持。

**Interfaces:** 已实现安全入口 `python3 scripts/run_web_core_controlled.py`。只启动拥有明确资源归属的本机受控环境；默认不解析真实 Provider 凭证、不访问公网、不接管已有服务。TERM 完整停止；HUP 保留同 owner 数据和持久受控邮箱，重启应用。进程健康不代表业务前置条件通过，未装配研究执行等能力仍明确 disabled。

- [x] 先写启动/失败测试：端口占用不会杀现有进程，依赖缺失给固定错误，初始化失败清理自有资源，重复启动不会复用另一任务的数据。
- [x] 实现 loopback API/Web、独立测试数据与安全角色选择；禁止生产数据库回退或通过临时 API 伪造审批/业务结果。
- [x] 提供停止信号和完成结果；只清理持有且可核实的资源，未知清理结果返回非零并留诊断。
- [x] 用真实子进程验证启动、SIGTERM、worker 异常退出和再次启动；文档记录操作步骤，提交。

**Exit gate:** 不改源码、不手工直插业务行，可以按说明启动 Web；受控模式标识、能力缺失原因和进程健康真实可见。

## 5. W3：邮件正文到已验证需求

### Task 5：入站正文插件与不可变归档

本任务拆为5a技术读取/归档与5b耐久入库/关联/待核对入口。5a已完成并通过独立复审：初始源码`0daa7e2d`，修复源码`904a2947`，交付文档HEAD `46df91c3`。独立gic1游标、typed Gmail读取、Gateway内Raw写后实际读回、task-owned一次性交付槽与完整候选护栏均已实现。审查发现的任意codec提前解压及Date尾部歧义均已真实复现并修复，最终97项单元通过；6项真实PG/MinIO/Gateway定向组另有分轮证据。初始80项、HTTP59项、较早216项兼容组各按报告版本记录，不合并为全量总数。静态、结构和增量敏感检查通过。

5a正式契约见`docs/superpowers/specs/2026-09-05-email-inbound-5a.md`和ADR0026。它只产生技术候选页，不验证客户关联、不写Message/Need/Opportunity；`parse_inbound_content`供后续授权Raw重读复用，已解析不等于已过护栏。

5b也已完成并于2026-09-06通过独立复审，正式契约为`docs/superpowers/specs/2026-09-05-email-inbound-5b.md`。初始源码`34be4346`、取消修复`f3e2a942`、文档HEAD `a2b7a64c`。交付0059三表、整页原子提交、真实SENT关联、人工邮箱绑定、版本化原位重试和受限待核对下载；初始同版本集中回归46 passed，修复后的定向事务组12 passed，分别保留证据，不累加。提交或日志刷新中取消被关闭错误覆盖的问题已真实复现并修复，复审无未解决阻断项；下载OpenAPI二进制声明这一非阻断问题由Task8消费前补齐。

当前受控启动仍因完整回复消费者尚未装配而将`inbound_body`明确标为disabled/required_ports_missing：允许人工绑定，自动抓取与cursor推进均停止，避免入站事件死信。Task6须装配原回复消费者后自然启用；本批真实Outbox到原reply Run、独立进程重启与零推进已有验证，不表示完整分类或Web页面已完成。真实Provider仍未运行。

**Files:**
- 读取：`connectors/gmail/AGENTS.md`、`artifact_store/AGENTS.md`、`tool_gateway/AGENTS.md`、`domains/conversations/AGENTS.md`。
- 新增候选：`connectors/gmail/inbound.py`、`tool_gateway/handlers/email_inbound.py`、`workflows/reply_qualification/inbound.py`。
- 复用：`domains/conversations/service.py`、`service_impl.py`、`artifact_store/store.py` 和既有出站关联读取端口。
- 新增测试：`tests/unit/test_email_inbound.py`、`tests/integration/test_email_inbound_gateway.py`。
- 新迁移仅在持久 cursor / 去重 / 待核对事实确需新增表时创建；执行时读取最新 head，不能硬编码迁移编号。

**Interfaces:** 调用现有 `ConversationService.ingest_inbound(tenant_id, conversation_id, account_id, raw_artifact_ref, external_message_id, sent_at, *, outbound_message_id=None) -> MessageId`。新 Gateway 工具拟名 `email.inbound.fetch`，使用 typed 一次性结果槽，ledger 只存安全引用。

- [x] 先完成该批子规格：Gmail 正文读取与 feedback 的边界、cursor 提交点、页面/消息大小上限、关联规则、隔离待核对和恢复语义；只把批准的设计写成新契约。
- [x] 建受控消息矩阵：正常回复、自动回复、退订、DSN/ARF、缺 Message-ID、未知关联、跨租户关联、同 ID 异内容、超大 MIME、正文含凭证标记。
- [x] 写失败测试：重复读取不能重复 ingest；未知关联不得伪造 account/outbound；落库失败 cursor 不越过尚未持久化消息；异常不回显 MIME/地址/凭证。
- [x] 按插件点实现读取→Artifact→可信关联→ingest；不恢复 Gmail 旧 free-dict 接口，不让 feedback worker 承担正文业务。
- [x] 经真实 Gateway、PostgreSQL 和原始资料端口验证整页重放/部分失败/取消；提交。

### Task 6：回复组合、下一问和接管

**子规格与验收：** docs/superpowers/specs/2026-09-06-reply-completion-6.md；初始源码6d06a7b、Fix1源码2e0afa43，最终文档HEAD b3b1c988。独立规格与质量复审通过；初始364项主组合及119项权限/服务分组、Fix1同源码193项作用组分别记录，不累加。Outlook历史后缀误入当前表达已修；HTML void元素保守误拒与Git环境噪声保留Task12/最终核对。真实邮件/模型效果/多渠道未运行。

**Files:**
- 复用/更新：`apps/scheduler_worker/runtime.py`、`reply_actions.py`、`reply_events.py`、`adapters/reply_customer_evidence.py`、`adapters/reply_business_facts.py`、`adapters/reply_opportunity_intake.py`。
- 复用：`agent_runtime/qualification_agent/openai_port.py`、`workflows/reply_qualification/flow.py`、`steps.py`。
- 测试：`tests/integration/test_scheduler_reply_trigger.py`、`test_reply_qualification_workflow.py`、`test_conversation_reply_work_actions.py`、`test_phase1_closed_loop.py`、`tests/evals/`。

**Interfaces:** 装配现有 `ReplyQualificationComposition`，输入事件为 `InboundMessageStored`，分类结果仍由 conversations 发布 `ReplyReceived`；不改变事件时序。

- [x] 先盘点现有动作端口和下一问逻辑，只为尚未装配的路径写失败测试。
- [x] 注入受控 StructuredJsonModelClient，经真实证据 verifier 和域服务推进；禁止直接生成已验证事实或接管行。
- [x] 验证退订停止、自动回复不误停、字段逐项来源、缺少采购信息只产生下一问/待补全、达到合法门槛才接管。
- [x] 追问形成原pending工作项及只读英文建议，不宣称草稿已存或已发送；任何价格、交期等承诺仍逐次审批，经 Gateway 发送。
- [x] worker 重启、事件重投、取消后保持幂等；运行原业务eval并明确受控与真实模型结果；提交。

**Exit gate:** 测试邮件通过 Task 5 的真实入口进入，不直插 Message/Need/Opportunity；真实后端及受权boss API证明分类、需求、下一问、接管和站内投递。完整浏览器原件追溯依赖Task7的message-scoped授权和Task8页面，Task12集中验收；不提前用技术review下载替代员工原件入口。

## 6. W4：员工权限与 Web 操作

### Task 7：收件箱服务层可见范围

**Files:**
- 更新：`domains/conversations/service.py`、`schemas.py`、`service_impl.py`、`repository.py`、`infra/db/conversations_uow.py`、`infra/db/repositories/conversations.py`。
- 更新：`apps/api/identity.py`、`apps/api/routers/inbox.py`；跨域员工/负责人读取通过上层公开端口映射。
- 测试：`tests/unit/test_inbox_api.py`、`test_conversation_inbox_views.py`；新增 `tests/integration/test_inbox_access.py`。

**Interfaces:** 在 conversations 公共契约中显式传入受信访问范围，覆盖列表、详情、纠正、证据和下一问读取；沿用现有 EmployeeView / RequestIdentity。ADR0028采用同UoW/session的最小当前权限事实端口、SQL过滤与纠正事务锁，不能仅把 `_INBOX_ROLES` 加上 sales。

- [x] 先为每个动作建立 boss/manager/sales/其他角色 × 自己/直属员工/其他人/其他租户的期望矩阵。
- [x] 写拒绝与竞态测试：员工停用、归属改变、旧链接、直接 HTTP 访问均重新判权；缺归属事实不开放整租户。
- [x] 服务与 repository 实现一致过滤，API 再按安全投影返回；ADR0028明确当前事实与锁，不新增归属缓存或迁移，不由前端回传 owner 决定权限。
- [x] 生成 OpenAPI 类型，跑既有 Inbox 与 CRM 权限回归，提交。

验收：源码3090ee3、报告1aa1d99；同版本179项测试无跳过，Ruff/Mypy/结构/增量扫描/schema与TS通过，独立规格与质量审查Approved。分类窗口为最近200条已授权会话；浏览器消费与独立附件不是本批交付。纠正接口旧异常docstring的Minor由Task13同步。

### Task 8：Web 核心操作闭环

**Files:**
- 更新：`apps/web/src/views/command-center/CommandCenter.vue`、`inbox/SmartInbox.vue`、`demand-radar/ValidatedNeedDetail.vue`、`crm/HandoffQueue.vue`、`crm/HandoffPacketView.vue`、`apps/web/src/router.ts`。
- 复用：`apps/web/src/api/client.ts` 的现有身份快照/订阅；原 API client 与 Inbox 测试保留回归，新增身份切换、跨通道撤权、原件和登记测试承接实际行为，不为文件清单机械修改已有接口。
- 更新：既有 `NotificationCenter.vue`、`NotificationBadge.vue` 的本人通知请求失效与未知计数。
- 补冷启动配置：`apps/api/routers/sending_identities.py`、已有 `apps/web/src/views/SendingIdentityCenter.vue` 及 Settings 入口与生成类型；必要的发件身份公开管理读取和对应真实数据库回归。
- 复用：`tests/e2e/test_phase1_browser.py`；新增受控核心场景纳入 Task 12。

**Interfaces:** 复用 Task 7 生成的 API DTO；发件身份配置沿用域中 `register`、认证检查和 `start_warmup`，新增窄 API 也从 OpenAPI 生成类型。身份切换通过已有 identity generation 和订阅机制清空旧请求/数据。

冷启动预检发现：现有发件身份 API 只有列表、详情和认证检查，可用列表仅包含认证通过的可发送身份。要让干净环境能从 Web 完成配置，本批补老板人工确认的登记、预热入口与受限管理读取；不预插认证或可发送结果、不跳过原预热曲线。Task 4 注入受控 DNS Resolver，认证仍由原 Gateway 和工作流执行。

- [x] 确认每一项主要按钮具有真实 API 动作、明确成功结果和可访问深链；未实现动作不展示成可用按钮。
- [x] 写 deferred 请求测试：身份或路由变化后旧请求不能覆盖新页面；权限撤销即时清理受限内容，同身份跨通道旧响应也不能恢复数据。
- [x] 接通提案确认、会话证据、需求详情和接受接管；角色许可由后端决定。
- [x] 从网页登记受控发件身份、发起真实认证工作流并人工启动预热；刷新后可管理尚不可发送的身份，未知结果先核对，保留 Campaign 可用列表的原语义。
- [x] 验证桌面与 390px 下的长 ID、证据摘要、主要按钮和确认对话框，运行组件测试后提交。

验收：初始源码96c0c01，Fix1源码85b3def、报告3ae0f1e；独立复审两项403旧响应回填问题均已解决，Spec与质量Approved。真实网页登记、认证、预热、绑定及原回复流程填充后的通知→接管→Need、邮件原件下载、接受接管204已验；390px长证据遮挡已修并实看截图。原受控.test域不兼容本地PSL，仅受控解析器/示例改为tradeos-controlled.example.com，生产校验不变。后端179项组合与修复项单测、Web358项历史全量与最后增量、Fix1最终28项分别记录版本，不合成未跑的最终全量；Task12仍需统一门禁及120条lint warning归属核查。

### Task 9：错误、暂停与恢复体验

已完成并通过独立复审。初始源码 `ebb8da6`，修复源码 `1810acd`，报告 HEAD `27098ce`。Run/Campaign/Settings/Sourcing 与入站状态区分失败、待核对和既有合法恢复；原 canonical 命令保持不可变。寻源新增有界安全恢复投影（ADR0064），历史随机 HTTP header 不作为耐久权威；仅首次明确请求校验 422 可改输入，业务 400 或已有未知结果仍冻结。入站绑定、读取和 retry 共用请求 generation，旧响应不能覆盖新绑定或版本。

原后端232项、入站4项、Web九文件150项及最后纯类型后64项均分轮记录；Fix1最终六文件88项和真实ASGI有限10项通过，独立复审两项Important均ADDRESSED。真实浏览器与受控响应截图明确分栏，owned资源已清零；未冒称全仓或真实Provider验证。Settings服务内部保存后启动失败留Task12实际故障验证；测试标题Minor M1也在Task12收口。

**Files:**
- 更新：`apps/web/src/views/runs/RunCenter.vue`、`campaigns/CampaignCenter.vue`、`settings/SettingsCenter.vue`、`sourcing/SourcingRecoveryForm.vue` 中各自的请求状态与恢复操作。
- 更新对应 API 安全状态投影；没有后端恢复命令时不新增虚假的重试按钮。
- 新增：`apps/web/tests/web-core-state-recovery.test.ts`。

**Interfaces:** 保留原始 Idempotency-Key 的重试语义；401/403/404/409/503、不可恢复与待核对状态区分显示，不能把错误投影为空列表。

- [x] 写状态测试覆盖加载、空数据、权限不足、配置缺失、预算不足、pending、paused、stale、queued、reconciliation_required。
- [x] 对确定失败、可重试失败、执行结果未知分别显示已有合法操作；未知发送结果不提供直接再发。
- [x] 页面刷新重读 canonical 状态，暂停不抹掉在途事实，历史深链不存在不跳到另一业务对象。
- [x] 完成组件与相关 API 回归，提交。

**Exit gate:** 合法员工可以独立完成职责内操作；错误、等待和恢复状态可理解且与后端一致。本机角色演练不宣称已有多人登录能力。

## 7. W5：已有业务工作台与观测收口

### Task 10：寻源、成本、审批、报价跨页面连接

已完成并通过独立复审。初始源码 `8cc0419`，最终修复源码 `0890ae9`，报告 HEAD `a5a59d9`。机会→需求/成本、报价→精确成本/Run、Run→审批沿用真实ID，保留老板审计权限；来源记录不冒称已验证事实，390布局裁切已修。两轮限定修复解决从A深链新建/选择B后被重读跳回A：成功、目标缺失、503及网络失败均保留同scope目标意图，失败清数据与确认，权限/对象变化仍完整撤销。

初始122/77前端、80Python及真实Linux浏览器1项分别通过；最终修复68组件及静态通过，未冒称最终源码重跑全部旧环境。当前Mac主入口实际回复产生Need/Opportunity并验证链接，报价仍受配置与Linux解析器平台限制；另一隔离Linux owner完成公开回复前置→来源/单位/成本/两版报价/独立审批/PDF与新增链接。两套环境与实体明确分栏，不宣称统一Mac入口报价可用。Task12须同版本集中核验，13保留可运行说明与平台限制；早轮未知根因/混合测试时钟不作真实耗时或完成证明。

**Files:**
- 核对/修复：`apps/web/src/views/sourcing/`、`costing-quotes/`、`products/`、`approvals/`、`runs/` 和对应 API 路由。
- 复用：`docs/acceptance/2026-08-28-phase2-costing-quotation.md`、`2026-08-30-phase2-sourcing-case-product-cards.md`、`2026-09-02-phase2-need-cluster-sourcing-admission.md`、`2026-09-04-phase2-catalog-product-proposal.md` 中已有验收入口。

**Interfaces:** 保留现有 Need、Case、Opportunity、Cost Sheet、Quote 与 Approval ID 和状态；不得为了联调新建简化业务对象。

- [x] 从真实受控回复生成的 Need/Opportunity 开始验证既有链；价格证据走已实现的来源确认端口，不能生成虚构供应商报价。
- [x] 校验寻源准入、独立审批、indicative/quoted 边界、单位/数量变更、成本冻结和 PDF 当前授权。
- [x] 修复实际发现的断链与页面缺口；Catalog queued/stale 如实展示，未有下游消费者不显示“培养成功”。
- [x] 运行受影响子项目的现有聚焦回归，提交。本任务不重写已验收域服务。

### Task 11：Run、接管和成本输入

**Files:**
- 复用：`infra/db/run_audit.py`、机会域接管统计、Gateway 调用记录和各外部预算记录。
- 更新：`apps/api/routers/runs.py`、`apps/web/src/views/runs/RunCenter.vue`、`crm/HandoffQueue.vue`。
- 新增：`tests/integration/test_web_core_observability.py`、`docs/operations/web-core-metrics.md`。

**Interfaces:** 以 tenant + Run/Need/Opportunity 安全 ID 归因；业务金额复用 Money/Decimal，统计结果带时间窗口和数据完整性，不额外做计费钱包。

- [x] 记录各阶段唯一实体数、停滞步骤、接管深度/等待时长及模型 token/来源调用/人工耗时输入。
- [x] 写重放不重复计数、跨租户隔离、未知费用不当作零、缺费率不输出金额的测试。
- [x] 已有来源不足以计算的指标显示未知及缺项；不通过修改历史账本补造成本。
- [x] 验证 UI 能从失败 Run 定位有权限的对象，日志/通知只含安全分类和引用；提交。

**Exit gate:** 老板能知道任务停在哪里、谁应处理、有哪些已知成本和缺项；不把测试统计当真实获客成绩。

## 8. W6：集中验收与交付

### Task 12：同版本受控端到端验收

**Files:**
- 新增：`tests/e2e/test_web_core_controlled.py`、`docs/acceptance/2026-09-05-web-core-completion.md`（文件名沿计划日期，正文填写实际验收日期）。
- 复用：既有 pytest、Vitest、PostgreSQL/Browser 生命周期支持；截图使用本次独占目录。

**Interfaces:** 覆盖设计 A1–A10；测试只替换外部端口。受控完整链与 research_only 链分别验证，不能把后者自动提升为 Campaign。

- [x] 使用 Task 4 入口启动；从浏览器发起任务，按正常 API 生成审批与业务状态。
- [x] 完成受控发现、单 Provider 验证、已批 Campaign 发送、邮件回复入站、需求验证和员工接管。
- [x] 完成现有寻源/成本/报价审批/PDF 的适用场景；没有 quoted 证据且没有符合既有规则的人工风险接受记录时，验证正式报价被拦。
- [x] 注入 worker 重启、数据库短暂不可用、重复消息、旧审批、权限撤销和响应未知；核对幂等和恢复。
- [x] 停止数据库敏感的并发验收后，按同一源码版本跑完整后端与 Web 门禁；发生修复才重跑受影响范围，最终证据精确标明源码与命令。
- [x] 人工检查关键桌面/390px 截图、原始证据深链、日志脱敏和自有资源清理结果；记录真实外部调用为未运行。

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

- [x] 开始实现前再次读取本计划与设计，确认分支和部署假设没有变化。
- [ ] 每批先补该批的接口/数据迁移详细规格，再写对应失败测试；不能一次同时改七个批次。
- [ ] W0–W6 覆盖设计 A1–A10；每个测试证据归属一个真实代码版本。
- [ ] 发生跨域公共契约、事件字段或共享规则变更时先留 ADR，执行时分配最新编号。
- [ ] 不创建平行 Agent 规则入口；所有长期执行约束仍在 AGENTS.md，规格与计划只描述这次交付。
- [ ] 完成标准按批次的 Exit gate 和 A1–A10 判定，不用百分比猜测完成。

执行顺序从 **Task 0 → Task 1 → Task 2** 开始，逐批实现与审查。用户已批准本机受控范围内的实施；任务通过门禁后才勾选，不从组件测试推导已完成整条业务链。
