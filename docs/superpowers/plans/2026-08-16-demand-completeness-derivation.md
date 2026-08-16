# Demand Completeness Derivation Plan（ValidatedNeed 完整度确定性推导）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (with strict TDD discipline) to implement this plan task-by-task. Every behavior step must follow RED → confirm expected failure → minimal GREEN; no step may be implemented before its RED test is written and its failure reason is confirmed as the missing implementation (NotImplementedError), not syntax, fixture, or ImportError accidents. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付 `domains/demand` 的最小确定性内核：`ValidatedNeed.completeness`（0–5 累积阶梯）、`is_sourcing_ready`（≥3）、`missing_fields_for_sourcing`（只返回当前阻塞 level-3 门槛的最低层）三个纯派生属性，同步修正 `docs/architecture/01-domain-model.md` 状态图的陈旧文案。这三个属性是已交付的 `ConversationService.suggest_next_questions` 输入契约（`missing_fields` + `completeness`）的**上游生产者**（只读审计结论：C1 是当前最小、未阻塞、可独立全绿交付的切片 7 任务）。

**候选判定依据（只读审计事实）**：`domains/demand/models.py:238/254/258` 三个属性均为 `raise NotImplementedError` 骨架；`domains/demand/AGENTS.md:65`「validated → sourcing_ready 需要完整度达到 3 级以上（数量明确）」；`domains/demand/service.py:159-163` `mark_sourcing_ready` docstring「完整度不足 3 抛 InvalidStateTransition」；`docs/architecture/01-domain-model.md:146` 状态图写「sourcing_ready（完整度达 5）」——与上述两处权威来源冲突，属**文档缺陷**（修复见下文，非新 ADR/边界变更）。demand 无表/无迁移/无 service_impl/无仓储实现——本任务**不触碰**这些。

## 文档契约盘点（诚实标注：已有 vs 为消歧采用的最小确定性语义）

| 语义 | 文档状态 | 本计划决定 |
|---|---|---|
| 0–5 阶梯各层含义 | 部分已有：`models.py` docstring + `01-domain-model.md:109-124` 表格 + AGENTS「需求完整度 0–5」 | 采用**累积阶梯 = 最高连续满足级别**（见下形式化）；docstring 层级 5 现有表述「material、size_spec、quantity、destination 齐全」未含 required_by/application，属歧义，**随实现改为累积表述** |
| 阶梯是累积（后级字段存在不能跳级） | 隐含（「4 destination 且 required_by 有」「5 齐全」）但未明说 | 显式固化为累积早退：前级不满足即返回当前级，后级字段存在不跳级 |
| level 2 的 OR 语义（application **或** size_spec） | 文档已有（docstring「application 或 size_spec 有」） | 固化：二者至少一个非 None 即满足；level 5 不单独强制 application（size_spec 最终必有，OR 由 size_spec 满足） |
| presence 判定 | 文档未明说 | 只用「FactualField 是否为 None」；**不检查 value 的 truthiness、不做字段值合法性校验**（本切片不新增业务验证） |
| is_sourcing_ready = completeness ≥ 3 | 文档已有（AGENTS:65、service.py:159-163、models docstring「3 数量明确 ← 寻源门槛」） | 精确 `>= 3` |
| missing_fields_for_sourcing 的范围与顺序 | **文档未规定**（docstring 只说「还缺什么才能寻源…一次只问最关键的一两项」） | 采用最小确定性语义：只返回**当前阻塞达到 level 3 的最低层**，固定 4 分支（见下）；不列 destination/required_by/material——函数名与本域权威门槛是进入 sourcing_ready（≥3），后续完整化不在此函数、不把后续所有字段一次抛给客户 |
| 「application 与 size_spec 都缺」时返回两者 | **文档未规定** | 返回 `["application", "size_spec"]`（替代条件成对给出；一次最多两个，与 conversations AGENTS「一次只问最关键的一两项」及 qualification 示例一致） |

## File and Interface Map（预计 4 文件，无迁移）

```text
domains/demand/models.py                      ValidatedNeed.completeness / is_sourcing_ready /
                                              missing_fields_for_sourcing 实现（仅这三个属性 + 诚实 docstring）
tests/unit/test_demand_completeness.py        新增：纯单元测试（无 DB；importlib 动态取域内模型，
                                              真实 Provenance + FactualField，不 mock）
docs/architecture/01-domain-model.md          第 146 行状态图：sourcing_ready（完整度达 5）→（完整度 ≥ 3）
docs/superpowers/plans/2026-08-16-demand-completeness-derivation.md   本计划文件（与代码同一全绿 commit）
```

提交清单（单 commit，4 文件）；commit message 建议
`feat(demand): derive validated-need completeness and sourcing readiness`；
`git add --chmod=-x <4 文件>` 后 `git ls-files --stage` 全部 `100644`。

## 实现规格（纯确定性，无副作用）

**completeness（累积阶梯/最高连续满足级别）**——形式化：

```text
level 0   product_category 为 None（仅防御性处理运行时非法/遗留对象；类型契约仍要求它）
level 1   product_category 有，且 application 与 size_spec 都为 None
level 2   1 满足，且（application 或 size_spec 至少一个有），且 quantity 为 None
level 3   1–2 满足，且 quantity 有，且（destination 或 required_by 至少一个为 None）
level 4   1–3 满足，且 destination 与 required_by 都有，且（material 或 size_spec 至少一个为 None）
level 5   1–4 全部满足（material 与 size_spec 都有）
```

- 累积早退：逐级检查，任一级不满足立即返回该级；后级字段存在不能跳级。
- 因此 level 5 必含：product_category、application **或** size_spec（最终由 size_spec 满足）、quantity、destination、required_by、material、size_spec。**application 不因 level 5 单独强制**。
- presence 一律 `field is None` 判定；`quantity=FactualField(value=0)`、`application=FactualField(value="")` 均算「字段存在」。
- 不改输入对象/字段；不持久化、不事件、不 DB、不模型/provider、不跨域 import、不产出置信度数值（硬边界 3：完整度是确定性等级，非模型概率）。

**is_sourcing_ready**：`return self.completeness >= 3`。

**missing_fields_for_sourcing**（固定 4 分支，稳定顺序）：

```text
product_category 为 None                      → ["product_category"]
否则 application 与 size_spec 都为 None        → ["application", "size_spec"]   （替代条件成对）
否则 quantity 为 None                         → ["quantity"]
否则（completeness >= 3）                     → []
```

- 只返回**当前阻塞 level-3 门槛的最低层**；不列 destination/required_by/material（函数名与本域权威门槛是 sourcing_ready ≥3，后续完整化不在此函数）。
- 顺序固定：`product_category` → `["application","size_spec"]` → `["quantity"]` → `[]`。

**models.py docstring 更新（诚实）**：`completeness` 的 docstring 改为累积阶梯表述（0–5 各层含义如上形式化，含 OR 语义与「3 ← 寻源门槛」），保留「由字段推导，不可手动设置」；`missing_fields_for_sourcing` docstring 增补「只返回当前阻塞达到 level 3 的最低层，固定顺序；不列出后续完整化字段」。

**docs/architecture/01-domain-model.md:146 修复（文档缺陷，非 ADR/边界变更）**：
`validated → sourcing_ready（完整度达 5）→ sourcing_in_progress → quoted`
→ `validated → sourcing_ready（完整度 ≥ 3）→ sourcing_in_progress → quoted`
对齐证据：`domains/demand/AGENTS.md:65`（≥3 级、数量明确）；`models.py` docstring「3 quantity 有 ← 寻源门槛」；`domains/demand/service.py:159-163`（完整度不足 3 抛 InvalidStateTransition）。此为既有三处权威来源一致的文档笔误修复，不改变任何行为/边界，不需要 ADR。
（备注：`01-domain-model.md:109-124` 的 0–5 表格不在本任务范围——表格层级含义与阶梯一致，仅「5 可启动 Sourcing Case」措辞存在解读空间，留待后续文档任务，本任务不扩大修改。）

## TDD 分阶段（每阶段 RED → 确认预期失败 → 最小 GREEN）

**阶段 1 — 模型属性（RED 预期：三个属性均 `raise NotImplementedError`）**

- 文件：`tests/unit/test_demand_completeness.py`（纯单元，无 DB fixture）。
- 约定（沿用 `tests/unit/test_next_question_suggestion.py` 先例）：`importlib.import_module("domains.demand.models")` 动态取 `ValidatedNeed`（check_boundaries 的 domain-internals 规则要求跨层测试不静态 import 域内 models）；`from shared.schemas.provenance import FactualField, Provenance, SourceType` 与 `from shared.schemas.identifiers import ...` 可静态（shared 允许）。
- 测试辅助：`_provenance()` 构造真实 `Provenance(source_type=SourceType.CONVERSATION, source_id="msg_...", extracted_by="model-v1", extracted_at=NOW)`；`_field(value)` 返回 `FactualField(value, provenance=_provenance())`；`_need(**overrides)` 以**全字段齐备**的 level-5 基准构造 `ValidatedNeed`（need_id/tenant_id/account_id/product_category/source_message_id/created_at + 全部 FactualField），overrides 把指定字段置 None 得到各级。
- 测试清单（行为断言，真实对象，不 mock）：
  1. `test_completeness_level_0_product_category_missing`：product_category=None → 0（即使 quantity/material 等后级字段存在——累积不跳级）。
  2. `test_completeness_level_1_only_product_category`：仅 product_category → 1。
  3. `test_completeness_level_2_application_or_size_spec_without_quantity`：application 有/size_spec None/quantity None → 2；application None/size_spec 有/quantity None → 2（OR 双侧）。
  4. `test_completeness_level_3_quantity_without_destination_or_required_by`：quantity 有但 destination None → 3；quantity 有但 required_by None → 3。
  5. `test_completeness_level_4_destination_required_by_but_material_or_size_spec_missing`：destination+required_by 有但 material None → 4；…但 size_spec None → 4（此时 application 可有可无）。
  6. `test_completeness_level_5_all_cumulative_present`：全字段 → 5；application None + size_spec 有 → 仍 5（level 5 不单独强制 application）。
  7. `test_completeness_is_cumulative_no_skip`：quantity 有但 product_category None → 0；material/size_spec 都有但 required_by None → 3（后级存在不能跳级）。
  8. `test_presence_is_none_based_not_truthiness`：`quantity=FactualField(value=0, ...)` 算存在（→ 3/4/5 相应级别）；`application=FactualField(value="", ...)` 算存在（→ ≥2）——证明属性不偷做字段值业务验证。
  9. `test_is_sourcing_ready_threshold_2_vs_3`：level-2 构造 → False；level-3 构造 → True。
  10. `test_missing_fields_product_category_first`：product_category None（其余随意）→ `["product_category"]`。
  11. `test_missing_fields_alternative_pair`：product_category 有、application 与 size_spec 都 None → `["application", "size_spec"]`。
  12. `test_missing_fields_quantity_when_level2_satisfied`：OR 已满足、quantity None（destination/required_by/material 缺失与否均不影响）→ `["quantity"]`。
  13. `test_missing_fields_empty_when_sourcing_ready`：completeness ≥3（含只到 3 但 destination/required_by/material 仍缺）→ `[]`——不列后续完整化字段。
  14. `test_factual_fields_provenance_points_to_source_message`：真实 FactualField 的 `provenance.source_id` 与 `need.source_message_id` 对齐（硬边界 4 语义：事实字段能点到客户说这句话的那条消息；quantity 与 product_category 断言等值）。
  15. `test_properties_do_not_mutate`：调用三个属性前后，need 的全部字段（含 wrapper 对象）快照一致；`missing_fields_for_sourcing` 返回新 list（同一对象连续调用 identity 不同、改返回值不影响对象）。
- RED：三个属性均 NotImplementedError（预期失败原因：骨架未实现）。
- GREEN 实现：仅 `domains/demand/models.py` 三个属性 + docstring；不加任何 import 之外的新依赖（models.py 已 import `FactualField/InferredField/Provenance` 于 `shared.schemas.provenance`、`date`、`Money`——均为既有）。

**阶段 2 — 文档修复**：`docs/architecture/01-domain-model.md:146` 一行替换（无测试；由 diff 与门禁覆盖）。

**Mutation proofs（临时、apply_patch、每次恢复后确认 GREEN、结束 `git diff` 无残留）**：

| mutation | 精确测试 | 预期 RED |
|---|---|---|
| 去掉累积早退（例如改为直通：只要 quantity 有就返回 3/5，跳过前级检查） | 1（level-0 不跳级）/ 7 | level 0/3 断言失败 |
| level-2 OR 错写 AND（`application is None and size_spec is None` → `or`） | 3（OR 双侧） | 仅 application 有 → 误判 level 1 |
| level-3 门槛错写 AND（`destination is None or required_by is None` → `and`） | 4 | 仅 destination 缺 → 误判 level 4 |
| `is_sourcing_ready` 阈值改 4 | 9 | level-3 构造误判 False |
| missing 返回所有后续字段（如 quantity 缺时列出 quantity+destination+required_by+material…） | 12 / 13 | 断言 `["quantity"]` / `[]` 失败 |

**阶段 3 — 计划文件**：本文件（与代码同一全绿 commit）。

## 验证命令（commit 前全部前台、timeout >= 1500000ms、pipefail 真实 rc，禁止后台/管道掩盖 rc）

```bash
export PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH
find . -name "._*" -not -path "./.git/*" -delete
# 每阶段 RED：仅跑该阶段测试，确认预期失败（本地，不提交）
python -m pytest tests/unit/test_demand_completeness.py -q -W error
# GREEN 后定向回归（unit 全局 + 上一任务集成，防回归）
python -m pytest tests/unit -q -W error
python -m pytest tests/integration/test_conversations_suggest.py \
  tests/integration/test_conversations_correction.py \
  tests/integration/test_conversations_classification.py -q -W error
# 完整门禁（前台，逐条记录真实 rc）
ruff check .
mypy domains shared tool_gateway apps workflows notification_gateway infra
python3 scripts/check_boundaries.py
python3 scripts/scan_sensitive.py
python -m pytest -q -W error -m "not e2e"
cd apps/web && npm run gen:api && git diff --exit-code -- src/api/api.d.ts
npm run typecheck && npm run lint && npm run test && npm run build && cd ../..
git diff --check
TRADEOS_REQUIRE_E2E=1 python -m pytest tests/e2e -q -W error
# 提交纪律（4 文件清单见 File and Interface Map）
git add --chmod=-x <4 文件> && git ls-files --stage（全部 100644）
git commit -m "feat(demand): derive validated-need completeness and sourcing readiness"
git push origin HEAD（禁 force）→ gh run 轮询 headSha==exact HEAD 且 completed+success → 独立 pre/post review
```

## 明确不做（本任务）

- ❌ demand 服务/repository/迁移/持久化（signal/hypothesis/validated_need/cluster 表与 impl 均不在本任务；各自独立小任务）
- ❌ `NeedHypothesis.evidence()` / `can_promote_to_validated()`、`DemandSignal.evidence_level`（依赖信号加载/仓储设计，属 demand 持久化切片；`evidence()` 的仓储访问设计是已记录未知）
- ❌ `NeedCluster.suggests_catalog_product`（Phase 2）
- ❌ `extract_need`/`decide_next` 工作流步骤、qualification_agent 英文措辞、生产模型 provider（模型 provider/凭证边界 ADR 未决，阻塞项与本任务无关）
- ❌ 字段值合法性校验（presence 只判 None；value=0/"" 算存在）、topic/字段优先级发明、置信度数值
- ❌ 修改 AGENTS.md/HANDBOOK/ROADMAP/GLOSSARY；不改 `shared/`；不改 `01-domain-model.md` 除第 146 行外的内容

## 关键未知/ADR 阻塞（与本任务无关，仅记录）

- 生产模型 provider/凭证边界未决（`2026-08-16-reply-qualification-production-trigger.md` 已记录；阻塞 reply 流程生产启用与 qualification 措辞）。
- `NeedHypothesis.evidence()` 的仓储访问设计未知（阻塞假设侧切片）。
- `missing_fields_for_sourcing` 范围/顺序由本计划显式消歧（最小确定性语义，已标注）；如有异议走独立复审，不回退为「一次性列出所有后续字段」。

## 计划自检（内部一致性）

- 阈值一致：completeness 阶梯中 3 = 数量明确 = 寻源门槛（AGENTS:65 / service.py:159-163 / models docstring 一致）；is_sourcing_ready 精确 `>= 3`；missing_fields 只服务 level-3 门槛，与「完整度 5 是完整化上限」不矛盾（5 是累计上限，不是本函数目标）。
- 字段组合无矛盾：level 5 ⇒ level 4 ⇒ level 3 ⇒ level 2 ⇒ level 1 全部满足（累积定义）；application 与 size_spec 的 OR 只在 level 2 判定一次，level 5 的「size_spec 必有」由 level 4 的否定分支保证（material 或 size_spec 至少一个无 → 5 时二者都有）。
- 每层返回条件与上一层的否定互斥：0↔1（product_category）、1↔2（OR）、2↔3（quantity）、3↔4（destination∧required_by）、4↔5（material∧size_spec）。
