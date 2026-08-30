# Task 7 实施报告：自动触发与内部匹配梯子工作流

## 结果

Task 7 已按 Task BASE `005e7ac` 完成。实现范围限于 Sourcing Case V2 的自动触发、八步流程骨架、内部 1–5 级匹配梯子、内部产品 Option 准备，以及 Ruling P22 明确授权的最小 sourcing 服务/仓储兼容扩展。Task 8 及以后步骤仍是显式无外部副作用等待，没有实现公开寻源、产品卡投影、成本交接或生产 composition。

Fix Round 1 按 Ruling P23 收紧了同一范围：临时依赖错误保持可重试且脱敏；Ready 精确冻结唯一合格内部梯级的 ProductId 全集；内部产品只有在所有规范化 Need 规格（包括数量/MOQ 与单位）都有证据绑定的确定性 `exact` comparison 时才能短路。本轮仍未实现 Task 8+。

## TDD 证据

### RED

1. 先新增 workflow 与 scheduler 测试，再运行：

   ```bash
   PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
   pytest tests/unit/workflows/test_sourcing_case.py \
          tests/unit/test_scheduler_sourcing_events.py -q
   ```

   结果：测试收集失败，`workflows/sourcing_case/flow.py` 与 `apps/scheduler_worker/sourcing_events.py` 尚不存在。

2. 先增加 P22 服务测试，再运行：

   ```bash
   PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
   pytest tests/unit/test_sourcing_service.py -q \
          -k 'ladder_exact_replay or existing_product_option'
   ```

   结果：2 个预期失败；同级精确重放仍被当作下一梯级拒绝，且服务尚无 `register_existing_product_option`。

### GREEN

- Task 7 主测试：`12 passed`。
- Task 6/P22 相关 unit、真实 PostgreSQL persistence、migration、repository 回归：`66 passed`。
- 更宽的 sourcing/product/supplier 相邻回归：`224 passed, 3 failed`。3 个失败均来自基线 `005e7ac` 已存在的夹具/约束矛盾：`test_product_candidate_idempotency.py` 写入空 `indicative_price_tiers`，而同一基线的 `ck_sourcing_candidates_price_tiers_json` 已要求数组非空；与本任务改动无关，未越界修复。

### Fix Round 1 RED / GREEN

1. 临时错误重试语义：新增 scheduler start 失败与 workflow 部分提交测试后首跑 `3 failed`，证明 `TransientError` 被错误包装为不可重试 `ValidationError`；最小修复后 `3 passed`。
2. Ready 冻结集合：先增加子集 Ready 与就绪后补建 Option 测试，首跑因未拒绝子集而失败；实现精确 ProductId 集合比对后通过。
3. P23 产品事实比较：先增加 exact/different/missing/unbound/duplicate 与公共 `ProductActor` 类型测试，首跑 `6 failed`；再扩展 MOQ 过高与 Product MOQ 事实漂移，首跑 `4 failed`；实现后全部转绿。
4. 领域入口继续 fail closed：先增加“合格产品 LadderCheck 不得空 comparison/非 exact”测试，首跑 `1 failed`；实现后通过。
5. Fix Round 1 主定向 unit：`69 passed`；全量 unit：`5648 passed`。
6. 真实 PostgreSQL：Outbox + `PostgresWorkflowEngine` 恢复、并发 canonical Option、Ready 精确冻结/重放/就绪后拒绝补建全部通过；服务 persistence 文件 `8 passed`，migration + supply repository `27 passed`。相关 PG 宽回归为 `51 passed, 3 failed`，三个失败仍是上述已记录的基线空价格档夹具矛盾。

## 实现摘要

- `NeedValidated` 与 `NeedBecameSourcingReady` 共用精确业务键 `sourcing-case:v2:{tenant}:{need}`；低 completeness、跨租户、未知类型与 Need mismatch 均按固定边界处理。
- 两种就绪事件都重读可信 Need 快照，先取得 canonical Case，再幂等启动 `sourcing_case` V2 Run；context 只保留安全 allowlist。
- 精确声明 V2 八步定义、转换、等待事件、`run_on_entry`，并将公开搜索的 `max_retries` 固定为 0。
- 梯子只接受产品服务的 `qualified_matches`，按正式无修改、正式可修改、候选池的最早梯级短路；没有合格产品才调用一次供应商能力搜索，且梯级 4–5 始终只记录 `no_qualified_supply` lead。
- 梯子检查使用稳定排序、确定性检查 ID、固定结论码和可信 Need 哈希；下层自由异常统一清洗为固定错误。
- 内部产品路径登记 canonical existing-product Option，最终以空 Supplier Candidate 集合发布 Ready，并绕过公开候选产品卡等待进入人工审核等待。
- Task 8+ handler 均为显式 wait/guard，不伪造搜索、核验、建卡、审核或成本交接成功。

## Ruling P22 文件

- `domains/sourcing/service.py`
- `domains/sourcing/service_impl.py`
- `domains/sourcing/repository.py`
- `infra/db/repositories/sourcing.py`
- `infra/db/tables.py`
- `migrations/versions/0047_sourcing_core.py`
- `tests/unit/test_sourcing_service.py`
- `tests/integration/test_sourcing_service_persistence.py`
- `tests/integration/test_sourcing_migrations.py`

P22 行为：精确相同的 LadderCheck 重放为 no-op、同级 payload 漂移冲突；SYSTEM-only existing Product Option 使用真实 products 外键和 tenant+Case+Product partial unique index，Case 行锁串行化并发登记，缺失产品时整笔事务回滚。

## Ruling P23 最小公共契约扩展

- `domains/products/service.py` 公开导出 `ProductActor`、规格要求/事实/comparison 和 `QualifiedProductMatch`；workflow 不再使用 `Any`。
- products service 对每个规范化 Need spec 返回稳定逐项结果；缺失、重复、无 Artifact、different、unknown、MOQ 不满足或 MOQ/单位与 Product 字段漂移都只返回 finding。
- `products.match_specs` 是本轮修改 `0048_supply_pools.py`、ORM 与 products repository 的唯一原因：持久化“规格值 + ArtifactId”的产品事实，使确定性 comparison 在重启后仍可重建。它没有引入新数据源、商业 API 或 Task 8+ 行为。
- qualified 内部 LadderCheck 保存非空、全 exact 比较与证据集；Ready 从唯一 qualified 梯级的 `qualified_product_ids` 重建不可变集合。

## 最终门禁

- Ruff（全部 touched 文件）：通过。
- Mypy（10 个 touched source 文件）：通过。
- `python3 scripts/check_boundaries.py`：7 项全部通过。
- `git diff --check`：通过。
- `scripts/scan_sensitive.py`：未通过，报告 8 个均位于未修改的既有测试文件中的测试数据库口令/DSN 形态；Task 7 新增或修改文件无命中。

## 残余风险

- 生产 scheduler/runtime composition 属于 Task 13，本任务只提供可注入 handler 和事件处理器，没有启用生产订阅。
- 公开计划、真实搜索、候选核验、产品卡投影、人工审核接线与成本交接分别属于 Task 8–13，当前对应步骤会安全等待。
- 仓库持续输出 `.git/objects/pack/._pack-*.idx` 的 `non-monotonic index` 警告；不影响本次命令退出码，但属于共享 Git 对象库卫生问题，本任务未修改。
- 基线产品候选幂等测试的空价格阶梯夹具与数据库非空约束矛盾需由后续统一修正，不能把这 3 个失败误归因于 Task 7。
- P23 对历史 Product 采取 fail-closed：未回填 `match_specs` 的旧数据会成为 `product_spec_unknown` finding，不会自动短路。这是预期的安全兼容，但上线前需单独安排有证据的产品规格回填，不能用无来源文本补齐。
