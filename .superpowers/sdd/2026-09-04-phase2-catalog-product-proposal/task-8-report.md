# Task 8 实施报告：目录提案评估与培育生命周期

日期：2026-09-04

基线：`0960e04743025f3277e50188538fd2e45f5467a0`

提交：本报告随 `feat: create catalog cultivation proposals` 一并提交

## 交付范围

- 实现 `evaluate_cluster`、评估/提案/培育 case 的列表与详情读取、`bind_proposal_approval` 和 `apply_cultivation_decision`。
- 评估只接受 SYSTEM actor 与可信 `run_` RunId；在租户策略命名空间锁内读取活动策略，提案 owner 只取自活动策略提交人。
- 正常输入完整复制为历史事实快照；损坏输入形成 blocked evaluation 时只持久化 `tenant_id`、`cluster_id`、`facts_hash` 三个字段的 `CatalogBlockedFactsInput`，不保存不可信业务值。
- 通过评估、唯一 `awaiting_approval_submission` 提案与 `CatalogProductProposalCreated` 同事务写入。批准且仍 current 的精确 subject 才会将提案转为 `cultivation_queued`，并同事务创建唯一 queued case 与 `CatalogCultivationQueued`。
- 历史读取只依赖已持久化的策略和 facts 快照；apply 路径在锁内重新读取提案与当前活动策略，并用调用方提供的当前 mapped facts 重新验证完整契约和 hash。
- 未创建正式 Product，未进入 sourcing，未调用外部供应商、报价或 Approval apply API。

## 范围裁定

Task 8 原始 brief 的五个文件不足以原子证明“持久 Approval subject 与提案完全一致后才绑定”。进度 ledger 已裁定允许本任务额外修改：

- `domains/products/repository.py`
- `infra/db/repositories/catalog_products.py`

新增的 narrow `bind_approval` port 在同一事务锁定 proposal 与 `ApprovalPackageRow`，核对 tenant、approval ID、`catalog-cultivation-v1` namespace、`catalog_product_cultivation` type、proposal/policy/facts subject、唯一 change-set reference、owner、可信 run，以及三方完全一致的 request hash 后才更新。错误绑定固定冲突且不更新；精确重放幂等。该实现不导入 Approvals domain，并保持与 tables.py 已有 catalog-cultivation 分支兼容。

代价是两个 Task 6 repository 文件重新进入 Task 8 review；收益是把验证与更新保持为真正原子操作，避免 service 层“先读后写”的竞态窗口。

## TDD：RED → GREEN

### RED

先创建 Task 8 单元和集成测试，再运行：

```text
.venv/bin/python -m pytest tests/unit/test_catalog_proposal_service.py tests/integration/test_catalog_proposal_service.py -q
```

结果为测试收集阶段错误：Task 8 的 error types 尚不存在。随后为唯一 approval reference helper 增加独立测试，收集阶段再次以缺少 `catalog_cultivation_change_set_ref` 的 `ImportError` 失败。这些失败均发生在对应生产实现之前。

### GREEN

- Task 8 unit：`18 passed`
- Task 8 real PostgreSQL integration：`7 passed`
- Task 8 focused + repository：`49 passed in 5.32s`
- Task 5–8 Products 相关回归、migration、repository、真实 PostgreSQL：`199 passed in 10.10s`
- 无 skip。

## 一致性、并发与回滚证据

- `(tenant, cluster, policy_version, facts_hash)` 在重复调用和两个独立 PostgreSQL session 并发时收敛为同一 evaluation、同一 proposal、同一 created event；相同唯一键但 run 或完整 snapshot 不一致固定冲突。
- failed 与 blocked evaluation 都持久化规则明细且不创建 proposal；数据库断言 blocked facts JSON 恰好等于最小 envelope。
- proposal created outbox 写入失败会回滚 evaluation 和 proposal；cultivation queued outbox 写入失败会回滚 case，并让 proposal 保持 `pending_review`。
- Approval 原子绑定测试覆盖 wrong type、owner、run、request hash 和 subject；每次冲突后 proposal 保持 `awaiting_approval_submission`，正确绑定和完全相同的重放均返回同一 `pending_review`。
- apply 校验 approval ID、request hash、type/namespace、reference、run、owner/decider、活动策略和当前 facts；非法决定走 fixed invalid/conflict，真正过时走 `stale`，rejected/expired 保持准确终态。
- exact approved current subject 只创建一个 queued case 和一个 queued event；响应丢失后的精确重放返回原结果，不重复写入。case 已提交后才允许后续 Task 11 workflow 标记中央 Approval applied，本任务没有执行该外调。

## 受控外部动作矩阵

| 动作 | Task 8 结果 |
|---|---:|
| 正式 Product 写入 | 0 |
| Sourcing 行写入 | 0 |
| 供应商联系 | 0 |
| 报价或价格承诺 | 0 |
| 外部网络 / Tool Gateway 调用 | 0 |
| 中央 Approval `mark_applied` 调用 | 0 |

## 最终门禁

```text
.venv/bin/python -m pytest <Task5-8 related unit/integration/repository/migration tests> -q -rs
199 passed in 10.10s

.venv/bin/ruff check <7 Task8 production/test files>
All checks passed!

.venv/bin/ruff format --check <7 Task8 production/test files>
7 files already formatted

.venv/bin/mypy domains/products/service.py domains/products/catalog_service_impl.py domains/products/errors.py domains/products/repository.py infra/db/repositories/catalog_products.py
Success: no issues found in 5 source files

.venv/bin/python scripts/check_boundaries.py
结构自检通过

git diff --check
exit 0
```

## 风险与后续边界

- Task 9 才正式注册 Approval 构造合约；本任务只实现并验证与现有 `tables.py` catalog-cultivation branch 一致的窄绑定协议。
- Task 11 workflow 才能在 case 事务成功后调用 Approval applied；提前外调会破坏恢复顺序，因此本任务明确不做。
- 当前 mapped facts 来自可信 SYSTEM workflow 边界；如果未来暴露为外部 API，必须在应用层继续保持 SYSTEM-only 和 typed validation，不得把客户端 facts 当作可信事实。
- Git 读取对象时持续输出既有 AppleDouble pack 索引告警（`._pack-*.idx: non-monotonic index`），但所有 Git 命令均成功退出；应由独立仓库维护任务清理。
- 工作树原有未跟踪目录 `output/playwright/t10-2f6ab75a1aee427f82a6e7581b3a31da/` 未读取、未修改、未暂存。

## Fix round 1：current facts 与可信 workflow run 加固

### Review 问题与修复

1. **current facts 精确绑定历史 evaluation**
   - apply 在打开 UoW 前重新执行完整 `CatalogClusterFactsInput` model validation；损坏 model 固定拒绝，不再把输入错误写成 proposal stale。
   - tenant 不匹配在 run / decision 校验前固定抛租户隔离错误且不进入 UoW。proposal cluster 只能在锁定 proposal 后获知，因此 cluster 不匹配在该锁后固定拒绝，事务不产生任何业务写或状态变化。
   - `facts_hash` 保持独立精确比较；此外逐字段比较 cluster category、稳定 Need/account/country 集、全部 counts、unified unit、safe total quantity 与完整 evidence summaries。
   - `display_codes` 和 `facts_observed_at` 明确排除在决策比较之外；只改变这两个展示字段仍可使用同一受审 facts subject。
   - proposal 对应 evaluation 必须是结构自洽的 passed 完整快照；缺失、blocked、failed 或损坏 evaluation 统一为固定 cultivation conflict，而不是继续排队或泄漏底层异常。
2. **跨租户 pre-I/O**
   - evaluate 先安全提取最小 locator，再进行 run 校验和 UoW 访问；同租户业务字段损坏仍可形成只含 locator/hash 的 blocked evaluation。
   - apply 先完整验证 current facts 及 tenant，再读取 proposal；真实 PostgreSQL SQL spy 证明跨租户 evaluate/apply 没有 INSERT、UPDATE 或 DELETE，原 proposal 仍为 `pending_review`。
3. **可信 Run 持久化 guard**
   - `CatalogEvaluationRepository` 新增 tenant-bound guard；PostgreSQL adapter 查询 `WorkflowRunRow` 并精确要求 `catalog_cluster_evaluation`、version 1、subject=cluster、status=`running`、step=`evaluate`。
   - tenant、type、version、subject、status、step 任一不符均返回固定 `目录评估 Run 不可信`，且不创建 evaluation、proposal 或 outbox event；不向上泄漏原始行内容。

### Strict TDD RED 证据

- 同一自报 `facts_hash` 下改变 `cluster_category` 的 reviewer probe，旧实现返回 `cultivation_queued`；其余 11 组 hash-covered 字段参数化用例同样错误排队。
- cross-tenant evaluation 旧实现进入 UoW 后才发现租户不符；cross-tenant apply 旧实现把 proposal 更新为 `stale`。加入无 I/O / 无写入断言后稳定 RED。
- tenant/type/version/subject/status/step 不匹配的真实 workflow run 在旧实现中仍尝试持久化 evaluation；其中 foreign-tenant run 由 FK 偶然阻断，其余 run 会实际写入，证明只检查 `run_` 前缀不足。
- 损坏历史 evaluation 的旧路径返回 transient unavailable，而不是要求的固定 corruption/conflict。
- RED 阶段发现一项测试 fixture marker 超过数据库 `varchar(32)`；缩短合法测试标识后重新确认失败来自业务缺口，不把 fixture 错误计作功能 RED。

### GREEN 与最终回归证据

- Task 8 unit：`37 passed in 0.17s`。
- Task 8 real PostgreSQL integration：`15 passed in 4.39s`，无 skip。
- Task 8 focused + repository：`76 passed in 5.29s`，无 skip。
- Task 5–8 Products 相关单元、migration、repository、真实 PostgreSQL：`226 passed in 9.60s`，无 skip。
- 12 组 hash-covered 字段变化全部落 `stale` 且 case/event 为 0；两个展示字段变化仍只创建一个 queued case。
- 6 类 persisted run 反例全部固定拒绝；既有正例、并发收敛、blocked 最小 envelope、Approval 原子绑定与 outbox rollback 全部保持 GREEN。
