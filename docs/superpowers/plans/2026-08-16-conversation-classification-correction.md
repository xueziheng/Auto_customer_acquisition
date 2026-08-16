# Conversation Classification Correction Persistence Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (with strict TDD discipline) to implement this plan task-by-task. Every behavior step must follow RED → confirm expected failure → minimal GREEN; no step may be implemented before its RED test is written and its failure reason is confirmed as the missing table/interface/implementation (not syntax, fixture, or ImportError accidents). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 本轮只交付纠正记录的**耐久持久化基础**：`ConversationService.correct_classification` 把人工纠正分类留痕为独立 append-only 纠正记录（原分类行不变、纠正历史只增不改、DB 原子幂等、并发安全）。**评估集摄取/导出是后续独立任务**——本片不实现、不声称「自动成为候选样本」已完成；只保证纠正数据可耐久查询（`list_corrections` 按时间升序），为将来评估集摄取提供可靠来源。不引入模型、不引入凭证、不依赖 demand/approvals 域。

**Architecture:** conversations 域内闭环。新增独立表 `conversation_classification_corrections`（不复用/不改 0018 `conversation_classifications` 表，避免破坏其 at-most-once 单行与既有 0018 迁移测试断言）。数据模型消除固定时钟主键冲突与 TOCTOU：

```text
correction_id        域内生成 new_id("ccr")（str；不修改 shared/identifiers，不引入 ADR）
PK                   (tenant_id, correction_id)
UNIQUE               (tenant_id, message_id, corrected_by, corrected_category)   ← DB 幂等键
corrected_at         仅业务语义（同键并发只一行；不同纠正即使同一时钟也两行）
```

`add_correction` 用 PostgreSQL `INSERT ... ON CONFLICT DO NOTHING` 返回 `bool`（True=新插入，False=幂等冲突）——**不做服务层 list-then-insert**，消除 TOCTOU。`list_corrections` 按 `corrected_at ASC, correction_id ASC` 排序。依赖方向保持 conversations → shared 单向。

**Tech Stack:** Python 3.12、SQLAlchemy 2.x async、PostgreSQL 16、pytest-asyncio、testcontainers（Postgres）、Alembic。

## Global Constraints

- 所有 Python 命令使用 `PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH`。
- 九条全局硬边界全部生效；本片重点：正文/凭证不进日志/异常（本片无正文输入）；tenant 显式过滤（硬边界 8）；依赖方向 conversations → shared（硬边界 9）；金额/置信度不涉及。
- 不修改 AGENTS.md/HANDBOOK/ROADMAP/GLOSSARY；不改 `conversation_classifications` 表结构（0018 不动）；不改 `shared/` 契约；不引入模型/provider/凭证路径。
- 新文件 Git mode `100644`；不提交 AppleDouble、`.env`、凭证、构建产物。
- **提交时对新文件使用 `git add --chmod=-x <files>`**，并以 `git ls-files --stage` 验证全部新文件 index mode 为 `100644`（T7 外置盘 stat 展示可能为 `-rwx------`，以 git index 为准）。
- **AppleDouble 卫生**：根 `.gitignore` 已忽略 `._*`；`find . -name "._*" -not -path "./.git/*" -delete` 清理范围**同时覆盖 `.mypy_cache/`、`.pytest_cache/`、`.ruff_cache/` 等缓存目录内的 sidecar**（目的：Alembic 扫描 `migrations/versions/` 时若混入 `._0020_...` 会因 NUL 字节报 SyntaxError；清理保证迁移扫描卫生与测试确定性）。
- 单全绿逻辑 commit + push；push 前完整门禁前台执行（timeout >= 1500000ms，禁止 run_in_background）；push 后 exact-HEAD GitHub Actions CI success 才交付；独立复审通过才算完成。
- 每个任务：RED（本地跑不提交）→ 确认预期失败原因 → GREEN → focused pytest `-W error` → Ruff → mypy → `scripts/check_boundaries.py` → `scripts/scan_sensitive.py` → `git diff --check` → commit → push → exact-HEAD CI。

## File and Interface Map

```text
migrations/versions/0020_conversation_classification_corrections.py  新增：纠正表 + PK(tenant,correction_id) + UNIQUE 幂等键 + CHECK
infra/db/tables.py                                                    新增 ConversationClassificationCorrectionRow
domains/conversations/models.py                                       新增 ClassificationCorrection dataclass（含 correction_id）
domains/conversations/repository.py                                   ClassificationRepository 增 add_correction / list_corrections
infra/db/repositories/conversations.py                                impl（tenant-bound fail-closed；ON CONFLICT DO NOTHING）
domains/conversations/service_impl.py                                 实现 correct_classification
tests/integration/test_conversations_correction.py                    新增（RED 先行，分阶段）
tests/integration/test_migrations.py                                  新增 0020 roundtrip；head 字面量 0019→0020
docs/superpowers/plans/2026-08-16-conversation-classification-correction.md  本计划文件（与代码同一全绿 commit）
```

`ClassificationCorrection`（models.py，dataclass）字段：

```text
correction_id: str            # new_id("ccr")；域内 str，不改 shared/identifiers
tenant_id: TenantId
message_id: MessageId
corrected_category: ReplyCategory
corrected_by: str             # 人工标识，非空
corrected_at: datetime        # UTC aware（业务语义；不参与唯一性）
```

模型长度 fail-closed（与 DB 列上限对齐，先于 DB 报错/截断拒收）：
`correction_id`/`tenant_id` ≤ 32，`message_id`/`corrected_by` ≤ 100；
`__post_init__` 超长即 `ValidationError`，恰好等于上限为合法（有测试覆盖）。
模型 docstring 只声明「未来评估集摄取的耐久来源」——本期不实现评估集摄取，
不得写成已接入摄取/评估（docstring 诚实性）。

`ClassificationRepository` 新增方法（均显式 tenant 参数，check_boundaries 要求）：

```text
async def add_correction(self, correction: ClassificationCorrection) -> bool:
    """INSERT ... ON CONFLICT DO NOTHING；True=新插入，False=幂等冲突。"""
async def list_corrections(
    self, tenant_id: TenantId, message_id: MessageId
) -> list[ClassificationCorrection]:   # ORDER BY corrected_at ASC, correction_id ASC
```

`ConversationService.correct_classification`（签名已在 service.py 固定，不改）。

---

### Task 1: Append-only correction persistence with DB-atomic idempotency

**Files:**
- Create: `migrations/versions/0020_conversation_classification_corrections.py`
- Modify: `infra/db/tables.py`
- Modify: `domains/conversations/models.py`
- Modify: `domains/conversations/repository.py`
- Modify: `infra/db/repositories/conversations.py`
- Modify: `domains/conversations/service_impl.py`
- Create: `tests/integration/test_conversations_correction.py`
- Modify: `tests/integration/test_migrations.py`

**TDD 分阶段（每阶段：RED → 确认预期失败 → 最小 GREEN）：**

**分阶段执行纪律：阶段 1/2/3 每阶段到达 GREEN 后只做本地 checkpoint 与报告（不提交、不 push）；整个 classification correction 小任务全部阶段完成且完整门禁全绿后，才做一个 commit + push。已知不完整功能不得中途 push。**每阶段内的 RED 测试先行、确认预期失败原因、实现最小 GREEN、跑该阶段 + 既有回归后本地 checkpoint；下一阶段的 RED 在上一阶段 GREEN 的基础上编写。

**阶段 1 — 模型/迁移/仓储契约测试（RED 预期：缺表/缺接口/缺实现）**

RED 测试文件先行，预期失败原因逐一写明：
1. 迁移契约（实际：`test_0020_classification_corrections_revision_present` /
   `test_0020_classification_corrections_contract_matches_orm` /
   `test_0020_classification_corrections_downgrade_roundtrip`）：
   - RED 预期：`alembic upgrade head` 后表不存在 / ORM `ConversationClassificationCorrectionRow` 缺失 → ImportError 或表查询失败；**不允许** collection ImportError 当有效 RED——若 collection 报 ImportError，先补最小模块骨架再确认。
   - GREEN 断言：`revision == "0020"`；列/PK/UNIQUE/CHECK 与 ORM 语义 parity（`inspect(sync)` + `_type_key` + 14 类 `set()` 比对，沿用 0018/0019 模式）→ `downgrade 0019` → 新表消失 → `upgrade head` 恢复契约不变。
   - head 字面量：用 `rg -n 'revision == "0019"' tests/integration/test_migrations.py` 与 `rg -n 'RED：0019' tests/integration/test_migrations.py` 定位（不写死行号）→ 全部 `"0019"` → `"0020"`、RED 消息 → `"RED：0020 会话分类纠正迁移尚未创建"`；`EXPECTED_TABLES` 追加 `conversation_classification_corrections`；既有 0018/0019 逐版本断言原样保留。
2. `test_correction_repository_insert_and_list`（仓储）：
   - RED 预期：`ClassificationRepository` 无 `add_correction`/`list_corrections` → AttributeError；或表缺失 → SQL 错误。
   - GREEN 断言：`add_correction` 返回 True；`list_corrections` 返回 1 条且字段全等；同键重复 `add_correction` 返回 False 且仍 1 条；不同 `corrected_by` 同 message → 2 条；排序为 `corrected_at ASC, correction_id ASC`。
3. 模型（实际：`test_correction_model_valid_construction_preserves_fields` /
   `test_correction_model_rejects_invalid_inputs`，含长度 fail-closed 与边界合法）：
   - RED 预期：`ClassificationCorrection` 不存在 → ImportError（先建最小模型后确认校验行为）。
   - GREEN 断言：空 `corrected_by` → ValidationError；非 `ReplyCategory` → ValidationError；naive `corrected_at` → ValidationError；`correction_id` 非空。

**阶段 2 — 服务行为测试（RED 预期：service_impl 无 correct_classification 实现 → AttributeError，实际确认 6 failed）**

测试 harness 复用 `test_conversations_classification.py` 的 `conversations_db`/`_service`/`MutableClock` 模式。
4. `test_correct_classification_persists_and_preserves_original_classification`：先 `record_classification(REQUESTS_MATERIALS, "model-v1")` → 纠正 UNSUBSCRIBE/emp-1 → 恰 1 条纠正（`new_id("ccr")` 前缀、≤32、corrected_at=时钟）；原分类 category/classified_by/classified_at 完全不变；`now` 只调用/验证一次；成功纠正 outbox 等于 baseline（无事件）。
5. `test_correct_classification_unclassified_message_fails_closed`：未分类 message → 精确 `ValidationError("消息尚未分类")`，零纠正、零事件。
6. `test_correct_classification_sequential_idempotency`：同 (by, category) 两次均返回 None 且恰 1 条；不同 category / 不同 by（固定时钟）各保留一行。
7. `test_correct_classification_input_fail_closed`：空值/空白/超长/非枚举/时钟非法 → 固定摘要 ValidationError。Codex 复审：空白 tenant/message（`"   "`）必须精确 `纠正租户无效`/`纠正消息无效`（str+strip，与 `ClassificationCorrection` 一致），开 UoW 前拒绝；长度 tenant 33/message 101/corrected_by 101；时钟 naive 与非 UTC offset → `服务时钟必须为 UTC`；全部零纠正、outbox 不变、不回显输入。
8. `test_correct_classification_cross_tenant_invisible`：tenant B-bound 服务查 A 的原分类 → 精确 `ValidationError("消息尚未分类")`（不抛 TenantIsolationViolation，那是仓储参数越界测试）；A/B 均零纠正、A 原分类不变。
9. `test_correct_classification_no_event_no_leak`：101 字符凭证 marker 作 corrected_by → 固定 `纠正人超长`；异常文本/日志/outbox 无 marker、marker 不落库；合法纠正同样零事件、零日志。

**阶段 3 — 服务级真实并发测试（结果纪律：初始运行若直接 GREEN，是既有 DB 级并发语义的行为确认，如实报告、不伪造 RED；必须补 mutation proof）**

11. `test_correction_repository_cross_tenant_fails_closed`（阶段 1 已实现）：不匹配绑定租户的参数调 `add_correction`/`list_corrections` → `TenantIsolationViolation`。
12. `test_correction_concurrent_same_key_exactly_one_row`：两个独立 `ConversationServiceImpl` 实例并发 `correct_classification` 同 message/category/by、同固定 clock → 两结果均非异常且都是 None；新 session 回读恰 1 行、key 正确；outbox 等于并发前 baseline。**初始运行直接 GREEN**（DB UNIQUE + ON CONFLICT DO NOTHING 的既有并发语义确认）；mutation proof：临时去掉 `on_conflict_do_nothing`（普通 INSERT）→ 一方 `IntegrityError`，断言失败 RED；恢复后 GREEN。
13. `test_correction_concurrent_different_keys_two_rows_same_clock`：两个独立服务实例并发同 message 不同 category（同 by、同 clock）→ 两结果正常；回读恰 2 行、两个 key 完整、corrected_at 均 NOW；原分类不变；outbox baseline 不变。mutation proof：临时服务固定 category（错误折叠不同纠正）→ 恰 1 行 RED；恢复后 GREEN。

并发测试的实现约束（写进测试 docstring）：
- 两个独立 `ConversationServiceImpl` 实例；各自 `uow_factory` 每调用创建独立 `SqlAlchemyConversationsUnitOfWork`（其 `__aenter__` 新建 AsyncSession）——**绝不复用同一 AsyncSession**；
- 用 `asyncio.gather(..., return_exceptions=True)` 同时启动；断言行数/原分类用新 session 回读；
- 不得在测试间共享 AsyncSession/engine session 对象；临时 mutation 只用 apply_patch，结束 `git diff` 确认无残留；
- 单 commit 仍在整个任务全部阶段 + 完整门禁全绿后（见「分阶段执行纪律」）。

**GREEN 实现（Task 1）：**

- models.py：`ClassificationCorrection` dataclass + `__post_init__`（`correction_id` 非空、`corrected_by` 非空、`corrected_category` 枚举、`corrected_at` UTC）。
- tables.py：`ConversationClassificationCorrectionRow`：列 `tenant_id`/`correction_id`/`message_id`/`corrected_category`/`corrected_by`/`corrected_at`；PK `(tenant_id, correction_id)`；UNIQUE `(tenant_id, message_id, corrected_by, corrected_category)`；CHECK `corrected_category IN (14 类，与 0018 完全一致)`；无 FK（与 0018 一致）。
- migrations/0020：与 ORM 逐列/PK/UNIQUE/CHECK 同名同语义；downgrade 只 drop 本表。
- repository.py：Protocol 增 `add_correction -> bool` / `list_corrections`（显式 tenant 参数）。
- repositories/conversations.py：`add_correction` 用 `pg_insert(...).on_conflict_do_nothing(index_elements=["tenant_id","message_id","corrected_by","corrected_category"])`，`rowcount > 0` → True；`list_corrections` `_require_tenant` + `ORDER BY corrected_at ASC, correction_id ASC`。
- service_impl.py：`correct_classification`：校验 tenant/message/category/corrected_by → **tenant-bound `classifications.get` 原分类**（None → `ValidationError("消息尚未分类")`）→ 构造一次 `ClassificationCorrection(correction_id=new_id("ccr"), ...)` → `await uow.classifications.add_correction(correction)`（DB 原子幂等，返回 bool 忽略）→ 不发布事件、不改原分类行。
- 不改 `domains/conversations/service.py` 签名、不改 0018 表/`MessageClassification`、不改 `Message` 模型、不改 `shared/`。

**验证命令（每个 commit 前，全部前台、timeout >= 1500000ms、pipefail 真实 rc）：**

```bash
export PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH
find . -name "._*" -not -path "./.git/*" -delete
# 每阶段 RED：仅跑该阶段测试，确认预期失败（本地，不提交）
python -m pytest tests/integration/test_conversations_correction.py -q -W error
# GREEN 后定向回归
python -m pytest tests/integration/test_conversations_correction.py \
  tests/integration/test_conversations_classification.py \
  tests/integration/test_conversations_messages.py \
  tests/integration/test_reply_qualification_workflow.py \
  tests/integration/test_migrations.py \
  tests/integration/test_message_content_reader.py \
  tests/integration/test_scheduler_reply_trigger.py \
  tests/integration/test_outbox_delivery.py -q -W error
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

**GREEN 判定：** 上述全部 rc=0；新测试 13 个全绿；0018 测试仍绿（表未动）。

**提交（单全绿 commit，含本计划文件）：**

```bash
git add docs/superpowers/plans/2026-08-16-conversation-classification-correction.md \
        migrations/versions/0020_conversation_classification_corrections.py \
        infra/db/tables.py domains/conversations/models.py \
        domains/conversations/repository.py infra/db/repositories/conversations.py \
        domains/conversations/service_impl.py \
        tests/integration/test_conversations_correction.py \
        tests/integration/test_migrations.py
git commit -m "feat(conversations): persist classification corrections durably"
git push origin HEAD
# 等待 exact-HEAD GitHub Actions CI success（gh run view --json status,conclusion,headSha）
```

**不做（明确排除）：**

- ❌ 评估集摄取/导出（本片只保证纠正数据耐久可查询；「自动成为候选样本」属后续独立任务）
- ❌ 修改 0018 `conversation_classifications` 表结构或 `MessageClassification` 模型
- ❌ 修改 `shared/` 契约、新增 ADR
- ❌ 发布纠正事件 / 改 outbox / 通知
- ❌ `suggest_next_questions`（依赖 demand 完整度，demand 域无持久化 → 阻塞）
- ❌ 生产 `ReplyModelPort`/provider（未决，需架构审查/ADR）
- ❌ UI、模型重评估
- ❌ 修改 AGENTS/HANDBOOK/ROADMAP/GLOSSARY
- 注：`extract_need`/`decide_next` 是 workflows/reply_qualification/AGENTS.md 明确的 Phase 1 四步范围，**仅因 demand 持久化等前置依赖未实现而暂时阻塞，后续仍必须完成**——本片不实现，但不得将其从 roadmap 移除。
