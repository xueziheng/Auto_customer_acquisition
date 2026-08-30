# Task 3 报告：Sourcing、产品与供应商数据库迁移

## 结果

完成线性、单 head 的 `0047 -> 0048 -> 0049` 迁移链及对应 SQLAlchemy ORM 行。
迁移仅在 testcontainers 创建的隔离 PostgreSQL 测试库中执行；未读取、打印或修改用户/生产
数据库连接信息。

- `0047` 创建 9 张 Sourcing V2 表：Case、连续 LadderCheck、版本化公开计划、候选、
  tenant-bound 原始 Evidence、统一 SupplyOption、人工 Review、搜索定位回执与独立
  reconciliation audit。
- `0048` 创建产品、变体、供应能力、候选来源、候选价格引用、供应商和供应商价格历史 7 表，
  并在产品表可用后闭合 SupplyOption 的同租户产品 FK。
- `0049` 只扩展 `cost_sheets` 三个可空来源字段，建立同 Case/Option/Candidate 的复合 FK、
  来源配对 CHECK，以及 `uq_cost_sheets_sourcing_case` 租户内部分唯一索引。
- `infra/db/tables.py` 增加全部新表 ORM 行并同步 `CostSheetRow`；金额列均为
  `Numeric(28, 12)`/`Decimal`，没有 `float`。
- `tests/integration/test_migrations.py` 的当前 head 更新为 `0049`。

## TDD 证据

### RED 1：迁移链不存在

先新增 `tests/integration/test_sourcing_migrations.py`，再运行：

```text
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
pytest tests/integration/test_sourcing_migrations.py -q
```

结果：`4 failed in 5.51s`。四项均以预期首因失败：

```text
Can't locate revision identified by '0049'
```

不存在收集、fixture 或连接错误；证明测试确实由缺失迁移触发。

### GREEN 1：三迁移与核心约束

实现 0047–0049 后，同一命令结果：`4 passed in 7.65s`。随后补齐 ORM 列 parity 和
供应商价格历史只增合同。

### RED 2：供应商价格历史可被覆盖

新增真实数据库 UPDATE 拒绝测试后，先观察：

```text
FAILED test_supplier_price_history_is_append_only
Failed: DID NOT RAISE IntegrityError
```

随后在 0048 增加 `BEFORE UPDATE OR DELETE` guard，完整 Task 3 迁移测试转为：

```text
5 passed in 8.53s
```

中间两次失败来自测试 SQL 参数类型歧义和 SQLAlchemy `ColumnCollection` 的断言写法；
均只修正测试 harness，未放宽数据库合同。修正后目标业务断言保持不变。

## 约束与边界覆盖

- 所有 16 张新表均以包含 `tenant_id` 的复合主键落地；所有唯一键、查询索引与关系 FK
  均含租户列。
- `sourcing_candidates(case_id, candidate_id)`、
  `sourcing_supply_options(case_id, option_id, supplier_candidate_id)` 等复合唯一键为下游
  tenant-bound 关系提供数据库级目标。
- Candidate Evidence 使用 `(tenant_id, artifact_id)` 复合 FK 指向现有
  `raw_artifacts`；测试实际尝试跨租户引用并得到 `IntegrityError`。
- Case 对 `opened/discovering/verifying/candidates_ready` 使用租户内部分唯一索引；
  同一 Need/workflow 不能出现两个活跃 Case。
- 计划 `confirmed_by/confirmed_at/authorized_plan_hash` 成对，且授权哈希必须等于计划哈希。
- Review 的 primary snapshot 必须为 JSON object，alternate IDs 必须为最多两个字符串的
  JSON array；触发器进一步拒绝重复、包含主选或引用其他 Case/tenant 的备选。
- observed facts、supplier claims、match inferences、verified specs、indicative price tiers、
  Evidence、locator receipts 与 reconciliation audit 使用独立列/表；不会把搜索摘要提升为证据。
- 候选 JSON 价格档的 amount 只允许十进制字符串；测试实际插入 JSON number `1.25` 并被拒绝。
  产品候选与供应商价格记录使用 `NUMERIC(28,12)`。
- LadderCheck 数据库触发器拒绝跳级以及 UPDATE/DELETE；reconciliation 与 supplier price
  history 为只增审计事实。
- Cost 来源必须全部为空，或至少同时绑定 Case 与 Option；Candidate 路径还通过四列复合 FK
  绑定同租户、同 Case、同 Option 的 candidate。

## 最终验证

```text
pytest tests/integration/test_sourcing_migrations.py \
       tests/integration/test_migrations.py \
       tests/unit/test_work_intake_migration_head.py -q
=> 66 passed in 47.05s

ruff check infra/db/tables.py migrations/versions/0047_sourcing_core.py \
  migrations/versions/0048_supply_pools.py migrations/versions/0049_sourcing_cost_origin.py \
  tests/integration/test_sourcing_migrations.py tests/integration/test_migrations.py
=> All checks passed!

PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
python3 scripts/check_boundaries.py
=> 结构自检通过

git diff --check
=> exit 0

python scripts/run_alembic.py heads
=> 0049 (head)
```

`git` 在共享卷读取 AppleDouble pack sidecar 时继续输出仓库既有的
`non-monotonic index .../._pack-*.idx` 警告；依照任务约束未触碰或修复这些文件，命令本身
仍成功。

## 自审

- 未添加 Repository、UoW、Service、API、Workflow、Connector 或 Gateway 修改。
- 未更改或重编号历史迁移；0047 的父节点是当前 0046，最终只有 0049 一个 head。
- upgrade/downgrade/upgrade 在隔离 PostgreSQL 中实际执行，并验证新表完整消失与恢复。
- 0048 在产品表创建后才给 0047 的 SupplyOption 补产品复合 FK；这是闭合跨租户关系所需的
  同一迁移步骤，不修改 Gateway 或业务服务。
- 无生产数据 backfill；新增列均可空，旧成本表可无来源继续存在。
- 未发现需要放宽硬边界或新增 ADR 的设计冲突。

## Fix Round 1（Task 3 审查）

### 修复结果

- 按 Ruling P3 将 `SourcingStopCode`、0047 CHECK 与 ORM CHECK 统一为领域生命周期码和
  Ruling P1 精确公开搜索码的并集；删除 `stop_code` 中四个迁移草稿缩写。既有 Gateway
  quota reservation 的 `stop_reason` 及搜索执行的 `provider_status` 属于不同合同，依照
  “不修改 Gateway core / Task 10”边界未改动。
- 新增冻结的 `SourcingStopDetail` 与固定 `SourcingStopStage`。详情只允许阶段、查询序号、
  HTTP 状态、观察数量和配置上限；领域拒绝自由字符串，数据库拒绝未知键、错误类型、负数、
  小数计数和越界 HTTP 状态，避免保存 Provider 原文、请求载荷或异常文本。
- 0048 与 ORM 的产品 CHECK 显式处理 SQL NULL：候选池只接受非空 `source_only`；内部成本
  四字段全有或全无；售价 amount/currency 成对；交期 min/max 成对且范围有效。
- 新增真实 PostgreSQL 行为覆盖：Ladder 跳级及 UPDATE/DELETE、reconciliation
  UPDATE/DELETE、Review 备选重复/包含主选/跨 Case/跨 tenant、supplier price DELETE，
  以及停在 0048 插入旧 `cost_sheets` 行再升 0049 的兼容性与数据保留。

### TDD RED / GREEN 证据

第一组先写停止码、安全详情和 SQL NULL 绕过测试，未改实现时：

```text
pytest <2 个 stop contract tests> <2 个 migration constraint tests> -q
=> 4 failed in 7.33s
```

失败首因分别为：领域枚举缺少精确公开码、`SourcingStopDetail` 不存在、数据库拒绝
`approval_required`、candidate 的 NULL status 穿过 CHECK。最小实现后同组：

```text
=> 4 passed in 10.71s
```

随后扩大安全详情负例时，真实数据库暴露 JSON number 小数仍可穿过整数语义：

```text
pytest test_stop_codes_and_safe_detail_roundtrip_without_draft_shorthands \
       test_stop_detail_rejects_invalid_structured_values -q
=> 1 failed, 7 passed in 8.76s
```

在迁移和 ORM CHECK 增加 `trunc` 整数约束后：

```text
=> 8 passed in 7.35s
```

行为覆盖在生产触发器未变更的前提下首次运行即通过，证明审查缺口是测试覆盖而非触发器实现：

```text
pytest <ladder/reconciliation/review/supplier-delete/legacy-upgrade 5 tests> -q
=> 5 passed in 17.92s
```

### Fix Round 1 最终验证

```text
pytest tests/integration/test_sourcing_migrations.py \
       tests/integration/test_migrations.py \
       tests/unit/test_work_intake_migration_head.py -q
=> 72 passed in 106.60s

pytest tests/unit/test_sourcing_models.py tests/unit/test_sourcing_v2_contracts.py -q
=> 66 passed in 0.28s

ruff check domains/sourcing/models.py infra/db/tables.py \
  migrations/versions/0047_sourcing_core.py migrations/versions/0048_supply_pools.py \
  tests/unit/test_sourcing_v2_contracts.py tests/integration/test_sourcing_migrations.py
=> All checks passed!

PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
python3 scripts/check_boundaries.py
=> 结构自检通过

git diff --check
=> exit 0

python scripts/run_alembic.py heads
=> 0049 (head)
```

全部迁移仍只在 testcontainers 隔离数据库执行。Fix Round 1 未新增迁移 revision、未修改 Task 10、
Gateway core、Repository/UoW/Service/API/Workflow/Connector。Git 仍报告既有 AppleDouble
pack sidecar `non-monotonic index` 警告；按约束未触碰或修复。

## Fix Round 2（Task 3 定向复审）

### 修复结果

- 按 Ruling P4 收紧 `stop_detail.stage`：键必须存在，JSON 类型必须为 string，且值属于
  固定阶段集合；`stage: null`、number、boolean 均由 PostgreSQL CHECK 拒绝。
- 四个可选详情字段同时支持“键省略”和普通 dataclass/asdict 生成的 JSON null；只有非 null
  值才进入 number、integer 与 range 校验。未知键、错误类型、负数、小数计数及越界 HTTP
  状态仍被拒绝。0047 与 ORM CHECK 保持一致。
- 0048 与 ORM 允许 candidate pool 持久化完整生命周期
  `source_only | partial | not_approved`，同时拒绝 NULL 和未知值；formal/capability pool 仍只
  接受 NULL。创建时只能是 `source_only` 的边界留给 Task 5 服务，未下沉为永久表约束。
- Ladder 测试在 rung 1 后实际插入并读取 rung 2，再验证预先跳级以及 UPDATE/DELETE 拒绝。
  触发器实现无需改动。

### TDD RED / GREEN

生产修改前运行四个定向 PostgreSQL 测试：

```text
pytest <stage-type/null-optionals/candidate-lifecycle/ladder tests> -q
=> 3 failed, 1 passed in 13.48s
```

三个预期失败分别是：`stage:null` 未被拒绝、asdict 的 JSON null 可选字段被拒绝、
`candidate_status=partial` 被拒绝；Ladder 1→2 已由既有触发器正确支持。最小修改 0047、
0048 与 ORM CHECK 后：

```text
=> 4 passed in 15.20s
```

补充域 dataclass/asdict 默认值断言，并将 integration 测试改为不跨层导入域私有实现后：

```text
pytest <domain-asdict + 4 PostgreSQL tests> -q
=> 5 passed in 13.96s
```

中间结构门禁曾准确报告 integration test 直接导入 `domains.sourcing.models`；该问题只修正
测试分层，数据库断言未放宽。随后结构门禁恢复通过。

### Fix Round 2 最终验证

```text
pytest tests/integration/test_sourcing_migrations.py \
       tests/integration/test_migrations.py \
       tests/unit/test_work_intake_migration_head.py -q
=> 74 passed in 99.42s

pytest tests/unit/test_sourcing_models.py tests/unit/test_sourcing_v2_contracts.py -q
=> 67 passed in 0.24s

ruff check infra/db/tables.py migrations/versions/0047_sourcing_core.py \
  migrations/versions/0048_supply_pools.py tests/integration/test_sourcing_migrations.py \
  tests/unit/test_sourcing_v2_contracts.py
=> All checks passed!

PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
python3 scripts/check_boundaries.py
=> 结构自检通过

git diff --check
=> exit 0

python scripts/run_alembic.py heads
=> 0049 (head)
```

本轮没有新增 revision，没有修改域业务实现、Task 5/Task 10、Gateway core 或其它持久化
范围。迁移仍只在隔离测试 PostgreSQL 执行；AppleDouble pack 警告继续按约束原样保留。
