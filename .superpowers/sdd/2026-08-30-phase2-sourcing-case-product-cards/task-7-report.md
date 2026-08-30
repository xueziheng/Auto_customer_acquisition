# Task 7 实施报告：自动触发与内部匹配梯子工作流

## 结果

Task 7 已按 Task BASE `005e7ac` 完成。实现范围限于 Sourcing Case V2 的自动触发、八步流程骨架、内部 1–5 级匹配梯子、内部产品 Option 准备，以及 Ruling P22 明确授权的最小 sourcing 服务/仓储兼容扩展。Task 8 及以后步骤仍是显式无外部副作用等待，没有实现公开寻源、产品卡投影、成本交接或生产 composition。

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

## 最终门禁

- Ruff（全部 touched 文件）：通过。
- Mypy（9 个 touched source 文件）：通过。
- `python3 scripts/check_boundaries.py`：7 项全部通过。
- `git diff --check`：通过。
- `scripts/scan_sensitive.py`：未通过，报告 8 个均位于未修改的既有测试文件中的测试数据库口令/DSN 形态；Task 7 新增或修改文件无命中。

## 残余风险

- 生产 scheduler/runtime composition 属于 Task 13，本任务只提供可注入 handler 和事件处理器，没有启用生产订阅。
- 公开计划、真实搜索、候选核验、产品卡投影、人工审核接线与成本交接分别属于 Task 8–13，当前对应步骤会安全等待。
- 仓库持续输出 `.git/objects/pack/._pack-*.idx` 的 `non-monotonic index` 警告；不影响本次命令退出码，但属于共享 Git 对象库卫生问题，本任务未修改。
- 基线产品候选幂等测试的空价格阶梯夹具与数据库非空约束矛盾需由后续统一修正，不能把这 3 个失败误归因于 Task 7。
