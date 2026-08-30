# Task 6 实施报告：Sourcing Case 领域服务

## 结果

Task 6 服务已实现，并在 Fix Round 1 按 Ruling P14–P18 关闭了全部
1 Critical + 5 Important 审查项：

- `open_case` 精确派生并核对
  `sourcing-case:v2:{tenant_id}:{need_id}`，通过 PostgreSQL
  `ON CONFLICT` 原子返回 `(canonical_case, created)`。只有竞争胜者发布
  `SourcingCaseOpened`；两个 barrier 同步的真实 PostgreSQL session 证明竞争双方
  一次调用就获得同一 Case ID，且只有一条 Case 和一条 Opened outbox。
- 不可变 `LadderCheck` 新增类型化 `LadderOutcome`：
  `no_qualified_supply` / `qualified_supply_found`。早期梯级一旦命中，服务和数据库
  trigger 都禁止后续梯级；只有前五级全部是 `no_qualified_supply`
  才能保存公开寻源计划，不再解析自由文本 `conclusion`。
- 精确 plan hash 确认在同一 Case CAS 中绑定 `active_search_plan_id`，
  并完成 `discovering -> verifying`，Case 版本只增加一次。
- 候选提交依赖 domain-local、tenant-bound 的
  `CandidateEvidenceSnapshotReader`。授权先于 reader，reader 又先于 UoW；服务
  校验 Artifact ID、canonical 安全 URL、hash 和真实 `observed_at`，并使用
  reader 时间而不是 `now`。`observed_facts`、`supplier_claims` 的每个
  `evidence_ref` 以及 `match_inferences.based_on` 都必须属于已验证快照集。
  reader 失败、形状非法、字段错配、凭证 URL 或 naive 时间在打开事务前
  fail closed，外部异常内容不会进公共错误。
- 规格核验不再把不兼容或待客户确认的供给误标为合格：
  `DIFFERENT + substitutable=False` 必然拒绝；
  `needs_customer_confirmation=True` 必须有客户会话
  `ProvenanceSummary`。缺项以结构化核验结果进入既有拒绝原因。
- P14 候选事件时序已拆分。`mark_candidates_verified` 从仓储重建精确
  qualified Supplier Candidate 全集，发布过去式
  `SourcingCandidatesVerified`，但不推进 Case。SYSTEM 可通过
  `register_supplier_candidate_option` 幂等登记真实 candidate/Product/Option
  绑定，包括 Case 已 ready 后的相同投影重放。最终
  `mark_candidates_ready` 再次从仓储重建完整集合，拒绝漏/多 Candidate、
  漏/多 qualified Option、候选未绑定 Option 或空 Option 集合；产品卡和
  Option 完整后才进入 `candidates_ready` 并发布最终
  `SourcingCandidatesReady`。existing-product-only 路径可以用空 Candidate 集合直接
  finalise。
- 原 Task 6 的 authorizer-first、tenant 隔离、候选上限、固定 stop code、
  Review 确认与 P10 单次 CAS 成本交接保持不变。

## TDD 证据

原 Task 6 的首轮 RED：

```text
pytest tests/unit/test_sourcing_service.py -q
=> 11 failed（SourcingServiceImpl 尚不存在）

pytest tests/integration/test_sourcing_service_persistence.py -q
=> 2 failed（SourcingServiceImpl 尚不存在）
```

Fix Round 1 的每项行为都先有有效 RED：

- P14：新事件合同先出现 3 个 import/collection 错误；事件建立后服务测试
  因 `mark_candidates_verified` 不存在失败。ready 后幂等重放测试又先以
  `InvalidStateTransition` 失败 1 个，然后最小放宽为只允许返回已存在的
  canonical 绑定。事件/outbox/服务定向组合 GREEN 为 67 passed。
- P15：unit 先因 `LadderOutcome` 不存在无法收集；迁移测试先有 2 个
  因 outcome 列/约束缺失失败，服务+迁移定向组合实现后 15 passed。
- 规格核验：2 个不合格供给测试先错误地 qualified；正向客户 Provenance
  在字段未定义时先被 `extra_forbidden` 拒绝，实现后 3 passed。
- P16：公共 reader/projection 尚不存在时先发生 ImportError；安全 URL/naive
  `observed_at` 的事务前门禁又先以 2 failed（UoW 被错误打开），改为
  reader 投影校验前置后 2 passed。
- P17：伪造 trigger key 先未被拒绝；两个真实 PostgreSQL session 竞争
  首先复现 unique `IntegrityError`，实现原子 get-or-create 后双方同 ID、
  单 Case、单 outbox 且无异常。
- P18：集成测试先观察到 `active_search_plan_id is None`，实现同一 CAS
  绑定后定向测试 GREEN。

## 最终门禁

Task 6 主回归（真实 PostgreSQL 集成测试未 skip）：

```text
pytest tests/unit/test_sourcing_models.py tests/unit/test_sourcing_service.py \
       tests/integration/test_sourcing_service_persistence.py \
       tests/integration/test_outbox_transaction.py -q
=> 43 passed in 3.95s
```

真实 PostgreSQL 服务并发和迁移/trigger 定向组合：

```text
pytest tests/integration/test_sourcing_service_persistence.py \
       tests/integration/test_sourcing_migrations.py -q
=> 18 passed in 16.22s
```

Task 1–4 联合回归，含迁移 head/round-trip 和 single-head：

```text
pytest tests/integration/test_demand_sourcing_ready_event.py \
       tests/unit/test_sourcing_trigger_contracts.py \
       tests/unit/test_outbox_serialization.py \
       tests/unit/test_sourcing_models.py \
       tests/unit/test_sourcing_v2_contracts.py \
       tests/unit/test_sourcing_permissions.py \
       tests/integration/test_sourcing_repositories.py \
       tests/integration/test_supply_pool_repositories.py \
       tests/integration/test_outbox_transaction.py \
       tests/integration/test_sourcing_migrations.py \
       tests/integration/test_migrations.py \
       tests/unit/test_work_intake_migration_head.py -q
=> 259 passed in 53.57s
```

静态、结构与 diff 门禁：

```text
ruff check <17 个 touched Python 源码/测试文件>
=> All checks passed!

mypy <10 个 touched source files>
=> Success: no issues found in 10 source files

mypy tests/unit/test_sourcing_service.py \
     tests/unit/test_sourcing_trigger_contracts.py \
     tests/unit/test_outbox_serialization.py \
     tests/integration/test_sourcing_service_persistence.py
=> Success: no issues found in 4 source files

python scripts/check_boundaries.py
=> 结构自检七项全部通过

git diff --check
=> exit 0
```

## 合同、兼容与额外文件

Fix Round 1 受权的最小公共合同修正：

1. `SourcingService` 新增 `mark_candidates_verified` 和
   `register_supplier_candidate_option`；原方法名、旧兼容包装与状态门禁保留。
   `SourcingServiceImpl` 新增必填 reader 依赖，生产装配明确 carry-forward 至
   Task 13，不从 request 构造伪 reader。
2. `SourcingCaseRepository.get_or_create` 和
   `SupplyOptionRepository.get_or_create_supplier_candidate` 是原子新接口；为必要
   旧调用保留 `add` / `get_by_trigger` / `get`，但新建 Case 不再接受另一种
   trigger key。
3. `LadderCheck.outcome` 是必填强类型字段。这是 P15 指定的未发布合同收紧；
   仓储、ORM、DB CHECK/trigger 已同步。
4. `SpecComparison.customer_confirmation` 是可选 Provenance 扩展，旧 JSON 无该键时
   依然可回读为 `None`。
5. 最终 Ready 事件的语义从“触发产品卡”收紧为“产品卡和 Option 已完整”。
   新增中间过去式 `SourcingCandidatesVerified` 作为 Task 11 投影输入；旧
   `SourcingCandidatesReady` 类名保留，但不得按旧冲突文本消费。

超出原 Task 6 初始 allowlist、但由 Fix Round 1/P14–P18 明确授权的额外文件：

- `domains/sourcing/models.py`、`schemas.py`、`repository.py`、`events.py`
- `shared/events/catalog.py`、`infra/db/outbox.py`
- `infra/db/repositories/sourcing.py`、`infra/db/tables.py`
- `migrations/versions/0047_sourcing_core.py`
- `tests/unit/test_sourcing_trigger_contracts.py`、`test_outbox_serialization.py`
- `tests/integration/test_sourcing_repositories.py`、`test_sourcing_migrations.py`
- `docs/superpowers/specs/2026-08-30-phase2-sourcing-case-product-cards-design.md`
- `docs/superpowers/plans/2026-08-30-phase2-sourcing-case-product-cards.md`
- `docs/adr/0023-sourcing-candidate-verification-and-readiness-events.md`

本轮没有创建新迁移头；只修正尚未发布的 0047，因此继续保持单 head。
没有修改 Tool Gateway 核心、工作流实现、API、Connector、成本/报价域或前端。

## ADR 与 carry-forward

- 新增中文 ADR 0023，说明 Verified 事件→产品卡投影→Option 登记→最终 Ready
  的时序，并明确替代设计/计划里“产品域消费 Ready”的冲突旧文本。
- Task 11 必须消费 `SourcingCandidatesVerified`，幂等生成产品卡，调用
  `register_supplier_candidate_option`，待全部绑定完整后才调用
  `mark_candidates_ready`。
- Task 13 必须注入读取 Artifact 不可变元数据的
  `CandidateEvidenceSnapshotReader`，不得从搜索摘要或请求字段重建可信投影。

## 关注项

- 0047 在本分支是未发布迁移。若有外部环境曾提前应用该特性分支，不应直接
  假设修订后 0047 已生效，需在合并/部署前单独核对 schema。
- Git 命令仍会打印仓库既有 AppleDouble `._pack-*.idx` non-monotonic index
  警告；本任务未触碰该 sidecar，且 `git diff --check` 返回 0。
