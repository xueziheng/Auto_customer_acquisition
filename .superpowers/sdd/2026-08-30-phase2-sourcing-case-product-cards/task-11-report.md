# Task 11 实施报告：候选产品卡投影与人工审核唤醒

## 状态

`DONE_WITH_CONCERNS`

Task 11 的功能、聚焦测试、真实 PostgreSQL 集成测试、全量单元测试、Ruff、结构边界和变更文件敏感信息扫描均已完成。仓库级 mypy 与敏感信息扫描仍受既有基线问题阻断，见“关注事项”。

## 基线与约束

- 起始提交：`6ddead7`
- 仅消费封存事实 `SourcingCandidatesVerified`；`SourcingCandidatesReady` 仅作为产品卡与 Option 完整就绪后的最终事实。
- 所有 Supplier Option 登记与最终 ready 迁移均原样使用事件携带的 `case_version` 和 `candidate_set_hash`，不重算、不混代。
- 未编辑 `progress.md`，未使用子代理，未执行联系人、邮件、报价或外部页面动作。

## 实现结果

1. 新增 `SourcingCandidateProductProjector`：
   - 严格验证 tenant、Case、候选集合及 sealed generation；单 Case 最多三张候选卡。
   - 通过 Sourcing 公共投影读取与 `CandidateProductCreate` 同形的严格 DTO，不跨域导入领域模型。
   - 用 Product canonical source key 幂等创建/复用 Product，再用 Sourcing canonical candidate key 幂等登记 Option。
   - 支持 Product、Option、ready 任一阶段后的安全重试；ready Outbox 与内部工作流事件均不重复。
   - Product/Sourcing/Engine 的自由异常被转换为固定错误，且在脱离原异常上下文后抛出。

2. 新增 tenant-bound `get_candidate_product_inputs`：
   - 仅允许 V2 Case 的 `VERIFYING` / 精确重放 `CANDIDATES_READY` 状态。
   - 从仓储重建完整、排序且已验证的 sealed 候选全集。
   - 产品名、品类、匹配摘要、MOQ、参考价格和 Artifact 引用均来自已有事实；价格保持 `Decimal`，并拒绝不能精确写入 `NUMERIC(28,12)` 的值。

3. 完成内部工作流事件 `SourcingProductCardsPrepared`：
   - payload 仅含 `case_id`、稳定 Candidate/Product/Option ID、原始 generation version/hash。
   - 查询和投递严格绑定 tenant、`sourcing_case`、V2、Case subject、owning Run 和 `case_id` context。
   - `AwaitProductCardsStep` 只接受完整精确 payload，随后进入 `await_review`。

4. 完成 `application.review` 唤醒语义：
   - trusted caller 必须显式提供非空、无控制字符、最长 200 字符的 `request_id`。
   - 先保存或精确重放唯一人工 review；同 Case 的变更 review 冲突。
   - owning V2 Run 位于 `await_review` 时投递 `SourcingReviewSubmitted`。
   - Run 位于 `handoff_costing` 且 durable stop reason 精确为 `opportunity_required` 时，以新 request ID 投递 `SourcingHandoffRetryRequested`，不改写 review。
   - 同 request ID 精确重放成功；terminal、foreign 或错误等待边界均拒绝；显式接受实际 `RUNNING` / `WAITING_EVENT` 状态。

5. 更新事件订阅语义：Products 订阅 `SourcingCandidatesVerified`，不再把最终 Ready 事实当作建卡请求。

## TDD 证据

依次观察并修复了以下 RED：投影器模块不存在、严格投影 DTO 不存在、Sourcing service projection 不存在、投影器构造缺失、产品卡事件未推进等待步骤、review 精确重放被拒、application review 不存在、Products 仍订阅旧事件、`NUMERIC(28,12)` 越界值未被拒绝。对应测试随后逐项 GREEN。

## 验证结果

- 聚焦单元 + 真实 PostgreSQL 集成 + workflow：`35 passed in 8.86s`
- 全量单元（最后一次生产代码变更后重跑）：`6266 passed in 109.24s`
- 结构边界：通过（分层、金额 float、置信度、事件、租户过滤、AGENTS、域结构全部通过）
- Ruff：`All checks passed!`
- 变更文件敏感信息扫描：通过，无命中
- `git diff --check`：通过
- 相关宽回归曾运行 218 项，其中 215 项通过；3 个既有 `test_product_candidate_idempotency.py` 用例在测试夹具写入 `indicative_price_tiers='[]'` 时被当前数据库 `ck_sourcing_candidates_price_tiers_json` 拒绝，失败发生在本任务生产代码之前。

## 关注事项

1. Makefile 的仓库级 mypy 命令首先因 `apps/composition_support/quotations.py` 被识别为两个模块名而失败。使用 `--explicit-package-bases` 后检查 487 个源文件，报告 74 个既有错误；对本任务 6 个生产文件定向检查时，只剩 `shared/schemas/quote_creation.py` 的 13 个既有 Decimal exponent 类型错误，本任务文件无新增错误。
2. 仓库级 `scan_sensitive.py` 命中 8 个既有测试文件中的 password/DSN 形态；本任务全部变更文件的显式扫描为 0 命中。
3. Git 每次读取索引都会报告共享仓库 `.git/objects/pack/._pack-*.idx` 的 AppleDouble `non-monotonic index` 警告，但状态、diff、提交对象读取仍可继续；本任务未改动共享 Git 对象。
4. 上述 3 个 Product candidate 集成测试的夹具与当前 DB CHECK 不一致，未在本任务中扩大范围修复。
