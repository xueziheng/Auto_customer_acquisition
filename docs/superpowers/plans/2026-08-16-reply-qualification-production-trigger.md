# Reply Qualification InboundMessageStored Handler Contract + Composition Seam Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付 `InboundMessageStored` 消费 handler 契约与可选的 `ReplyQualificationComposition` 组合 seam：handler 按 `outbound_message_id` 精确解析被回复 attempt、校验消息行、启动 `reply_qualification` run（context 只含五个 typed ID）；runtime 在提供组合时完成定义/handler/outbox 注册。**本片不声称"生产触发完成"**：production main 尚无真实 `ReplyModelPort` 注入，实际生产进程不会因本片自动启用 reply flow（见 Global Constraints 残余清单）。

**Architecture:** 沿用 `CampaignMessagingComposition` 组合模式：`SchedulerDomainDependencies` 新增可选 `reply_qualification` 组合（typed 端口：classifier/content_reader/input_guard/conversations/outreach）。事件时序保持：`ingest_inbound` 发布 `InboundMessageStored`（消息已持久化）→ 本 handler 起 reply run → classify 落分类并发布 `ReplyReceived` → `campaign_events.on_reply_received` 停序列/唤醒 campaign（既有权责不动）。

**Tech Stack:** Python 3.12、SQLAlchemy 2.x async、PostgreSQL 16、pytest-asyncio、testcontainers（Postgres + MinIO）。

## Global Constraints

- 所有 Python 命令使用 `PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH`。
- 九条全局硬边界全部生效；本片重点：正文/凭证绝不进事件、workflow context、outbox、日志、异常（artifact_store/AGENTS 边界 4）；模型永不接触凭证（硬边界 1）；所有表/查询 tenant 过滤（硬边界 8）；依赖方向 `apps → workflows/agent-runtime → domains → shared`（硬边界 9）。
- **模型 provider/凭证边界未决（阻塞项，本片不预判）**：根硬边界写「所有对外部世界动作只能经过 tool-gateway」，但生产模型 provider 是否必须经 tool-gateway、凭证如何解析，当前仓库无实现、无 ADR。**列为后续架构审查/可能 ADR 的阻塞**，本片只在组合 seam 注入 `ReplyClassifier` 端口，不引入任何凭证路径、不声称模型调用边界。
- **生产 `ReplyModelPort` 适配器不在本片**（provider 选择、凭证解析、评估集重跑属独立切片，且受上述未决边界阻塞）。**残余**：production main（`apps/scheduler_worker/main.py` 的 `RuntimeFactory` 注入方）在没有真实 `ReplyModelPort` 适配器前不会提供 `reply_qualification` 组合 → 生产进程 reply flow 不自动启用；本片交付的是可注入的组合 seam 与 handler 契约，端到端由测试组合验证。
- `InboundMessageStored.outbound_message_id is None`、attempt 无匹配、message 行不存在/非 inbound/跨租户 → handler fail-closed（不起 run、不抛正文、只返回）。
- **测试组合只用于测试**：`SchedulerRuntimeFactory` 的 `reply_qualification` 组合由测试显式构造；生产 main 的 composition 注入方不引入 fake。
- 新文件 Git mode `100644`；不提交 AppleDouble、`.env`、凭证、构建产物。
- 单全绿逻辑 commit（**含本计划文件本身**）→ push；push 前完整门禁**前台执行，timeout >= 1500000ms，禁止 run_in_background**；push 后监控 exact-HEAD GitHub Actions CI success 才交付。
- 每个任务：RED（本地跑不提交）→ GREEN → focused pytest `-W error` → Ruff → mypy → `scripts/check_boundaries.py` → `scripts/scan_sensitive.py` → `git diff --check` → commit → push → exact-HEAD CI。

## File and Interface Map

```text
apps/scheduler_worker/reply_events.py              新增：ReplyQualificationEventHandlers（EventHandler）
apps/scheduler_worker/runtime.py                   修改：ReplyQualificationComposition + SchedulerDomainDependencies.reply_qualification + _resources 装配
workflows/reply_qualification/flow.py              修改：仅 docstring（陈旧触发声明 → 诚实描述 InboundMessageStored + 可选组合 seam）；结构/接口不变
shared/events/catalog.py                           修改：仅 ReplyReceived docstring（删除「qualification_agent 用它触发分类」订阅说明）；事件字段/结构不变
infra/db/outbox_delivery.py                        只读：register_handler 已存在，不改
tests/integration/test_scheduler_reply_trigger.py  新增：集成测试（真实 PostgreSQL + MinIO）
docs/superpowers/plans/2026-08-16-reply-qualification-production-trigger.md  本计划文件（与代码同一全绿 commit）
```

`ReplyQualificationComposition`（runtime.py，frozen dataclass）字段与运行时校验：

```text
classifier: ReplyClassifier            （agent_runtime.qualification_agent.agent）
content_reader: MessageContentReader   （workflows.reply_qualification.ports）
input_guard: InputContentGuard         （workflows.reply_qualification.ports）
conversations: ConversationService     （domains.conversations.service）
outreach: OutreachService              （domains.outreach.service）
```

`ReplyQualificationEventHandlers`（reply_events.py）接口：

```text
__init__(*, engine: WorkflowEngine, factory: async_sessionmaker[AsyncSession], tenant_id: TenantId)
async def handle(self, event: object) -> None        # 仅接受 InboundMessageStored，未知类型 ValidationError
async def on_inbound_message_stored(self, event: InboundMessageStored) -> None
```

`scheduler runtime._resources` 装配（当 `dependencies.reply_qualification is not None`）：

```text
register_reply_qualification(workflow)
workflow_handlers 并入 build_reply_qualification_handlers(
    classifier=composition.classifier, content_reader=composition.content_reader,
    input_guard=composition.input_guard, conversations=composition.conversations,
    outreach=composition.outreach, tenant_id=config.tenant_id, now=self._now)
outbox.register_handler(InboundMessageStored, "reply_qualification.inbound_stored", reply_events)
```

---

### Task 1: InboundMessageStored handler contract + optional composition seam

**Files:**
- Create: `apps/scheduler_worker/reply_events.py`
- Modify: `apps/scheduler_worker/runtime.py`
- Create: `tests/integration/test_scheduler_reply_trigger.py`

**Handler 契约（写进代码 docstring 与测试断言）：**

1. `on_inbound_message_stored(event)` 顺序：
   - **租户校验（第一条）**：`event.tenant_id != self._tenant_id` → 直接返回（fail-closed，不依赖绑定 tenant 的查询兜底）。
   - `event.outbound_message_id is None` → 直接返回（fail-closed）。
   - **消息行校验**：按 `MessageRow.tenant_id == str(tenant_id)` 且 `message_id == str(event.message_id)` 查行（经 `infra.db.tables.MessageRow` tenant 过滤只读查询，沿用 `campaign_events.py` 的 `OutreachMessageAttemptRow` 查询先例，apps → infra 合法）；行不存在 → 返回；`direction != "inbound"` → 返回。
   - **持久化关联一致性校验**：`row.outbound_message_id` 必须**非空且与 `str(event.outbound_message_id)` 完全一致**，否则 → 返回（把真实 inbound message_id 与另一个 attempt 的 outbound id 拼接的事件必须 fail-closed，防止错误 enrollment/contact 被分类和抑制）。
   - 按 `OutreachMessageAttemptRow.tenant_id == str(tenant_id)` 且 `deterministic_message_id == str(event.outbound_message_id)`（即经一致性校验后的持久化关联值）查 attempt；无匹配 → 返回。
   - 由 attempt 的 `enrollment_id` 查 `OutreachEnrollmentRow`（同 tenant）取 `account_id`、`contact_point_id`；缺失 → 返回。
   - `engine.start(tenant_id, "reply_qualification", subject_ref=str(event.message_id), initial_context={...}, idempotency_key=f"reply:{event.message_id}")`。
2. **context 五个 typed ID 及逐一依据（最小披露）**：
   ```text
   message_id           classify 的 subject_ref 与幂等键；ApplyActionsStep 的 source_ref 前缀
   outbound_message_id  ClassifyStep → conversations.record_classification 的 outbound_message_id
                        （ReplyReceived 出站关联契约，steps.py:156 实证消费）→ 必需，保留
   enrollment_id        ApplyActionsStep._stop_sequence（stop_enrollment 目标）
   account_id           ApplyActionsStep._suppress 校验（同 conversation 归属）
   contact_point_id     ApplyActionsStep._suppress 目标（SuppressionTarget 唯一资源）
   ```
   无 category、无正文、无 artifact 引用、无其他键；测试对 `run_row.context` 键集做精确断言。
3. `runtime._resources` **装配顺序（精确）**：
   1. 先按可选 `dependencies.reply_qualification` 构造 `reply_handlers = build_reply_qualification_handlers(...)`（仅当组合提供时）；
   2. 将 `reply_handlers` 与 handoff/auth/campaign handlers **一并传入 `PostgresWorkflowEngine(factory, {**handoff_handlers, ..., **campaign_handlers, **reply_handlers}, now=...)` 构造器**（引擎构造后 `_handlers` 即固化，不能再并入 handler dict）；
   3. 引擎构造后 `register_reply_qualification(workflow)`；
   4. 最后 `outbox.register_handler(InboundMessageStored, "reply_qualification.inbound_stored", reply_events)`。
   未提供组合时以上 1/3/4 完全不执行（scheduler 行为不变）。**测试组合仅测试**：生产 main 的 composition 注入方不引入 fake。

**RED 测试（tests/integration/test_scheduler_reply_trigger.py，真实 PostgreSQL + MinIO）：**

测试组合复用 `test_scheduler_campaign_driver.py` 的 harness 模式（`SchedulerRuntimeFactory` + `_composition` + `_poll`）+ 上一片 `ArtifactMessageContentReader` 真实实现 + MinIO fixture（复制 `test_artifact_store_minio.py` 的 `minio_runtime`/`_stores` 模式）。

- **reply 业务链测试替身边界**：content reader/conversations/outreach/artifact store **一律真实**——`ArtifactMessageContentReader`（经 `infra.db.artifact_uow.SqlAlchemyArtifactUnitOfWork` + `S3ObjectBlobTransport`，limits 显式：`max_raw_bytes=1024*1024, max_subject_chars=200, max_body_chars=65536`）、`CredentialMarkerGuard`、真实 `ConversationServiceImpl`/`SqlAlchemyConversationsUnitOfWork`、真实 `OutreachServiceImpl`；模型调用允许 fake `ReplyModelPort`（`QualificationAgent(model="reply-scheduler-test-v1", model_client=_FakeModelPort("unsubscribe"), gateway=None, guardrails=None)`）。
- **runtime 外围按现有 harness 可用受控测试适配器**：沿用 `test_scheduler_campaign_driver.py` 的 `_Resolver`/`_HealthServer`/`_Transport`/`FakeSenders` 等（它们服务 DNS/健康检查/通知/Gmail 外围，不进入 reply 业务链）。
- 种子：sending identity（`_seed_identity` 模式）、campaign、enrollment、一个带 `deterministic_message_id` 的 attempt（`campaign_driver.scan_once()` 发送一封得到真实 attempt 行）；`email_raw` artifact 经真实 store put（RFC822 bytes）→ `conversations.ingest_inbound(raw_artifact_ref=<该 artifact>, outbound_message_id=<attempt.deterministic_message_id>)`。
- `_reply_composition(...)` 显式标注 test-only。

测试用例：
1. `test_inbound_stored_starts_reply_run_and_applies_actions`：真实链 ingest→outbox→handler→workflow→reader→guard→QualificationAgent(fake model)→record_classification→apply_actions。断言：**run 启动后、推进前**（第一次 `outbox.drain()` 后、`_poll` 前）查 `run_row`，初始 context 键集精确等于五个 typed ID；**完成后** context 键集精确等于五个 typed ID ∪ `{category, actions}`（ClassifyStep 确定性 patch，属已知内部键）；恰一条 `reply_qualification` run 且 `status == "completed"`；`enrollment.state == "replied"`；恰一条 suppression（unsubscribe）；`InboundMessageStored` payload 键集精确等于 `{tenant_id, occurred_at, run_id, message_id, outbound_message_id}`；context/outbox/异常无正文与凭证 marker。
2. `test_inbound_stored_without_outbound_correlation_fails_closed`：`outbound_message_id=None` 的真实 ingest → `_poll` → 无 `reply_qualification` run、enrollment 保持 `in_sequence`、无 suppression。
3. `test_inbound_stored_unknown_outbound_fails_closed`：`outbound_message_id=<不存在的 id>` → 无 run、无副作用。
4. `test_inbound_stored_missing_or_forged_message_fails_closed`：五个独立子断言，每个都断言 0 run / 0 副作用（enrollment 不变、无 suppression、无分类）：
   - **事件 tenant 不匹配**：`event.tenant_id != self._tenant_id`（构造跨租户事件对象直投 handler）→ fail-closed；
   - **消息不存在**：`message_id` 无对应 `MessageRow` → fail-closed；
   - **direction 非 inbound**：`MessageRow.direction == "outbound"` → fail-closed；
   - **持久化关联与事件不匹配**：真实 inbound 行（`row.outbound_message_id = attempt A`），事件拼接**另一条真实 attempt B** 的 outbound id（`row.outbound_message_id != event.outbound_message_id`，两条 attempt 均来自真实 campaign 双 enrollment 发送）→ fail-closed（防止错误 enrollment/contact 被分类和抑制）；
   - **消息仅存在于另一 tenant**：另一 tenant 经真实 `ingest_inbound` 落库得到真实 `other_message_id`，事件携带该真实 id 投递到本 tenant handler（本 tenant 无该行）→ fail-closed。
5. `test_reply_trigger_consumer_redelivery_is_idempotent`：**直接对同一事件重复调用 handler（或重复投递同一 outbox 事件）两次** → `engine.start` 同幂等键返回同一 run：恰一条 run 行、恰一次分类、恰一条 suppression、`ReplyReceived` outbox 事件恰一条。
6. `test_reply_trigger_does_not_retrigger_on_reply_received`：**对象级断言**：reply run 完成后，outbox 中 `ReplyReceived` 恰一条且 `InboundMessageStored` 消费次数为 1；`campaign_events.on_reply_received` 对该 `ReplyReceived` 只停一次序列（enrollment.state 仍为 `replied`，无新 run）——不依赖「既有 e2e 全绿」推断，直接断言事件计数与单 run。

**GREEN 实现（Task 1）：**

- `reply_events.py`：按上述契约实现；只捕获明确异常路径，异常为固定安全摘要（ValidationError「回复触发事件无效」等），绝不回显正文/地址/凭证。
- `runtime.py`：
  - 新增 `ReplyQualificationComposition` frozen dataclass + `__post_init__` 按 reply 链实际消费能力校验（`SchedulerDomainDependencies` 同款 callable 存在性风格：`classifier.classify`/`classifier.model`（非空 str，classified_by 留痕）/`content_reader.load`/`input_guard.check`/`conversations.record_classification`/`outreach.stop_enrollment`/`outreach.add_suppression`——`ingest_inbound` 是入站已发生的事实，非 runtime/workflow 消费能力，不校验），不做整份 Protocol isinstance——浅域实现可能只实现部分方法。
  - `SchedulerDomainDependencies` 增 `reply_qualification: ReplyQualificationComposition | None = None` + `__post_init__` 校验。
  - `_resources`：在 `campaign_outreach` 装配之后，`if self._dependencies.reply_qualification is not None:` **先构造 `reply_handlers = build_reply_qualification_handlers(...)`，把 `reply_handlers` 与 handoff/auth/campaign handlers 一起作为 `PostgresWorkflowEngine` 构造器的 `handlers` 参数传入**；引擎构造后再 `register_reply_qualification(workflow)` 注册定义，最后 `outbox.register_handler(InboundMessageStored, "reply_qualification.inbound_stored", handler)`。
- `shared/events/catalog.py`：仅改 `ReplyReceived` docstring——删除「订阅方：``agent_runtime/qualification_agent``（分类与追问）」声明（ReplyReceived 是分类落库后的结果事件，不触发分类；订阅方是 outreach 停序列与 campaign 唤醒）；事件字段/结构零改动。
- `workflows/reply_qualification/flow.py`：仅改模块 docstring——删除「生产触发在本切片未接线」「由摄取 connector 接线时新增」陈旧声明，改为诚实描述：触发事件 `InboundMessageStored` 已存在，本切片提供可选 `ReplyQualificationComposition` seam（scheduler 提供组合时装配），但 production main 尚未注入真实 provider（`ReplyModelPort` 适配器未决）；函数签名/结构零改动。
- 不改 `campaign_events.py`、生产 main 的 composition 注入方。

**验证命令（每个 commit 前，全部前台、timeout >= 1500000ms、pipefail 真实 rc）：**

```bash
export PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH
find . -name "._*" -not -path "./.git/*" -delete
# RED 阶段：仅跑新测试，确认失败（本地，不提交）
python -m pytest tests/integration/test_scheduler_reply_trigger.py -q -W error
# GREEN 后定向回归
python -m pytest tests/integration/test_scheduler_reply_trigger.py \
  tests/integration/test_scheduler_campaign_driver.py \
  tests/integration/test_conversations_messages.py \
  tests/integration/test_reply_qualification_workflow.py \
  tests/integration/test_conversations_classification.py \
  tests/integration/test_message_content_reader.py \
  tests/integration/test_migrations.py \
  tests/integration/test_outbox_delivery.py \
  tests/unit/test_outbox_serialization.py -q -W error
# docstring 改动（catalog/flow）随 full non-e2e 与 ruff/mypy 覆盖
# 完整门禁（前台）
ruff check .
mypy domains shared tool_gateway apps workflows notification_gateway infra
python3 scripts/check_boundaries.py
python3 scripts/scan_sensitive.py
python -m pytest -q -W error -m "not e2e"
cd apps/web && npm run gen:api && git diff --exit-code -- src/api/api.d.ts
npm run typecheck && npm run lint && npm run test && npm run build && cd ../..
git diff --check
TRADEOS_REQUIRE_E2E=1 python -m pytest tests/e2e -q -W error
```

**提交后、push 前另执行：**

```bash
git diff --check a5548fb..HEAD
# 必须 rc=0 才允许 push
```

**GREEN 判定：** 上述全部 rc=0（e2e 2 passed、non-e2e 全绿、diff-check rc=0）。

**提交（单全绿 commit，含本计划文件）：**

```bash
git add docs/superpowers/plans/2026-08-16-reply-qualification-production-trigger.md \
        apps/scheduler_worker/reply_events.py apps/scheduler_worker/runtime.py \
        workflows/reply_qualification/flow.py shared/events/catalog.py \
        tests/integration/test_scheduler_reply_trigger.py
git commit -m "feat(scheduler): InboundMessageStored handler contract and reply qualification composition seam"
git push origin HEAD
# 等待 exact-HEAD GitHub Actions CI success（gh run view --json status,conclusion,headSha）
```

**不做与残余（明确列出）：**

- ❌ 生产 `ReplyModelPort` 适配器与模型 provider/凭证边界：**未决，需架构审查/可能 ADR**，本片不预判、不引入凭证路径。
- ❌ 声称「生产触发完成」：production main 无真实 `ReplyModelPort` 注入前，生产进程 reply flow 不自动启用；本片交付 handler 契约 + 可选组合 seam + 测试组合端到端验证。
- ❌ 修改 `campaign_events.on_reply_received` 的停序列/唤醒逻辑。
- ❌ `extract_need`/`decide_next` 步骤（demand 域属切片 7）。
- ❌ Gmail 入站 connector、正文/主题持久化、HTML 解析。
- ❌ 修改 AGENTS/HANDBOOK/ROADMAP/GLOSSARY。
- ❌ 把测试组合（含 fake model）注入生产 main。
- 保持单一全绿 commit（含本计划文件）；不改 AGENTS/HANDBOOK；不引入 production fake。
