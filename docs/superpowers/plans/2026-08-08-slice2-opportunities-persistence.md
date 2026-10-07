# Slice 2 · opportunities 持久化实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让「插入一条机会 → 打分 → 看到分桶与门槛解释 → 状态推进到 lost 时强制要求 loss_reason」能跑通；机会关键字段带可追溯 Provenance、事实/推断结构分离；打分输入可回测、快照只增（DB 强制）、失败必有归因；事件发布写入与业务写入同事务落 outbox；为后续切片提供可用的机会域与持久化基建。

**Architecture:** 模块化单体。`domains/opportunities` 只定义 Protocol 与纯函数（不碰 SQLAlchemy）；Repository / Scorer / Service / UnitOfWork / EventBus(outbox) 具体实现放 `infra/db/` 与 `domains/opportunities/scorer.py|service_impl.py`。域间零直接导入：上层把派生值以扁平 DTO 传入。事件经 `EventBus` Protocol（outbox writer 绑定 UoW 同一 session）发布，发布写入半边本切片完成、投递轮询延后。

**Tech Stack:** Python 3.12+（conda `tradeos-py312`）、SQLAlchemy 2.x async + asyncpg、Alembic、PostgreSQL 16（本机 docker compose；集成测试用 testcontainers 独立容器）、pytest/pytest-asyncio。事件序列化用 JSON（安全类型 + 注册表白名单），**禁止 pickle**。

**Global Constraints:**
- 九条硬边界；本切片落地 2/4/5/8/9：金额 `Decimal`+币种成对、关键字段带 Provenance、事实/推断结构分离、全表 `tenant_id` + 查询强制过滤、`domains` 不碰 SQLAlchemy。
- **每任务**：提交前 `env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" python3 scripts/check_boundaries.py` 全绿；`env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" make check` 全绿；单独 commit + 单独 push。
- **无 Any/type-ignore/noqa 压制**；错误走 `shared.errors` 分类。
- **`score_snapshots`/`loss_records`/`provenance_records` 由 DB 触发器拒绝 UPDATE/DELETE**；`outbox_events` 仅 status 允许更新。
- **业务数字不写死**：`ScoringPolicy` 由上层注入；计划/代码不替老板决定数值。
- 集成测试需 Docker；无 Docker 环境 `@pytest.mark.skipif` 跳过并报告，不降级 sqlite。

**门禁命令前缀**：每条门禁用 `env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH"` 前缀运行——tradeos-py312 的 bin 目录置最前，再拼接当前 shell 的 PATH（`$PATH` 在运行时展开）；**不硬编码任何 Codex 临时路径**；`check_boundaries` 亦同。

**Step 7 的 CI watch 固定块**（每个任务 Step 7 均执行；gh 已认证）：
```bash
RUN_ID=$(gh run list --branch codex/phase1-implementation --commit "$(git rev-parse HEAD)" --json databaseId --jq '.[0].databaseId')
gh run watch "$RUN_ID" --exit-status
```

---

## 审计

### 一、事实

- `domains/opportunities/` 七件套齐全，全部 stub；`infra/db/base.py` 已存在；迁移仅 `0001_baseline_empty`。
- `domains` 与 `shared` 均不得 import sqlalchemy（`FORBIDDEN_SDK`）→ 具体实现放 `infra/db/`。
- `shared/events/bus.py` 明写 outbox 是 Phase 1 实现、`publish` 与业务写入同事务；HANDBOOK 切片 1 明确延后到切片 2 → 本切片完成发布写入半边。
- `ScoreSnapshot` 无 `tenant_id`/`snapshot_id`；`OpportunityCreateRequest` 缺门槛输入；`request_handoff` 无法组装完整包；`OpportunityService` 无 `mark_won`；`mark_lost` 无 `confirmed_by`；`wait_seconds` 是 property；`Opportunity` 无 `closed_by`/`assigned_by`。

### 二、文档冲突（C1–C5，同前轮安全决策）

- C1 状态机（01-domain-model vs models.py）→ 以 models.py 为准。
- C2 快照表名（`lead_scores` vs `ScoreSnapshot`）→ 建 `score_snapshots`。
- C3 门槛数（09 列 5 vs scoring.py 4）→ 去重是服务层幂等，以 4 门槛为准。
- C4 打分尺度（09「字典序」vs float `total_score`；`rank_bucket` 示例 `B` vs 高/中/低）→ 用 `SortKey` 三元 + tuple 比较，`total_score` 移除，分桶标签 `high/mid/low`。
- C5 快照不可变 vs 回填 → 快照只增（DB 触发器），归因存 `loss_records`。

### 三、提案（P1–P7，同前轮 + 本轮精确化）

- P1 outbox 发布写入半边本切片落地（`outbox_events` 完整承载 EventEnvelope，见 Schema 附录）。
- P2 `OpportunityUnitOfWork` 契约 + SqlAlchemy 实现（共享 session）；service 只依赖 `uow_factory + scorer`（policy 只在 scorer）。
- P3 `SortKey` 三元 + `ScoringPolicy`（带构造校验）替换 float。
- P4 `CRITICAL_FIELDS`（14 字段，见 S2-1）事实/推断严格化：create 只校验 present 子集、拒 `AGENT_INFERENCE`、推断不复制入 Opportunity；`validate_present_critical_provenance` 纯函数。
- P5 人工确认落库（`closed_by`/`closed_at`/`assigned_by`/`assigned_at`；`loss_records.confirmed_by/confirmed_at`；原子 `close_*_if_state`；普通 `update` 拒绝 state→WON/LOST）。
- P6 只增 DB 触发器强制。
- P7 `OpportunityWon` 事件 + `count_loss_reasons` 移除。

### 四、ADR 门槛分析

- 迁移加 6 表 + 触发器 → additive → 无 ADR。
- shared：`ScoreSnapshotId`/`LossRecordId` NewType + `OpportunityWon` 事件（additive）→ 无 ADR（最严解释可补轻量 ADR）。
- 域内契约变更 → 无 ADR。outbox 放 infra、域用 Protocol → 不违反依赖方向 → 无 ADR。

---

## 类型一致性核对表（含 handoff provenance 保存）

| 流 | DTO / 入参 | Service 签名 | UoW / Repo | ORM / 迁移列 | View / 事件 |
|---|---|---|---|---|---|
| create | `OpportunityCreateRequest`（含 `field_provenance: dict[str, Provenance]`、门槛输入） | `create_from_need(tenant_id, request)` | `uow.opportunities.add` + `uow.provenance.save(tenant_id, opp_id, field, prov)` ×N + `uow.bus.publish` | `opportunities` 业务列 + `provenance_records` + `outbox_events` | `OpportunityView` + `OpportunityQualified` |
| score | `ScoringInput` + scorer 内 `ScoringPolicy` | `scorer.score(uow.snapshots, tenant_id, opp_id, input)` | `uow.snapshots.add(tenant_id, snapshot)` | `score_snapshots.sort_evidence_rank/value_band/supply_rank` | `ScoreExplanation.sort_key` + `rank_bucket` |
| advance | `OpportunityState` | `transition(tenant_id, opp_id, target)`（拒 WON/LOST） | `uow.opportunities.advance_state(..., expected)` | `opportunities.state` | — |
| close-lost | `reason\|None`+`actor`+`confirmed_at` | `mark_lost(..., actor, confirmed_at)` | `close_lost_if_state(..., expected, ...)` + `uow.loss_records.add` + `uow.bus.publish` | `opportunities.loss_reason/died_at_state/closed_by/closed_at` + `loss_records` | `OpportunityLost` |
| close-won | `actor`+`confirmed_at` | `mark_won(..., actor, confirmed_at)` | `close_won_if_state(...)`（仅 NEGOTIATING）+ `uow.bus.publish` | `opportunities.state/closed_by/closed_at` | `OpportunityWon` |
| assign | `owner`+`assigned_by` | `assign(tenant_id, opp_id, owner, assigned_by)` | `uow.opportunities.assign_owner` | `opportunities.owner/assigned_by/assigned_at` | `OpportunityView.owner` |
| handoff | `HandoffCreateRequest`（含 `customer_verbatim_provenance`） | `request_handoff(tenant_id, request)` | **`uow.provenance.save(tenant_id, handoff_id, "customer_verbatim", prov)`** + `uow.handoffs.add` + `uow.bus.publish` | `handoffs` + `provenance_records(entity_type='handoff')` | `HandoffRequested` |
| accept | `accepted_by` | `accept_handoff(tenant_id, handoff_id, accepted_by)` | `uow.handoffs.accept_if_requested(...)`（原子） | `handoffs.state/accepted_by/accepted_at` | `HandoffAccepted` |
| outbox | — | `uow.bus.publish(event)` | `PostgresEventBus(session)` INSERT | `outbox_events`（见 Schema 附录） | 投递轮询（后续） |

---

## Schema 合同附录（六表，逐列）

> 通用：`tenant_id` 全表 NOT NULL（硬边界 8）；主键用带前缀字符串 ID（VARCHAR(32)）；时间一律 `TIMESTAMPTZ`（UTC）；金额 `NUMERIC(18,2)` + `CHAR(3)` 币种成对。FK 策略：本迁移不引外库（Phase 1 表分批落地）；`score_snapshots` 对 `opportunity_id` **不加 FK**（悬空依据见下）；`handoffs`/`loss_records` 的 `opportunity_id` 加**复合 FK `(tenant_id, opportunity_id) REFERENCES opportunities(tenant_id, opportunity_id) ON DELETE RESTRICT`**（禁止 A 租户记录引用 B 租户机会，与各表约束一致）；`provenance_records.entity_id` 为多态引用（entity_type 决定目标表）→ 不加 FK，由 repository/service 保证一致。

### `opportunities`

| 列 | 类型 | null | default |
|---|---|---|---|
| opportunity_id | VARCHAR(32) | NO | PK |
| tenant_id | VARCHAR(32) | NO | |
| account_id | VARCHAR(32) | NO | |
| account_name | VARCHAR(200) | NO | |
| country | VARCHAR(100) | NO | |
| need_id | VARCHAR(32) | NO | |
| product_category | VARCHAR(100) | NO | |
| state | VARCHAR(20) | NO | `'qualified'` |
| created_at | TIMESTAMPTZ | NO | `now()` |
| quantity | INT | YES | |
| spec_summary | TEXT | YES | |
| application | TEXT | YES | |
| destination | VARCHAR(100) | YES | |
| required_by | DATE | YES | |
| target_price_amount / target_price_currency | NUMERIC(18,2) / CHAR(3) | YES | |
| decision_maker | TEXT | YES | |
| current_supply_solution | TEXT | YES | |
| current_supply_problem | TEXT | YES | |
| can_source | BOOLEAN | YES | |
| estimated_cost_amount / estimated_cost_currency | NUMERIC(18,2) / CHAR(3) | YES | |
| estimated_profit_amount / estimated_profit_currency | NUMERIC(18,2) / CHAR(3) | YES | |
| owner / assigned_by / assigned_at | VARCHAR(32)/VARCHAR(32)/TIMESTAMPTZ | YES | |
| next_action / next_action_due | TEXT / TIMESTAMPTZ | YES | |
| loss_reason / died_at_state | VARCHAR(32)/VARCHAR(20) | YES | |
| closed_by / closed_at | VARCHAR(32)/TIMESTAMPTZ | YES | |

约束：`PRIMARY KEY (opportunity_id)`；**`UNIQUE (tenant_id, need_id)`**（并发幂等兜底）；**`UNIQUE (tenant_id, opportunity_id)`**（供复合 FK 引用）；`INDEX (tenant_id, state)`；`INDEX (tenant_id, owner, state)`。
CHECK：每对金额 `(amount IS NULL AND currency IS NULL) OR (amount IS NOT NULL AND currency IS NOT NULL)`（target_price / estimated_cost / estimated_profit）；`state = 'lost' ⇒ loss_reason IS NOT NULL AND died_at_state IS NOT NULL AND closed_by IS NOT NULL AND closed_at IS NOT NULL`；`state = 'won' ⇒ closed_by IS NOT NULL AND closed_at IS NOT NULL`；`closed_at IS NULL OR state IN ('won','lost')`（非终态不得 closed）。

### `score_snapshots`

| 列 | 类型 | null | default |
|---|---|---|---|
| snapshot_id | VARCHAR(32) | NO | PK |
| tenant_id | VARCHAR(32) | NO | |
| opportunity_id | VARCHAR(32) | NO | **无 FK** |
| scored_at | TIMESTAMPTZ | NO | |
| scorer_version | VARCHAR(32) | NO | |
| passed_gates / failed_gates | JSONB | NO | |
| evidence_tier | VARCHAR(32) | YES | |
| estimated_value_amount / estimated_value_currency | NUMERIC(18,2)/CHAR(3) | YES | |
| supply_available | BOOLEAN | YES | |
| sort_evidence_rank | INT | NO | |
| sort_value_band | INT | NO | |
| sort_supply_rank | INT | NO | |
| gate_reasons | JSONB | NO | |
| rank_bucket | VARCHAR(10) | NO | |

约束：`PRIMARY KEY (snapshot_id)`；`INDEX (tenant_id, opportunity_id, scored_at)`；**`BEFORE UPDATE OR DELETE` 触发器抛错**（只增）；CHECK 金额成对（estimated_value）。
**悬空依据**：`create_from_need` 被门槛拦截时仍存失败快照，其 `opportunity_id` 是生成后未创建的机会 ID——失败快照是「被拒记录」，是调整门槛的依据；若加 FK 则此类快照无法入库。故 `score_snapshots` 不设 FK，完整性由 Repository 与 scorer 保证。

### `handoffs`

| 列 | 类型 | null | default |
|---|---|---|---|
| handoff_id | VARCHAR(32) | NO | PK |
| tenant_id | VARCHAR(32) | NO | |
| opportunity_id | VARCHAR(32) | NO | FK→opportunities |
| trigger | VARCHAR(32) | NO | |
| state | VARCHAR(20) | NO | `'requested'` |
| requested_at | TIMESTAMPTZ | NO | |
| account_name | VARCHAR(200) | NO | |
| country | VARCHAR(100) | NO | |
| why_valuable | TEXT | NO | |
| customer_verbatim | TEXT | NO | |
| assigned_to / manager | VARCHAR(32)/VARCHAR(32) | YES | |
| accepted_at / accepted_by | TIMESTAMPTZ/VARCHAR(32) | YES | |
| how_we_found_them / validated_need_summary | TEXT | YES | |
| missing_information / already_sent / commitments_made / evidence_links | JSONB | YES | |
| conversation_summary / suggested_next_step | TEXT | YES | |

约束：`PRIMARY KEY (handoff_id)`；`INDEX (tenant_id, state, requested_at)`；**复合 FK `(tenant_id, opportunity_id) REFERENCES opportunities(tenant_id, opportunity_id) ON DELETE RESTRICT`**（禁止 A 租户记录引用 B 租户机会）。

### `loss_records`

| 列 | 类型 | null | default |
|---|---|---|---|
| loss_record_id | VARCHAR(32) | NO | PK |
| tenant_id | VARCHAR(32) | NO | |
| opportunity_id | VARCHAR(32) | NO | FK→opportunities |
| loss_reason | VARCHAR(32) | NO | |
| died_at_state | VARCHAR(20) | NO | |
| detail | TEXT | YES | |
| evidence_tier | VARCHAR(32) | YES | |
| confirmed_by | VARCHAR(32) | NO | |
| confirmed_at | TIMESTAMPTZ | NO | |
| recorded_at | TIMESTAMPTZ | NO | |

约束：`PRIMARY KEY (loss_record_id)`；`INDEX (tenant_id, loss_reason, died_at_state)`；**复合 FK `(tenant_id, opportunity_id) REFERENCES opportunities(tenant_id, opportunity_id)`**；`confirmed_by`/`confirmed_at` NOT NULL（人工确认必留痕）；**触发器只增**。

### `provenance_records`

| 列 | 类型 | null | default |
|---|---|---|---|
| provenance_id | VARCHAR(32) | NO | PK |
| tenant_id | VARCHAR(32) | NO | |
| entity_type | VARCHAR(32) | NO | `'opportunity'`/`'handoff'` |
| entity_id | VARCHAR(32) | NO | 多态，无 FK |
| field_name | VARCHAR(64) | NO | |
| source_type | VARCHAR(32) | NO | |
| source_id | VARCHAR(200) | NO | |
| extracted_by | VARCHAR(64) | NO | |
| extracted_at | TIMESTAMPTZ | NO | |
| confirmed_by / confirmed_at | VARCHAR(32)/TIMESTAMPTZ | YES | |
| source_url / page_hash | VARCHAR(2000)/VARCHAR(200) | YES | |

约束：`PRIMARY KEY (provenance_id)`；`INDEX (tenant_id, entity_type, entity_id, field_name, extracted_at)`（允许同字段多版本来源历史追加，**不设唯一约束**）；**触发器只增**。多态依据：同一字段可挂在机会或接管包上，单 FK 无法指向两表；租户一致性由 repository/service 强制。

### `outbox_events`（完整承载 EventEnvelope）

| 列 | 类型 | null | default |
|---|---|---|---|
| event_id | VARCHAR(32) | NO | PK（`new_id("evt")`） |
| tenant_id | VARCHAR(32) | NO | |
| event_type | VARCHAR(64) | NO | 注册表白名单 |
| event_payload | JSONB | NO | |
| attempt | INT | NO | `1` |
| published_at | TIMESTAMPTZ | NO | |
| trace_id | VARCHAR(32) | NO | `new_id("trc")` |
| run_id | VARCHAR(32) | YES | 来自 DomainEvent.run_id |
| occurred_at | TIMESTAMPTZ | NO | 来自 DomainEvent |
| status | VARCHAR(16) | NO | `'pending'` |
| delivered_at | TIMESTAMPTZ | YES | |

约束：`PRIMARY KEY (event_id)`（event_id 唯一 = EventEnvelope 幂等键）；`INDEX (tenant_id, status)`；**仅 status/delivered_at 允许更新，无只增触发器**；CHECK `attempt >= 1`；CHECK `status IN ('pending','delivered')`。

**publish 生成**：`PostgresEventBus.publish(event)` 用注入时钟 `now` 构造 `EventEnvelope(event, event_id=new_id("evt"), attempt=1, published_at=now(), trace_id=new_id("trc"))`；`event_type = type(event).__name__` 必须在 `EVENT_REGISTRY` 白名单（未注册抛 `ValidationError`）；`event_payload` 由 JSON 序列化器生成（NewType→str、datetime→ISO、Enum→value、Money→{amount,currency}、嵌套 dataclass 递归）；禁止 pickle。

---

## 文件职责地图

```text
shared/schemas/identifiers.py       新增 ScoreSnapshotId / LossRecordId（additive）
shared/events/catalog.py            新增 OpportunityWon(tenant_id, occurred_at, run_id, opportunity_id, closed_by)
domains/opportunities/models.py     Opportunity(+closed_by/closed_at/assigned_by/assigned_at) / ScoreSnapshot(+tenant_id,snapshot_id,sort_key) / LossRecord(新) / HandoffPacket(wait_seconds 方法) / SortKey(新) / CRITICAL_FIELDS
domains/opportunities/schemas.py     OpportunityCreateRequest(+门槛输入+field_provenance) / HandoffCreateRequest(+customer_verbatim_provenance) / ScoreExplanation(+sort_key) / 既有 DTO
domains/opportunities/repository.py  OpportunityRepository(+advance_state/close_won_if_state/close_lost_if_state/assign_owner, -count_loss_reasons, update 拒 WON/LOST) / ScoreSnapshotRepository(add 带 tenant) / HandoffRepository(+accept_if_requested) / LossRecordRepository(新) / FieldProvenanceRepository(新) / OpportunityUnitOfWork(新)
domains/opportunities/scoring.py     Gate / GateResult / ScoringInput / ScoringPolicy(带 __post_init__ 校验) / check_gates / compute_score→SortKey / rank_bucket(evidence_rank, policy)
domains/opportunities/scorer.py      OpportunityScorerImpl(policy)  [新建]
domains/opportunities/service_impl.py  OpportunityServiceImpl(uow_factory, scorer, now) + validate_present_critical_provenance  [新建]
domains/opportunities/errors.py      MissingFieldProvenanceError / AgentInferenceProvenanceError（ValidationError 子类）
infra/db/session.py                  async engine/session 工厂
infra/db/tables.py                   六表声明式模型
infra/db/outbox.py                   EVENT_REGISTRY + JSON 序列化器 + PostgresEventBus(session)
infra/db/repositories/opportunities.py  五 Repository 实现
infra/db/unit_of_work.py            SqlAlchemyOpportunityUnitOfWork
tests/unit/test_opportunities_contracts.py / test_opportunities_models.py / test_opportunities_scoring.py / test_opportunities_scorer.py / test_opportunities_service.py / test_opportunities_handoff.py / test_outbox_serialization.py
tests/integration/conftest.py / test_session_factory.py / test_migrations.py / test_repositories.py / test_outbox_transaction.py / test_demo_opportunities.py
scripts/demo_opportunities.py
```

---

## 任务列表

### 任务 S2-1：契约对齐（含 catalog 事件、SortKey、ScoringPolicy、UoW、outbox 接口、人工确认字段、Provenance 错误）

**Files**
- Modify: `shared/schemas/identifiers.py`、`shared/events/catalog.py`、`domains/opportunities/models.py`、`schemas.py`、`repository.py`、`scoring.py`、`service.py`、`events.py`、`errors.py`
- Create: `tests/unit/test_opportunities_contracts.py`

- [ ] **Step 1：写失败测试（契约形状 + 事件）** — `test_opportunities_contracts.py` 断言：`ScoreSnapshot(tenant_id=…, snapshot_id=…, sort_key=SortKey(1,0,0), …)` 可构造；`SortKey(7,0,0) > SortKey(1,9,2)`（tuple 序，证据主导）；`ScoringPolicy(version="x", value_band_boundaries=(Money(…),), bucket_map={1: "low",…,7:"high"})` 可构造且 `__post_init__` 对「边界为空/非升序/异币种」「bucket_map 未覆盖 1..7 / 值非 high/mid/low」抛 ValidationError；`LossRecord`/`HandoffCreateRequest`（含 `customer_verbatim_provenance`）可实例化；`OpportunityCreateRequest(..., category_allowed=…, minimum_order_value=…, field_provenance={...})` 可构造；`HandoffPacket.wait_seconds(now)` 可调用；`OpportunityWon` 可导入且 `issubclass(DomainEvent)`（字段 `opportunity_id`、`closed_by`）；`CRITICAL_FIELDS` 含 14 字段（account_name/country/quantity/spec_summary/application/destination/required_by/target_price/decision_maker/current_supply_solution/current_supply_problem/can_source/estimated_cost/estimated_profit）；`Opportunity`/`OpportunityCreateRequest` 含 `account_name`/`country`（扁平事实快照）；`ScoreSnapshot` 含 `gate_reasons`；`HandoffPolicy(sla_seconds, backlog_threshold)` 可构造且 `__post_init__` 对两值 `<=0` 抛 ValidationError；`OpportunityWon(..., closed_by=None)` 抛错。
- [ ] **Step 2：运行 RED** — `conda run -n tradeos-py312 pytest tests/unit/test_opportunities_contracts.py -q -W error`；预期形状断言失败（TypeError/ImportError 转行为失败）。
- [ ] **Step 3：最小实现（只改签名/字段/类型，函数体保持 NotImplementedError）** — 按类型一致性表 + P2–P5 更新契约；catalog 加 `OpportunityWon(opportunity_id: OpportunityId, closed_by: EmployeeId | None = None)` 且 `__post_init__` 拒 `closed_by is None`（dataclass 继承需默认值，但空值即抛错，防人工确认事件被伪造为空）；`ScoringPolicy.__post_init__` 加校验；`errors.py` 加两个 Provenance 错误；`ScoreSnapshot` 用 `sort_key: SortKey` 替换 `total_score`/`factor_scores` 并加 `gate_reasons: dict[str, str]`；`Opportunity`/`OpportunityCreateRequest` 加 `account_name`/`country`；`HandoffPolicy` frozen dataclass（`sla_seconds`/`backlog_threshold` 均 `>0` 校验、无默认业务数字）。
- [ ] **Step 4：GREEN** — 同上命令；预期全部通过。
- [ ] **Step 5：全量门禁** — `env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" make check`；`env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" python3 scripts/check_boundaries.py`（新事件注册一致）；`git diff --check`。
- [ ] **Step 6：仅 stage 精确文件** — `git add shared/schemas/identifiers.py shared/events/catalog.py domains/opportunities/models.py domains/opportunities/schemas.py domains/opportunities/repository.py domains/opportunities/scoring.py domains/opportunities/service.py domains/opportunities/events.py domains/opportunities/errors.py tests/unit/test_opportunities_contracts.py`
- [ ] **Step 7：commit + push + 核对** —
```bash
git commit -m "refactor(opportunities): align contracts for persistence"
git push
git rev-parse HEAD
git ls-remote origin refs/heads/codex/phase1-implementation
# CI watch：见「门禁命令前缀」节的固定块（gh run list 取本次 head SHA 的 run ID，再 gh run watch）
```

---

### 任务 S2-2：infra/db/session.py + 集成测试夹具

**Files**
- Create: `infra/db/session.py`、`tests/integration/conftest.py`、`tests/integration/test_session_factory.py`

- [ ] **Step 1：写失败测试** — `test_session_factory.py::test_select_one_roundtrip`（`SELECT 1`）、`test_get_session_context`。
- [ ] **Step 2：运行 RED** — `env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" pytest tests/integration/test_session_factory.py -q -W error`；预期 `infra.db.session` 缺失（延迟导入转行为失败）。
- [ ] **Step 3：最小实现** — `session.py`：`create_engine_from(url)`/`session_factory(url)`/`get_session`；`conftest.py`：testcontainers `PostgresContainer("pgvector/pgvector:pg16")`（session 级）+ subprocess `alembic upgrade head`（`DATABASE_URL` 指向容器）+ `integration_engine`/`integration_session`；`@pytest.mark.skipif`（Docker 不可用）。
- [ ] **Step 4：GREEN** — 同上命令通过。
- [ ] **Step 5：全量门禁** — `env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" make check`；`env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" python3 scripts/check_boundaries.py`；`git diff --check`。
- [ ] **Step 6：stage** — `git add infra/db/session.py tests/integration/conftest.py tests/integration/test_session_factory.py`
- [ ] **Step 7：commit + push + 核对** —
```bash
git commit -m "feat(infra): add async session factory and integration fixtures"
git push
git rev-parse HEAD
git ls-remote origin refs/heads/codex/phase1-implementation
# CI watch：见「门禁命令前缀」节的固定块（gh run list 取本次 head SHA 的 run ID，再 gh run watch）
```

---

### 任务 S2-3：迁移 — 六张表 + 只增触发器

**Files**
- Create: `migrations/versions/0002_opportunities.py`、`tests/integration/test_migrations.py`

- [ ] **Step 1：写失败测试** — `test_migrations.py`：独立容器 `alembic upgrade head` → 断言六表存在、每表含 `tenant_id`、`opportunities` 有 `UNIQUE (tenant_id, need_id)` → `alembic downgrade base` → 表消失 → 再 `upgrade head`（round-trip）；`test_append_only_trigger`：对 `score_snapshots`/`loss_records`/`provenance_records` 直接 `UPDATE`/`DELETE` 被触发器拒绝；`test_outbox_status_updatable`：`outbox_events.status` 可更新为 `delivered`；`test_db_check_constraints`：金额成对（单边插入被拒）、`lost` 缺 closed 字段被拒、`won` 缺 closed_by/closed_at 被拒、非终态带 closed_at 被拒、`loss_records.confirmed_by`/`confirmed_at` 缺失被拒、`outbox_events.attempt=0`/非法 status 被拒。
- [ ] **Step 2：运行 RED** — 迁移前六表缺失 → 断言失败。
- [ ] **Step 3：最小实现** — 按 Schema 附录精确实现 0002（六表逐列 + 约束 + 三张审计表触发器 + `outbox_events` 无只增触发器）。
- [ ] **Step 4：GREEN** — round-trip + 触发器负测通过。
- [ ] **Step 5：全量门禁** — `env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" make check`；`env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" python3 scripts/check_boundaries.py`；`git diff --check`；本地 dev Postgres `env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" alembic upgrade head && env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" alembic current`（预期 0002 head）。
- [ ] **Step 6：stage** — `git add migrations/versions/0002_opportunities.py tests/integration/test_migrations.py`
- [ ] **Step 7：commit + push + 核对** —
```bash
git commit -m "feat(db): add opportunities persistence migrations"
git push
git rev-parse HEAD
git ls-remote origin refs/heads/codex/phase1-implementation
# CI watch：见「门禁命令前缀」节的固定块（gh run list 取本次 head SHA 的 run ID，再 gh run watch）
```

---

### 任务 S2-4：ORM 表 + Opportunity / ScoreSnapshot Repository

**Files**
- Create: `infra/db/tables.py`、`infra/db/repositories/__init__.py`、`infra/db/repositories/opportunities.py`、`tests/integration/test_repositories.py`

- [ ] **Step 1：写失败测试** — `test_repositories.py`：`test_opportunity_crud_roundtrip`、`test_find_by_need_idempotent`、`test_unique_need_idempotent_race`（并发插入同 `(tenant_id, need_id)` → 一个成功一个 `IntegrityError`，service 捕获后返回既有）、`test_list_by_owner_and_state`、`test_tenant_isolation`（A 租户查/改 B 租户行→空/0 行）、`test_snapshot_append_only`（add 两次→latest 最新且旧仍在）、`test_update_rejects_terminal_state`（`update(opp.state=WON)` 抛错）、`test_advance_state_atomic`、`test_close_won_only_from_negotiating`（非 NEGOTIATING→False）、`test_close_lost_expected_state`、`test_assign_owner_records_actor`（owner/assigned_by/assigned_at 落库）。
- [ ] **Step 2：运行 RED** — 实现缺失 → 行为失败。
- [ ] **Step 3：最小实现** — `tables.Base` + 六表 ORM（列与迁移逐列一致）；`OpportunityRepositoryImpl(TenantScopedRepository)`：CRUD（`update` 遇 `state in {WON, LOST}` 抛 `InvalidStateTransition`）+ `advance_state`/`close_won_if_state`（WHERE state='negotiating'）/`close_lost_if_state`（WHERE state=expected）/`assign_owner`（条件更新返回 bool）；`ScoreSnapshotRepositoryImpl`：`add(tenant_id, snapshot)` 只 insert、`latest_for_opportunity`、`list_for_backtest`。
- [ ] **Step 4：GREEN** — 集成通过。
- [ ] **Step 5：全量门禁** — `env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" make check`；`env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" python3 scripts/check_boundaries.py`；`git diff --check`。
- [ ] **Step 6：stage** — `git add infra/db/tables.py infra/db/repositories/__init__.py infra/db/repositories/opportunities.py tests/integration/test_repositories.py`
- [ ] **Step 7：commit + push + 核对** —
```bash
git commit -m "feat(infra): add opportunity and snapshot repositories"
git push
git rev-parse HEAD
git ls-remote origin refs/heads/codex/phase1-implementation
# CI watch：见「门禁命令前缀」节的固定块（gh run list 取本次 head SHA 的 run ID，再 gh run watch）
```

---

### 任务 S2-5：Handoff / LossRecord / FieldProvenance Repository

**Files**
- Modify: `infra/db/repositories/opportunities.py`、`tests/integration/test_repositories.py`

- [ ] **Step 1：写失败测试** — `test_handoff_pending_ordered_by_requested_at`、`test_accept_if_requested_atomic`（并发只一个 True）、`test_loss_record_append_only_and_breakdown`（`count_by_reason_and_state` 二维）、`test_provenance_save_list_roundtrip`（含 `entity_type='handoff'`、`field_name='customer_verbatim'`）、`test_provenance_same_field_two_versions_preserved`（同字段两条来源均保留——append-only 历史，无唯一约束）、`test_provenance_tenant_isolation`。
- [ ] **Step 2：运行 RED** — 方法缺失 → 行为失败。
- [ ] **Step 3：最小实现** — `HandoffRepositoryImpl`（`accept_if_requested` WHERE state='requested'）、`LossRecordRepositoryImpl`（add 只增 + GROUP BY 二维）、`FieldProvenanceRepositoryImpl`（`shared.Provenance`↔`provenance_records` 全字段）。
- [ ] **Step 4：GREEN** — 集成通过。
- [ ] **Step 5：全量门禁** — `env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" make check`；`env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" python3 scripts/check_boundaries.py`；`git diff --check`。
- [ ] **Step 6：stage** — `git add infra/db/repositories/opportunities.py tests/integration/test_repositories.py`
- [ ] **Step 7：commit + push + 核对** —
```bash
git commit -m "feat(infra): add handoff loss and provenance repositories"
git push
git rev-parse HEAD
git ls-remote origin refs/heads/codex/phase1-implementation
# CI watch：见「门禁命令前缀」节的固定块（gh run list 取本次 head SHA 的 run ID，再 gh run watch）
```

---

### 任务 S2-6：UoW + outbox writer（同事务发布写入）

**Files**
- Create: `infra/db/unit_of_work.py`、`infra/db/outbox.py`、`tests/unit/test_outbox_serialization.py`、`tests/integration/test_outbox_transaction.py`

- [ ] **Step 1：写失败测试** — `test_outbox_serialization.py`：事件 JSON 序列化 round-trip（含 TenantId/RunId/enum/datetime/Money/嵌套 dataclass）；**无 pickle 路径**（序列化器仅 JSON）；`test_publish_requires_registered_type`（未注册事件类型→`ValidationError`）。`test_outbox_transaction.py`：**commit 后业务行+outbox 行同时存在；rollback 后二者都不存在**；`test_uow_rollback_on_exception`。
- [ ] **Step 2：运行 RED** — `infra.db.outbox`/`infra.db.unit_of_work` 缺失 → 行为失败。
- [ ] **Step 3：最小实现** — `outbox.py`：`EVENT_REGISTRY`（白名单：本切片发布的事件类）+ JSON 序列化器（类型安全）+ `PostgresEventBus(session)`（publish 按 P1 生成 EventEnvelope 并 INSERT，subscribe 内存注册）。`unit_of_work.py`：`SqlAlchemyOpportunityUnitOfWork`——一个 `AsyncSession`，构造五 repo + `PostgresEventBus`，`__aenter__` 返回自身、`__aexit__` 无异常 commit / 有异常 rollback。
- [ ] **Step 4：GREEN** — 序列化单测 + 事务集成通过。
- [ ] **Step 5：全量门禁** — `env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" make check`；`env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" python3 scripts/check_boundaries.py`；`git diff --check`。
- [ ] **Step 6：stage** — `git add infra/db/unit_of_work.py infra/db/outbox.py tests/unit/test_outbox_serialization.py tests/integration/test_outbox_transaction.py`
- [ ] **Step 7：commit + push + 核对** —
```bash
git commit -m "feat(infra): add unit of work and outbox event bus"
git push
git rev-parse HEAD
git ls-remote origin refs/heads/codex/phase1-implementation
# CI watch：见「门禁命令前缀」节的固定块（gh run list 取本次 head SHA 的 run ID，再 gh run watch）
```

---

### 任务 S2-7：领域模型行为（纯逻辑）

**Files**
- Modify: `domains/opportunities/models.py`
- Create: `tests/unit/test_opportunities_models.py`

- [ ] **Step 1：写失败测试** — `test_can_transition_to_matrix`（含 `CONTACTED→QUOTED` 合法、`WON`/`LOST` 终态空集）、`test_mark_lost_records_died_at_state`、`test_wait_seconds_accepted_vs_pending`（注入固定 `now`）。
- [ ] **Step 2：运行 RED** — stub NotImplementedError / property 调错。
- [ ] **Step 3：最小实现** — 按 docstring；`wait_seconds(now)` 显式时间参数。
- [ ] **Step 4：GREEN** — `conda run -n tradeos-py312 pytest tests/unit/test_opportunities_models.py -q -W error` 通过。
- [ ] **Step 5：全量门禁** — `env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" make check`；`env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" python3 scripts/check_boundaries.py`；`git diff --check`。
- [ ] **Step 6：stage** — `git add domains/opportunities/models.py tests/unit/test_opportunities_models.py`
- [ ] **Step 7：commit + push + 核对** —
```bash
git commit -m "feat(opportunities): implement domain model behavior"
git push
git rev-parse HEAD
git ls-remote origin refs/heads/codex/phase1-implementation
# CI watch：见「门禁命令前缀」节的固定块（gh run list 取本次 head SHA 的 run ID，再 gh run watch）
```

---

### 任务 S2-8：scoring 纯函数（SortKey / compute_score / rank_bucket / ScoringPolicy 校验）

**Files**
- Modify: `domains/opportunities/scoring.py`
- Create: `tests/unit/test_opportunities_scoring.py`

- [ ] **Step 1：写失败测试** — `test_check_gates_no_short_circuit`、`test_value_above_floor_none_reason`（「预计金额未知」）、`test_evidence_below_minimum_fails`、`test_compute_score_returns_sortkey`（evidence_rank 由稳定映射 1..7、value_band 由 policy 边界、supply_rank 0/1/2）、`test_value_currency_mismatch_raises`（estimated_value 与 boundaries 异币种→`CurrencyMismatchError`）、`test_sortkey_tuple_ordering`（`SortKey(7,0,0) > SortKey(1,9,2)`）、`test_rank_bucket_from_policy`、`test_policy_rejects_bad_boundaries`（空/非升序/异币种）、`test_policy_rejects_incomplete_bucket_map`（未覆盖 1..7 或值非 high/mid/low）。**无 float/加权/对数**。
- [ ] **Step 2：运行 RED** — stub NotImplementedError / 类型不符。
- [ ] **Step 3：最小实现** — `SortKey`（frozen、order）；`_TIER_RANK` 稳定映射（LOW=1…EXTREME=7）；`compute_score(input, policy) -> SortKey`（币种不匹配抛 `CurrencyMismatchError`）；`rank_bucket(evidence_rank, policy)`；`ScoringPolicy.__post_init__` 校验；失败快照哨兵 `SortKey(0,0,0)` 在 scorer 侧定义并注释为「系统保留哨兵，非业务权重」。
- [ ] **Step 4：GREEN** — `conda run -n tradeos-py312 pytest tests/unit/test_opportunities_scoring.py -q -W error` 通过。
- [ ] **Step 5：全量门禁** — `env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" make check`；`env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" python3 scripts/check_boundaries.py`；`git diff --check`。
- [ ] **Step 6：stage** — `git add domains/opportunities/scoring.py tests/unit/test_opportunities_scoring.py`
- [ ] **Step 7：commit + push + 核对** —
```bash
git commit -m "feat(opportunities): implement scoring sort key and gates"
git push
git rev-parse HEAD
git ls-remote origin refs/heads/codex/phase1-implementation
# CI watch：见「门禁命令前缀」节的固定块（gh run list 取本次 head SHA 的 run ID，再 gh run watch）
```

---

### 任务 S2-9：OpportunityScorer 实现（策略注入 + 快照持久化）

**Files**
- Create: `domains/opportunities/scorer.py`、`tests/unit/test_opportunities_scorer.py`

- [ ] **Step 1：写失败测试** — `test_failed_gates_store_snapshot`（`sort_key=SortKey(0,0,0)` 哨兵、failed 全、bucket=low、带 tenant/snapshot_id/version，经 fake `add(tenant_id, snapshot)` 断言 tenant 传入）、`test_passed_gates_store_sortkey`、`test_snapshot_stores_gate_reasons`（`GateResult.reasons` 落入 `snapshot.gate_reasons`）、`test_snapshot_immutable`（fake 记录无 update 调用）。
- [ ] **Step 2：运行 RED** — `scorer.py` 缺失 → 行为失败。
- [ ] **Step 3：最小实现** — `OpportunityScorerImpl(policy)`：`score(snapshots, tenant_id, opportunity_id, input)`——`check_gates` 未过存失败快照（哨兵 SortKey(0,0,0)）、过则 `compute_score`+`rank_bucket`；`snapshot_id=new_id("snap")`、`tenant_id`、`scorer_version=policy.version`。
- [ ] **Step 4：GREEN** — `conda run -n tradeos-py312 pytest tests/unit/test_opportunities_scorer.py -q -W error` 通过。
- [ ] **Step 5：全量门禁** — `env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" make check`；`env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" python3 scripts/check_boundaries.py`；`git diff --check`。
- [ ] **Step 6：stage** — `git add domains/opportunities/scorer.py tests/unit/test_opportunities_scorer.py`
- [ ] **Step 7：commit + push + 核对** —
```bash
git commit -m "feat(opportunities): implement opportunity scorer"
git push
git rev-parse HEAD
git ls-remote origin refs/heads/codex/phase1-implementation
# CI watch：见「门禁命令前缀」节的固定块（gh run list 取本次 head SHA 的 run ID，再 gh run watch）
```

---

### 任务 S2-10：OpportunityService 核心（UoW / create / assign / transition / mark_lost / mark_won）

**Files**
- Create: `domains/opportunities/service_impl.py`、`tests/unit/test_opportunities_service.py`

- [ ] **Step 1：写失败测试** — `test_create_idempotent`（重复 need_id 返回既有）、`test_create_unique_race_returns_existing`（`IntegrityError` 捕获→重查返回）、`test_create_failed_gates_returns_none_no_event`、`test_create_passed_publishes_and_saves_provenance`、`test_create_rejects_agent_inference_provenance`、`test_create_requires_provenance_for_present_critical_fields`、`test_transition_rejects_won_and_lost`、`test_transition_atomic`、`test_mark_lost_none_reason_raises_missing`、`test_mark_lost_order_and_persistence`（**断言调用顺序**：close_lost_if_state→loss_records.add→bus.publish，且 closed_by/closed_at/loss_records.confirmed_by/confirmed_at 落库）、`test_mark_won_only_from_negotiating`、`test_mark_won_publishes_opportunity_won`、`test_assign_records_actor`、`test_create_saves_account_name_and_country`（落库 + 纳入 Provenance 校验）。
- [ ] **Step 2：运行 RED** — `service_impl.py` 缺失 → 行为失败。
- [ ] **Step 3：最小实现** — `OpportunityServiceImpl(uow_factory, scorer, handoff_policy, now=datetime.now)`（**依赖 uow_factory + scorer + handoff_policy**；policy 只在 scorer）：各方法 `async with uow:`；`create_from_need` 保存 `account_name`/`country` 并纳入 `validate_present_critical_provenance`；`mark_lost(reason: LossReason | None, ...)`——None 抛 `MissingLossReasonError`；终结只走 `close_*_if_state`（普通 `update` 已拒 WON/LOST）；`validate_present_critical_provenance`（present 的 CRITICAL_FIELDS 缺来源抛 `MissingFieldProvenanceError`、含 `AGENT_INFERENCE` 抛 `AgentInferenceProvenanceError`）。
- [ ] **Step 4：GREEN** — `conda run -n tradeos-py312 pytest tests/unit/test_opportunities_service.py -q -W error` 通过。
- [ ] **Step 5：全量门禁** — `env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" make check`；`env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" python3 scripts/check_boundaries.py`；`git diff --check`。
- [ ] **Step 6：stage** — `git add domains/opportunities/service_impl.py tests/unit/test_opportunities_service.py`
- [ ] **Step 7：commit + push + 核对** —
```bash
git commit -m "feat(opportunities): implement opportunity service core"
git push
git rev-parse HEAD
git ls-remote origin refs/heads/codex/phase1-implementation
# CI watch：见「门禁命令前缀」节的固定块（gh run list 取本次 head SHA 的 run ID，再 gh run watch）
```

---

### 任务 S2-11：Handoff 与查询

**Files**
- Modify: `domains/opportunities/service_impl.py`
- Create: `tests/unit/test_opportunities_handoff.py`

- [ ] **Step 1：写失败测试** — `test_request_handoff_incomplete_rejected`、`test_request_handoff_requires_verbatim_provenance`（source_type 非 conversation/upload/employee_input 被拒）、`test_request_handoff_saves_verbatim_provenance`（**断言 `uow.provenance.save(tenant_id, handoff_id, "customer_verbatim", prov)` 被调用**）、`test_request_handoff_idempotent`、`test_accept_handoff_concurrent`、`test_get_queue_stats_uses_policy`（注入 `HandoffPolicy` 判定 breached/backlogged，不临场写死）、`test_get_view_gate_explanation`（`gate_reasons` 完整构造 `ScoreExplanation`）、`test_loss_reason_breakdown_2d`、`test_get_view_includes_score_summary`。
- [ ] **Step 2：运行 RED** — 方法未实现 → 行为失败。
- [ ] **Step 3：最小实现** — `request_handoff(tenant_id, request: HandoffCreateRequest)`：校验 verbatim_provenance → `uow.provenance.save(tenant_id, handoff_id, "customer_verbatim", prov)` → `uow.handoffs.add` → `uow.bus.publish(HandoffRequested)`；`accept_handoff`（`accept_if_requested` 原子 + 发布 `HandoffAccepted`）；`get_queue_stats`（深度/最久/按员工，`breached_count` 用 `handoff_policy.sla_seconds` 判超 SLA、`is_backlogged` 用 `handoff_policy.backlog_threshold`，超阈值发 `HandoffQueueBacklogged`）；`get`（从最新快照完整构造 `ScoreExplanation`，含 `gate_reasons`）/`list_for_employee`/`loss_reason_breakdown`（读 `LossRecordRepository`）。
- [ ] **Step 4：GREEN** — `conda run -n tradeos-py312 pytest tests/unit/test_opportunities_handoff.py -q -W error` 通过。
- [ ] **Step 5：全量门禁** — `env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" make check`；`env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" python3 scripts/check_boundaries.py`；`git diff --check`。
- [ ] **Step 6：stage** — `git add domains/opportunities/service_impl.py tests/unit/test_opportunities_handoff.py`
- [ ] **Step 7：commit + push + 核对** —
```bash
git commit -m "feat(opportunities): implement handoff and query service"
git push
git rev-parse HEAD
git ls-remote origin refs/heads/codex/phase1-implementation
# CI watch：见「门禁命令前缀」节的固定块（gh run list 取本次 head SHA 的 run ID，再 gh run watch）
```

---

### 任务 S2-12：Slice 2 演示脚本

**Files**
- Create: `scripts/demo_opportunities.py`、`tests/integration/test_demo_opportunities.py`

- [ ] **Step 1：写失败测试（可执行性冒烟，隔离容器）** — `test_demo_opportunities.py`：spawn testcontainers Postgres → `alembic upgrade head`（`DATABASE_URL` 指向容器）→ subprocess `python scripts/demo_opportunities.py`（`DATABASE_URL` 注入容器 URL）→ 退出码 0、stdout 含「分桶」「门槛解释」「MissingLossReasonError」「died_at_state」「closed_by」；用唯一 `tenant_id`/ID，容器即隔离、无需清理业务数据。
- [ ] **Step 2：运行 RED** — 脚本缺失 → 失败。
- [ ] **Step 3：最小实现** — `demo_opportunities.py` **只从 `os.environ["DATABASE_URL"]` 读连接**（不写死/不打印 DSN 或凭证）；流程：`create_from_need`（带完整 field_provenance + 明确本地 ScoringPolicy）→ 打分（分桶+门槛解释）→ `transition(LOST)`（被拒）→ `mark_lost(reason=None)`（打印 `MissingLossReasonError`）→ `mark_lost(reason=…, actor=…, confirmed_at=…)`（打印 `died_at_state`/`loss_reason`/`closed_by`）。中文走查。
- [ ] **Step 4：GREEN** — `env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" pytest tests/integration/test_demo_opportunities.py -q -W error` 通过。
- [ ] **Step 5：全量门禁** — `env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" make check`；`env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" python3 scripts/check_boundaries.py`；`env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" ruff check scripts/demo_opportunities.py`；`git diff --check`。
- [ ] **Step 6：stage** — `git add scripts/demo_opportunities.py tests/integration/test_demo_opportunities.py`
- [ ] **Step 7：commit + push + 核对** —
```bash
git commit -m "docs(opportunities): add slice 2 demo"
git push
git rev-parse HEAD
git ls-remote origin refs/heads/codex/phase1-implementation
# CI watch：见「门禁命令前缀」节的固定块（gh run list 取本次 head SHA 的 run ID，再 gh run watch）
```

---

## Slice 2 验收汇总

```bash
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" make check
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" pytest tests/integration -q -W error
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" python scripts/demo_opportunities.py
git diff --check
```

- 十二个任务 → 十二个独立 commit + 十二次独立 push。
- 迁移 round-trip + 只增触发器 + outbox 同事务（commit 共存 / rollback 皆无）验证通过。
- 租户越权负测、并发原子条件更新、`(tenant_id, need_id)` UNIQUE 幂等、lost 强制 reason（`reason=None` 可执行）、人工确认落库、事实/推断 Provenance、handoff verbatim provenance 保存全有测试。
- `shared` 新增两个 NewType + `OpportunityWon`（additive）；`total_score`/`factor_scores` float 移除，改 `SortKey` 三元。
- 文档冲突 C1–C5 已给安全决策。

## 风险与说明

- `ScoringPolicy` 的 value-band 边界与 bucket 映射是业务数字，由上层注入；代码不写死。
- outbox 投递轮询（drain/deserialize/dispatch）不在本切片；发布写入 + 同事务语义本切片完成。
- `provenance_records.entity_id` 多态、`score_snapshots.opportunity_id` 悬空为有意设计，依据见 Schema 附录。
- testcontainers 首次拉镜像较慢；无 Docker 环境集成测试 skip 并报告，不降级 sqlite。
- 迁移与 `tables.py` 手写对齐，以集成测试兜底。
