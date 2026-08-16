# Next Question Selection Plan（下一问选择：确定性选主题）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (with strict TDD discipline) to implement this plan task-by-task. Every behavior step must follow RED → confirm expected failure → minimal GREEN; no step may be implemented before its RED test is written and its failure reason is confirmed as the missing implementation (NotImplementedError / AttributeError), not syntax, fixture, or ImportError accidents. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付 `domains/conversations` 的**下一问选择**最小切片：`NextQuestionSuggestion.__post_init__` 校验 + `ConversationServiceImpl.suggest_next_questions` 确定性实现 + `service.py` 该方法的公共 docstring 更新（**只改 docstring，不改签名**）。输入（`missing_fields: list[str]` 与 `completeness: int`）由上层从 demand 域查得**传入**（`service.py` 已声明，本域不 import/read demand）；输出**最多两个**问题主题 + 内部中文 reason。具体英文问句措辞由 `qualification_agent`（后续切片）生成——**不在本任务**。

**候选判定：** 当前最小、未阻塞、可独立验收任务。依据：
- `service.py` 的 `suggest_next_questions(tenant_id, conversation_id, missing_fields, completeness)` 签名已存在且明确「输入由上层从 demand 域查得传入」——不依赖 demand 域持久化（旧计划的「阻塞」前提不成立）；
- 上一任务（纠正留痕）已全绿交付；本任务不依赖任何未决边界（模型/provider/凭证与 demand 持久化均无关）；
- 属于 HANDBOOK 切片 6 / conversations AGENTS「Phase 1 范围」明列的「下一问接口」；
- 不跳 UI、不跳切片 7、不引入真实模型 provider。

**Architecture:** conversations 域内闭环、纯确定性选择。`missing_fields` 顺序视为**上游已排序优先级契约**（最关键的在前）——文档未规定业务优先级，**不发明任何业务优先级、不做 topic whitelist**；选择语义 = 稳定去重（保留首次出现）→ 截断前 2 → 构造建议。`completeness` 是 0–5 确定性等级（GLOSSARY「Need Completeness」、demand AGENTS「需求完整度 0–5」），**不是模型置信度**：服务只读它、不回显概率、不持久化任何东西（无事件、无写库）。reason 是**内部中文解释**（为什么问这两个），不是客户可见邮件。**不做** hidden completeness 与空列表的一致性规则（不发明「completeness 高但列表空」的特殊处理——空列表就是空列表）。

## 文档契约盘点（诚实标注：哪些是文档已有，哪些是本计划采用的最小安全语义）

| 语义 | 文档状态 | 本计划决定 |
|---|---|---|
| topics ≤ 2 | 文档已有（AGENTS「最多两个」、模型 docstring「最多两个」） | 模型校验 `len(topics) <= 2`；服务截断前 2 |
| 主题是字段名（quantity/application…），措辞由 qualification_agent 生成 | 文档已有（AGENTS + 模型 docstring） | 本任务只输出字段名主题 |
| missing_fields 顺序 = 优先级 | **文档未规定** | 采用「上游顺序即已排序优先级契约」，写入 `service.py` 公共 docstring；稳定去重保留首次出现、只取前 2。理由：这是唯一无需发明业务规则的确定性解释；AGENTS「一次只问最关键的一两项」暗示顺序有意义 |
| 去重 | **文档未规定** | 服务去重（保留首次出现）；模型拒绝重复（防御性第二道）。理由：重复字段名是上游噪声，静默保留会浪费宝贵的 2 个名额 |
| 空 missing_fields | **文档未规定** | `topics=[]` + 固定 reason「无缺失字段，无需追问」；不报错、不编造问题。理由：上游 `missing_fields_for_sourcing()` 在 sourcing-ready 时合法返回空列表，报错会让「已完整」的会话崩溃，编造问题违背「缺失才问」 |
| 空白/非 str/带前后空白的 missing_fields 元素 | **文档未规定** | `ValidationError` fail-closed（固定摘要，不回显）。理由：字段名是固定词表，空白/前后空白/非 str 说明调用方 bug，静默丢弃或 trim 会掩盖上游错误 |
| completeness 范围 | 文档已有（0–5，demand AGENTS + GLOSSARY） | 服务校验 `int` 且非 `bool`（bool 是 int 子类，显式拒绝）、`0 <= x <= 5`；0/5 均为合法边界 |
| reason 内容 | 文档已有（模型 docstring「基于缺失字段与完整度级别」） | 服务确定性生成固定格式中文 reason（见下）；模型只校验 strip 后非空（**不拒绝 reason 自身的前后空白**——reason 由内部固定格式生成，无需过度限制；明确采用的最小语义） |
| conversation_id/tenant_id 为何在签名 | 文档未明说 | 采用：tenant-bound `uow.conversations.get(tenant_id, conversation_id)` 验证会话存在；不存在/跨租户不可见 → `ValidationError("会话不存在")` fail-closed（硬边界 8：所有查询强制租户过滤；不得查询他租户；不抛 TenantIsolationViolation——那是仓储参数越界语义）。理由：签名里有 conversation_id 却完全不查，等于对任意会话 id 都能出建议，跨租户误用不可检测 |

## Global Constraints

- 所有 Python 命令使用 `PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH`（完整 PATH 见上一任务报告，前台、timeout >= 1500000ms、禁止 background、真实 rc）。
- 九条全局硬边界全部生效；本片重点：硬边界 8（tenant 显式过滤）、硬边界 3（completeness 是确定性等级，非模型输出概率，不存置信度数字）。
- 不修改 AGENTS.md/HANDBOOK/ROADMAP/GLOSSARY；**`service.py` 只改 `suggest_next_questions` 的 docstring，不改签名**；不改 `shared/`；**无迁移**（0020 之后 head 不变）；不引入模型/provider/凭证路径；不 import demand。
- 新文件 Git mode `100644`（`git add --chmod=-x` + `git ls-files --stage` 验证）。
- AppleDouble 卫生：`find . -name "._*" -not -path "./.git/*" -delete`（含缓存目录 sidecar；Alembic 扫描卫生）。
- 单全绿逻辑 commit + push；push 前完整门禁前台执行；push 后 exact-HEAD GitHub Actions CI success 才交付；独立复审通过才算完成。**每阶段 GREEN 只做本地 checkpoint（不提交）。**
- 不削弱测试/边界/配置；临时 mutation 只用 apply_patch，结束 `git diff` 确认无残留。

## File and Interface Map（预计 6 文件，无迁移）

```text
domains/conversations/models.py            NextQuestionSuggestion.__post_init__ 实现（校验 + 防御性拷贝）
domains/conversations/service.py           suggest_next_questions 公共 docstring 更新（只改 docstring，不改签名）
domains/conversations/service_impl.py      suggest_next_questions 实现（导入 NextQuestionSuggestion）
tests/unit/test_next_question_suggestion.py          新增：纯模型单元测试（4 项，无 DB fixture）
tests/integration/test_conversations_suggest.py      新增：真 Postgres 服务集成测试（RED 先行）
docs/superpowers/plans/2026-08-16-next-question-selection.md   本计划文件（与代码同一全绿 commit）
```

提交清单（单 commit，6 文件）：上述 6 个；commit message 建议
`feat(conversations): suggest next qualification questions deterministically`；
`git add --chmod=-x <6 文件>` 后 `git ls-files --stage` 全部 `100644`。

## NextQuestionSuggestion 模型校验契约（`__post_init__`）

```text
topics: list[str]    # 必须 list（拒绝 str/tuple 等）；0..2 个；每项必须 str、
                     # strip 后非空、且 item == item.strip()（拒绝元素前后空白）；
                     # 必须无重复；无长度上限（topic 无 DB 长度定义，不自造 max）
reason: str          # 必须 str、strip 后非空；不拒绝 reason 自身前后空白
                     # （内部固定格式生成，无需过度限制——明确采用的最小语义）
```

- 合法：`topics=[]`（空缺失字段语义）、1 个、2 个；`reason` 任意 strip 后非空 str。
- 非法（均 `ValidationError` 固定摘要、不回显）：`topics` 不是 list；`len(topics) > 2`；任一 topic 非 str / strip 后为空 / 带前后空白（`item != item.strip()`）；重复 topic；`reason` 非 str / strip 后为空。
- **防御性拷贝（表述精确）**：`__post_init__` 用 `object.__setattr__(self, "topics", list(topics))` 换新 list——**只切断「构造后调用方继续修改传入源列表」这一条别名路径**；`suggestion.topics` 属性自身仍是可变 list（frozen 只挡属性重绑，不挡原地修改），**不宣称对象深度不可变**；把字段类型改为 tuple 属 API 变更，超出最小范围，不做。

## 服务输入/输出/无事件语义（`suggest_next_questions`）

```text
输入校验（开 UoW 前，固定摘要 ValidationError，全部先于任何 DB 访问）：
  tenant_id / conversation_id：必须 str、strip 后非空、len <= 32
    （与 conversations 表 String(32) 列上限对齐，fail-closed）
  missing_fields：必须精确 list（str/tuple 等一律拒绝）；每项必须 str、
    strip 后非空、且 item == item.strip()（拒绝前后空白）；非 str/空白/前后空白 → fail-closed
  completeness：必须 int 且非 bool，0 <= completeness <= 5（0/5 合法）
主题选择（纯确定性，服务内完成）：
  稳定去重保留首次出现 → 截断前 2 → topics
  （输入 list 绝不被原地修改；构造用新 list）
reason（固定格式，内部中文，确定性生成，无模型输出）：
  非空缺失字段：f"完整度 {completeness}/5，缺失字段（按上游顺序取前 2）：{'、'.join(topics)}"
  空缺失字段：  "无缺失字段，无需追问"
DB 访问（仅一次读，无写）：
  tenant-bound uow.conversations.get(tenant_id, conversation_id)
  None → ValidationError("会话不存在")（含跨租户不可见；不查他租户）
输出：NextQuestionSuggestion(topics, reason)
副作用：零——不发布事件、不写库、不写日志（outbox/caplog/行数断言不变）
```

## service.py 公共 docstring 更新内容（只改 docstring，不改签名）

`suggest_next_questions` 的 docstring 补充（本域不硬编码字段优先级）：
- `missing_fields` 顺序是**上游优先级契约**（最关键的在前；本域不做 topic whitelist、不发明业务优先级）
- 稳定去重保留首次出现 → **只选前 2**
- **空列表合法** → 空 `topics`（无缺失即无追问）
- `completeness` 是 0–5 确定性等级（非模型置信度），仅用于 reason 说明

## TDD 分阶段（每阶段 RED → 确认预期失败 → 最小 GREEN）

**阶段 1 — 模型单元测试（RED 预期：`__post_init__` 当前 `raise NotImplementedError`）**

- 文件：`tests/unit/test_next_question_suggestion.py`（纯单元，无 DB fixture，4 项）。
- RED：构造 `NextQuestionSuggestion(topics=["quantity"], reason="...")` → `NotImplementedError`（预期失败原因：骨架未实现，非语法/fixture 错误）。
- GREEN 实现：`__post_init__` 校验 + 防御性拷贝。
- 测试（行为断言，不自造 topic 长度上限——topic 无 DB 长度定义）：
  1. `test_valid_construction_preserves_fields`：1/2 个 topic + reason 保真。
  2. `test_empty_topics_allowed`：`topics=[]` + 非空 reason 合法（空缺失字段语义）。
  3. `test_rejects_invalid_inputs`：topics 非 list（str/tuple）；3 个 topic；非 str topic；空白 topic（`""`/`"  "`）；带前后空白 topic（`" quantity "`）；重复 topic；reason 非 str / strip 后为空 → 各 `ValidationError`。
  4. `test_defensive_copy_breaks_source_alias`：构造后修改源列表 → 建议不受影响（只断言这一条别名被切断，不断言 topics 深度不可变）。
- mutation proof：临时放开 `len(topics) <= 2` 上限 → 测试 3 RED；临时去掉防御性拷贝 → 测试 4 RED；恢复后 GREEN。

**阶段 2 — 服务集成测试（RED 预期：`ConversationServiceImpl.suggest_next_questions` 缺失 → AttributeError）**

- 文件：`tests/integration/test_conversations_suggest.py`（只放真 Postgres 服务集成）。
- 测试 harness 复用 correction/classification 模式：真实 PostgreSQL（`db_url` fixture）、真实 `SqlAlchemyConversationsUnitOfWork`、真实 `ConversationServiceImpl`、`MutableClock`；会话经 `uow.conversations.add(Conversation(..., channel="email"))` 直接建（无事件噪声）。
- 测试（真实行为断言，不 mock）：
  5. `test_returns_first_two_deduped_in_upstream_order`：`["quantity","quantity","destination","material"]` → `topics == ["quantity","destination"]`（顺序 = 上游首次出现序）；reason 含完整度与两主题。
  6. `test_truncates_to_two`：4 个不同字段 → 恰前 2。
  7. `test_empty_missing_fields_returns_no_topics`：`[]` → `topics == []`、reason 固定「无缺失字段，无需追问」。
  8. `test_completeness_boundaries_0_and_5`：0 与 5 均接受，reason 正确反映等级。
  9. `test_rejects_invalid_completeness`：`-1`、`6`、`2.5`、`True`、`False`、`"3"` → `ValidationError`（bool 显式拒绝）。
  10. `test_rejects_invalid_missing_fields`：`missing_fields` 传 str、tuple、`["", "quantity"]`、`["  "]`、`[" quantity "]`、`[123]` → `ValidationError`。
  11. `test_rejects_oversized_ids`：tenant_id 33 字符、conversation_id 33 字符 → `ValidationError`（长度 fail-closed，与 DB 列上限对齐）。
  12. `test_input_validation_precedes_db`：空白/超长/非法输入时，即使 conversation_id 不存在，错误也必须是输入校验摘要（绝非「会话不存在」）——证明校验先于 UoW/DB 路径。
  13. `test_conversation_must_exist_tenant_bound`：不存在的 conversation_id → `ValidationError("会话不存在")`；tenant A 有会话、B-bound 服务用同 id → 同样 `ValidationError("会话不存在")`（不可见即不存在，不抛 TenantIsolationViolation）；A/B 均无副作用。
  14. `test_no_event_no_log_no_write`：caplog 零记录；outbox 数量/内容与 baseline 完全一致；conversation/message 行数不变（无写库）。
- mutation proof（临时、apply_patch、无残留）：去掉去重 → 测试 5 RED；去掉截断 → 测试 6 RED；completeness 放行 6 → 测试 9 RED；去掉存在性检查 → 测试 13 RED；恢复后各自 GREEN。

## 验证命令（commit 前全部前台、timeout >= 1500000ms、pipefail 真实 rc）

```bash
export PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH
find . -name "._*" -not -path "./.git/*" -delete
# 每阶段 RED：仅跑该阶段测试，确认预期失败（本地，不提交）
python -m pytest tests/unit/test_next_question_suggestion.py -q -W error
python -m pytest tests/integration/test_conversations_suggest.py -q -W error
# GREEN 后定向回归
python -m pytest tests/unit/test_next_question_suggestion.py \
  tests/integration/test_conversations_suggest.py \
  tests/integration/test_conversations_correction.py \
  tests/integration/test_conversations_classification.py \
  tests/integration/test_conversations_messages.py \
  tests/integration/test_reply_qualification_workflow.py \
  tests/integration/test_migrations.py -q -W error
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
# 提交纪律（6 文件清单见 File and Interface Map）
git add --chmod=-x <6 文件> && git ls-files --stage（全部 100644）
git commit -m "feat(conversations): suggest next qualification questions deterministically"
git push origin HEAD（禁 force）→ gh run 轮询 headSha==exact HEAD 且 completed+success → 独立复审
```

## 明确不做（本任务）

- ❌ 具体英文问句生成（qualification_agent，后续切片；AGENTS 明示措辞由其生成）
- ❌ 任何 demand 域 import/读/持久化（输入由上层传入）
- ❌ 事件、持久化、迁移、`shared/` 改动、`service.py` 签名改动、UI、真实模型 provider
- ❌ topic 业务优先级发明 / topic whitelist（上游顺序即契约）、概率/置信度存储（硬边界 3）
- ❌ hidden completeness 与空列表的一致性规则（空列表就是空列表，不发明特殊处理）
- ❌ topic 长度上限自造（无 DB 长度定义）、`topics` 改 tuple（API 变更）
- ❌ 修改 AGENTS.md/HANDBOOK/ROADMAP/GLOSSARY（AGENTS 中「NextQuestionSelector」为旧名，代码契约以 `service.py` 的 `suggest_next_questions` 为准，仅记录不改文档）
