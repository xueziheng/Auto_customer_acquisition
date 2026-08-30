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
