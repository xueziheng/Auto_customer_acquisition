# Task 6 实施报告：Sourcing Case 领域服务

## 结果

- 新增 `SourcingServiceImpl`，实现 `open_case`、`record_ladder_check`、
  `save_public_plan`、`confirm_public_plan`、`submit_candidate`、
  `mark_candidates_ready`、`review` 与 `hand_to_costing`。每个公开写入口均先授权，
  未授权时不会创建 UoW、读取仓储或调用可信边界。
- 开案执行完整度门禁，按 tenant 与稳定 trigger key 幂等；Need 快照与哈希冻结在首次开案
  事实中。业务行与 `SourcingCaseOpened` 使用同一事务，事件失败时整体回滚。
- 匹配梯子强制按 1–5 连续、不可重复或越级；公开计划要求完成五级检查，保存精确
  `plan_hash`，范围变化创建新 ID/新版本，旧版本不能被确认。
- 候选把观察事实、供应商自述、模型推断、规格比较、indicative 价格档与不可变 Evidence
  分离保存；缺 Evidence 快照直接拒绝。八项核验、需求数量档和 MOQ 均由确定性代码判断，
  最多三个合格候选；第四个仍以 rejected 与结构化原因落库。
- 候选就绪只接受本 Case 的合格 Option/Candidate，`option_ids` 必须非空，内部产品路径允许
  `candidate_ids=()`；状态变化与 `SourcingCandidatesReady` 原子提交。
- Review 只能选择本 Case 的合格供给选项，人工确认绑定 Case 版本 N。成本交接要求已确认
  Review 与可信 `OpportunityId`，以一次 Case CAS 完成 Opportunity 绑定和
  `candidates_ready -> handed_to_costing`，终态版本为 N+1；终态快照和
  `SourcingCaseHandedToCosting` 均在同一事务中校验/发布。
- 保留旧 `SourcingService` 公共表面：`create_public_plan`、`submit_review` 作为新方法别名，
  `complete_case` 在 V2 fail closed，`fail_case` 只接受既定公开停止码；读方法继续输出原有
  `CaseView`/handoff snapshot。运行时 Protocol 检查已覆盖。

## TDD 证据

生产实现创建前先落测试并执行：

```text
pytest tests/unit/test_sourcing_service.py -q
=> 11 failed

pytest tests/integration/test_sourcing_service_persistence.py -q
=> 2 failed
```

失败均由 `domains.sourcing.service_impl` / `SourcingServiceImpl` 尚不存在引起，不是测试收集、
fixture 或 PostgreSQL 环境错误，属于有效 RED。

实现过程中又由凭证 URL 用例命中一次真实控制流回归：期望
`MissingEvidenceSnapshotError`，实际因候选构造函数错误返回 `None` 而出现
`AttributeError`。修正候选构造返回路径后，该定向回归与主测试均 GREEN。

覆盖包括完整度阈值、跨终态幂等与 tenant 隔离、authorizer-first、梯子连续性、计划 hash
和版本、候选八项/Evidence/MOQ/数量档/三候选上限、ready 精确 IDs、Review 与 Opportunity
门禁、P10 N→N+1、终态快照，以及 outbox 失败回滚。

## 最终门禁

主回归（真实 PostgreSQL 集成测试未 skip）：

```text
pytest tests/unit/test_sourcing_models.py tests/unit/test_sourcing_service.py \
       tests/integration/test_sourcing_service_persistence.py \
       tests/integration/test_outbox_transaction.py -q
=> 32 passed in 3.72s
```

Task 2/4 相关回归：

```text
pytest tests/unit/test_sourcing_v2_contracts.py \
       tests/unit/test_sourcing_permissions.py \
       tests/unit/test_sourcing_trigger_contracts.py \
       tests/unit/test_outbox_serialization.py \
       tests/integration/test_sourcing_repositories.py \
       tests/integration/test_sourcing_migrations.py \
       tests/integration/test_outbox_transaction.py -q
=> 177 passed in 16.31s
```

静态与结构门禁：

```text
ruff check <Task 6 touched Python files>
=> All checks passed!

mypy <8 touched source files>
=> Success: no issues found in 8 source files

mypy <3 touched test files>
=> Success: no issues found in 3 source files

python scripts/check_boundaries.py
=> 结构自检七项全部通过

git diff --check
=> exit 0
```

## Task 2/4 最小兼容修正

Task 6 首次用真实服务路径装配既有合同后暴露以下明确缺口；均为最小兼容修正：

1. `SourcingCaseRepository.get_by_trigger` 及 SQL 实现：原 `get_active_by_need` 无法在 Case
   进入终态后返回首次开案事实，导致相同 trigger 重试重复建档。新增读取仍强制 tenant
   过滤，并复用现有 `(tenant_id, trigger_key)` 唯一约束；没有新增列或索引。
2. `PriceRejectionReason` 增加 `verification_incomplete`、`moq_not_met`、
   `qualified_limit_reached`，用于持久化第 4 个候选等结构化拒绝原因；该字段原本就是 JSON，
   不需要 schema 迁移。
3. `CANDIDATES_READY` 的状态表移除 `-> failed`，与 Task 6 已裁决的唯一状态图一致；
   `fail_case` 因而只能终止允许失败的早期状态，不能绕过成本交接终态。
4. `SourcingCaseOpened` 加入 Postgres outbox 显式注册白名单，并同步精确白名单测试。事件合同
   已由 Task 2 存在，本次只补实际发布适配，不修改 outbox 核心管线。
5. `SourcingCaseRow.stop_detail` 的 ORM JSONB 绑定设置 `none_as_null=True`。数据库已有
   `stop_detail IS NULL OR jsonb_typeof(stop_detail) = 'object'` 约束；默认绑定会把 Python
   `None` 写成 JSON `null` 并违反现有约束。该改动只修正 ORM null 语义，数据库列类型和
   约束均未变化，因此不新增迁移。
6. `SourcingService` Protocol 对齐 Task 6 新方法名和 keyword-only actor；具体实现仍提供旧
   名称的兼容包装，避免既有调用方因为方法移除而失配。

未修改迁移、Tool Gateway 核心、工作流、API、Connector、成本/报价域或前端；Sourcing 域
没有直接导入其他业务域。

## 关注项

- 无 Task 6 阻断或失败门禁。
- 并发首次开案由现有数据库唯一约束防止重复事实；竞争失败者会 fail closed，调用方可用同一
  trigger key 重试取得 canonical Case。本任务没有在领域层捕获 SQLAlchemy 异常或加入跨层
  重试，以免破坏依赖方向；若产品合同要求并发请求也必须一次返回相同 ID，应在后续事务装配
  层另行裁决原子 get-or-create 合同。
- Git 命令继续打印仓库既有 AppleDouble `._pack-*.idx` non-monotonic index 警告；未触碰
  该 pack sidecar，且 `git diff --check` 返回 0。
