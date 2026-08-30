# Task 7 实施报告：自动触发与内部匹配梯子工作流

## 结果

Task 7 已按 Task BASE `005e7ac` 完成。实现范围限于 Sourcing Case V2 的自动触发、八步流程骨架、内部 1–5 级匹配梯子、内部产品 Option 准备，以及 Ruling P22 明确授权的最小 sourcing 服务/仓储兼容扩展。Task 8 及以后步骤仍是显式无外部副作用等待，没有实现公开寻源、产品卡投影、成本交接或生产 composition。

Fix Round 1 按 Ruling P23 收紧了同一范围：临时依赖错误保持可重试且脱敏；Ready 精确冻结唯一合格内部梯级的 ProductId 全集；内部产品只有在所有规范化 Need 规格（包括数量/MOQ 与单位）都有证据绑定的确定性 `exact` comparison 时才能短路。本轮仍未实现 Task 8+。

Fix Round 2 按 Ruling P24 将产品逐项规格事实移出 JSONB，改为同租户 Product 与 Raw Artifact 双重复合外键约束的规范化子表；workflow 重新绑定可信 Need 的逐项 `required` 值，并保存逐产品、逐规格 Evidence 映射。不存在或跨租户 Artifact、Need required 漂移和重复规格都不能进入合格梯级。

Fix Round 3 按 Ruling P25 封闭最后一条证据替换路径：workflow 同时将每项 comparison 的 `required` 绑定可信 Need、将 `offered` 与 `evidence_ref` 绑定持久化 Product 事实；领域服务独立要求 qualified Product、完整规格集合、comparison、逐项 Evidence 映射和聚合 Evidence 集形成精确闭包。任何单项缺失、多余、替换或跨产品不完整都不能持久化合格梯级。

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

### Fix Round 2 RED / GREEN

1. P24 证据租户边界：先增加 migration contract、repository 不存在/跨租户 Artifact 测试，首跑分别因 `product_match_specs` 不存在及两个无效引用仍被 JSONB 接受而失败；规范化子表与复合外键实现后转绿。
2. P24 Need 绑定：先增加 `material=stainless` 的 Need 遇到伪造 `required/offered=carbon exact`、重复 comparison、重复规范化 Need 名称和逐项 Evidence 映射测试，首跑 4 个失败；可信 Need map 与返回结果逐项精确比对后转绿。
3. Repository 重启恢复：真实 PostgreSQL 保存同租户 Evidence 绑定事实，创建新的 products service/UoW 后仍可产生完整 exact comparisons；同时验证规范化 spec name、value 与 ArtifactId 往返。
4. Fix Round 2 主定向 unit：`72 passed`；全量 unit：`5651 passed`。
5. 真实 PostgreSQL migration + supply repository + sourcing persistence：`38 passed`，其中包含真实 Outbox + `PostgresWorkflowEngine` 恢复、canonical Option 并发以及 Ready freeze 并发/重放。相关 PG 宽回归为 `57 passed, 3 failed`；三个失败仍是既有 `test_product_candidate_idempotency.py` 空价格档夹具与非空数据库约束冲突。

### Fix Round 3 RED / GREEN

1. Product 事实不可替换：先增加持久事实为 `stainless/art_material`、返回 comparison 伪造为 `carbon` 或 `art_forged` 的 workflow 测试，首跑 `2 failed`；逐项绑定持久事实的 value 与 ArtifactId 后转绿。
2. LadderCheck Evidence 闭包：先增加缺失/多余 mapping、Product、spec，mapping ref 与 comparison 不一致、ref 不属于聚合 `evidence_refs`、`no_qualified` 伪造映射等测试，首跑 `8 failed`；实现领域独立校验后转绿。
3. 跨产品完整性：另以两个 Product 复现某一规格从第二个 Product 的 comparison 与 mapping 同时删除的旁路，首跑 `1 failed`；要求所有 qualified Product 的规格集合完全一致后转绿。
4. 持久化与回滚：`SpecComparison` 的 ProductId/ArtifactId 经 repository 往返；真实 PostgreSQL 非法映射在写入前拒绝，Case 保持 `opened` 且 LadderCheck 数量为零；canonical Option 并发测试同时验证重读后的 comparison 与逐产品 Evidence 映射。
5. Task 6/7 定向 unit：`84 passed`；全量 unit：`5663 passed`。
6. 真实 PostgreSQL migration、repository、Outbox/Engine recovery、Ready freeze 与并发套件：`58 passed`。加入既有候选幂等文件后的宽回归为 `58 passed, 3 failed`；三个失败仍是上述空 `indicative_price_tiers` 基线夹具与数据库非空约束冲突。

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
- Product 域模型仍以 `match_specs` 聚合公开规格事实，但 P24 已移除 `products.match_specs` JSONB 持久列；0048、ORM 与 repository 改用 `product_match_specs` 规范化子表持久化“规格值 + ArtifactId”，使确定性 comparison 在重启后仍可重建，且不存在 JSON-only qualifying 旁路。
- qualified 内部 LadderCheck 保存非空、全 exact 比较与证据集；Ready 从唯一 qualified 梯级的 `qualified_product_ids` 重建不可变集合。

## Ruling P24 最小可信证据扩展

- `product_match_specs` 以 `(tenant_id, product_id, normalized_spec_name)` 为主键，分别通过 `(tenant_id, product_id)` 和 `(tenant_id, evidence_ref)` 复合外键绑定同租户 Product 与 Raw Artifact；写入、更新、读取始终带 tenant 谓词并与 Product 聚合共享事务。
- Repository 在持久化前规范化规格名并拒绝空值、重复名或未绑定 Evidence；数据库最终拒绝不存在和跨租户 Artifact。失败提交会回滚先行 flush 的 Product，不留下孤儿产品。
- Workflow 在调用产品服务前从可信 Need 快照重建唯一的规范化 `spec_name -> required` map；合格结果必须逐项、唯一、排序稳定、Evidence 非空，且每项 normalized `required` 精确等于可信 Need。
- LadderCheck 的 `input_snapshot.product_spec_evidence` 保存每个合格 Product 的逐规格 ArtifactId 映射；聚合 `evidence_refs` 与 exact `spec_comparisons` 保持原有查询兼容。

## Ruling P25 Evidence 闭包

- Workflow 对可信 Need 与 `Product.match_specs` 分别建立规范化唯一 map；只有每项 comparison 的 spec name、required、offered、`EXACT` 和 evidence_ref 同时精确匹配两边可信事实，且每个 Product 覆盖全部 required specs，才会生成 qualified LadderCheck。
- Qualified LadderCheck 的每个 comparison 显式携带 ProductId 与 ArtifactId；repository JSON 往返保留这两个键，支持 PostgreSQL 重启后继续核验和重放。
- `SourcingService.record_ladder_check` 不信任 workflow：冻结 Product 集、mapping Product 集、每个 Product 的共同规格集合、comparison 集与逐项 ref 必须完全相等，所有逐项 ref 还必须属于聚合 `evidence_refs`；`no_qualified_supply` 不允许携带产品 Evidence 映射。

## 最终门禁

- Ruff（全部 touched 文件）：通过。
- Mypy（本轮 4 个 touched source 文件）：通过。
- `python3 scripts/check_boundaries.py`：7 项全部通过。
- `git diff --check`：通过。
- `scripts/scan_sensitive.py`（本轮 touched files）：通过；全库既有 8 个测试数据库口令/DSN 形态仍未由本任务修改。

## 残余风险

- 生产 scheduler/runtime composition 属于 Task 13，本任务只提供可注入 handler 和事件处理器，没有启用生产订阅。
- 公开计划、真实搜索、候选核验、产品卡投影、人工审核接线与成本交接分别属于 Task 8–13，当前对应步骤会安全等待。
- 仓库持续输出 `.git/objects/pack/._pack-*.idx` 的 `non-monotonic index` 警告；不影响本次命令退出码，但属于共享 Git 对象库卫生问题，本任务未修改。
- 基线产品候选幂等测试的空价格阶梯夹具与数据库非空约束矛盾需由后续统一修正，不能把这 3 个失败误归因于 Task 7。
- P23/P24 对历史 Product 采取 fail-closed：没有 `product_match_specs` 证据事实的旧数据会成为 `product_spec_unknown` finding，不会自动短路。这是预期的安全兼容，但上线前需单独安排有证据的产品规格回填，不能用无来源文本补齐。
- 0048 尚未发布，因此本轮直接修订该 migration；已经在本地应用旧 0048 的开发数据库必须重建或执行受控 downgrade/upgrade，不能仅修改代码后继续沿用旧表结构。
- `SpecComparison.product_id` 与 `evidence_ref` 为兼容供应商候选及历史 JSON 保持可空；领域服务仅在 qualified Product 梯级强制两者非空并校验精确闭包。历史记录仍能读取，但不能借空字段进入新的合格 Product 路径。
