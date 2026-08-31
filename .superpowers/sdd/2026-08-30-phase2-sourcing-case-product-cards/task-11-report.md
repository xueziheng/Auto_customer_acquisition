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

---

## Fix round 1/5（P50 / P51）

### 状态

`DONE_WITH_CONCERNS`

修复基线为 `2415737`。独立 review 的 2 个 Critical、2 个 Important 和 1 个 Minor 均已处理；P49 的双 generation token 传递、review 幂等与 opportunity-only handoff retry 保持不变。未编辑 `progress.md`，未使用子代理。

### 修复内容

1. **P50 supplier prepare bridge**
   - `PrepareCandidatesStep` 增加严格 supplier-generation 分支，只验证并携带精确排序 Candidate IDs、`candidate_case_version` 与 `candidate_set_hash` 到 `await_product_cards`。
   - 该分支不创建 Product、不登记 Option、不调用 ready；原 internal-product 分支保持不变。
   - PostgreSQL 流程验收不再从 `await_product_cards` 起步，而是运行真实 `VerifyCandidatesStep → PrepareCandidatesStep → AwaitProductCardsStep` 后再接收 prepared 事件。

2. **Ready 后的可恢复 Engine handoff**
   - Projector 先完成 canonical Product/Option/Ready，再按 owning Run 查询和唤醒；所有原始 Engine 查询、读取与投递异常统一转换为固定、无异常链的 `TransientError`。
   - Run 仍在 `prepare_candidates` 被视为合法排序竞争并返回固定 transient；重试复用已提交的 Product/Option/Ready，只投递一次 prepared 事件。

3. **精确 owning Run / V2 / generation 绑定**
   - `WorkflowEngine.has_delivered_event` 新增可选 `run_id` 过滤，保持既有 version+context 历史查询兼容。
   - required context 支持有界稳定整数与字符串列表；Projector 和 review 均绑定 tenant、type、V2、Case subject、精确 active `run_id` 及 supplier candidate IDs/version/hash。
   - `_bound_run` 在任何查询/投递前校验完整 P49 generation；历史 terminal Run 的相同事件指纹不再能满足当前 Run。

4. **P51 Product canonical race**
   - concurrent source loser 使用内部控制信号让 staged Product UoW 先回滚，之后在新 tenant-bound UoW 中 fresh-read canonical origin。
   - winner 可见时两个调用返回同一 ProductId；尚不可见或 fresh-read 失败时返回固定、无异常链 `TransientError`，不永久 dead-letter。
   - 真实双事务屏障测试证明两个调用先同时读空后仍只留下一个 Product 和一个 source，无 orphan loser。

5. **契约与测试夹具**
   - `domains/products/AGENTS.md` 已更新为 `SourcingCandidatesVerified` 投影契约，并明确 Ready 不是建卡请求。
   - Product PostgreSQL 测试夹具改用满足当前 `ck_sourcing_candidates_price_tiers_json` 的最小价格事实；首轮报告中的 3 个相关基线失败已消除。

### RED → GREEN 证据

- supplier 全步骤测试先以 `内部候选准备上下文无效` RED，加入 P50 bridge 后 GREEN。
- Ready 后原始 Engine 错误先被错误映射为永久 `ValidationError`，修复后为固定 detached `TransientError`，重试仅一次唤醒。
- prepare 排序竞争先永久失败，修复后 transient 并可精确重试。
- prior-Run 指纹先吞掉 active Run 投递，加入 exact `run_id` 后当前 Run 被正确唤醒；错误 generation 在投递前拒绝。
- Product canonical loser 先抛永久 `ValidationError`，P51 回滚 + fresh-read 后返回 winner ProductId。

### 最终验证

- Task 11 聚焦单元、真实 PostgreSQL projection/concurrency 与事件历史：`68 passed in 12.23s`
- Workflow Engine 全集成回归：`51 passed in 19.88s`
- 全量单元（最后一次生产代码变更后）：`6271 passed in 199.04s`
- Ruff 全库：`All checks passed!`
- 结构边界：全部通过
- 本轮全部变更文件敏感信息扫描：0 命中
- 定向 mypy：本轮生产文件 0 个错误；仍只报告 `shared/schemas/quote_creation.py` 的 13 个既有 Decimal exponent 类型错误
- `git diff --check`：通过

### 剩余关注事项

1. 仓库 Makefile 的 mypy 命令仍先被 `apps/composition_support/quotations.py` 双模块名基线阻断。
2. 仓库级 sensitive scan 仍命中 8 个既有测试文件；本轮变更文件为 0 命中。
3. 共享 Git pack 的 AppleDouble `non-monotonic index` 警告仍存在，但不影响工作树读取与提交。
