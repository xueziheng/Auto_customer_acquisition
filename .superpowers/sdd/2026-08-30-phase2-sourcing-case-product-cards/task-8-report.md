# Task 8 实施报告：公开寻源计划门禁与保守恢复

## 结果

Task 8 已按 BASE `ddd3f82975ebbd1e4ffe97d94486d7bea8c189d3`、ADR 0023、Ruling P26/P27 完成。实现范围只包含公开寻源计划的创建、精确确认、免费状态运行门禁、Workflow Engine 安全事件投递，以及不确定搜索的 `count_as_consumed` 保守恢复；没有实现 Task 9+ 的真实搜索、联系人、邮件、报价或成本。

## TDD 证据

### RED

1. 先增加 `tests/unit/workflows/test_sourcing_plan.py`，首跑在收集阶段因缺少 `SourcingUncertainReconciliationCommand` 失败。
2. 先扩展 `tests/unit/test_sourcing_service.py`，四个核对证据测试因 `SourcingServiceImpl` 尚无 `provider_usage_evidence_reader` 失败。
3. 先扩展 `tests/integration/test_search_quota.py`，两个真实 PostgreSQL 测试因 `acknowledge_uncertain_as_consumed` 尚不存在失败。
4. 实现期间真实 Workflow/PostgreSQL 测试暴露了全局唯一 Tavily 账户对相邻随机租户测试的污染；增加测试前后清理后，组合运行恢复全绿。这是夹具隔离问题，不是通过放宽租户约束解决。

### GREEN

```text
pytest tests/unit/workflows/test_sourcing_plan.py \
       tests/unit/test_sourcing_service.py \
       tests/integration/test_sourcing_plan_confirmation.py \
       tests/integration/test_search_quota.py -q
=> 93 passed

pytest tests/unit/workflows/test_sourcing_case.py \
       tests/unit/test_scheduler_sourcing_events.py \
       tests/integration/test_sourcing_service_persistence.py \
       tests/integration/test_sourcing_repositories.py \
       tests/integration/test_sourcing_migrations.py -q
=> 61 passed

pytest tests/unit -q
=> 5683 passed
```

真实 PostgreSQL 综合场景覆盖：确认后替换计划、旧授权失效、两次并发 run、计划只发生一次 durable `authorized -> running`、真实 Workflow Engine 从 `await_public_plan` 接受精确事件并进入 `public_search`、审计事实提交后额度确认失败、重建 service/quota/engine 后恢复、旧 request key 保持 consumed、累计预留不释放、精确重放只保留一条核对事实，以及跨租户读取不可见。

## 实现摘要

- `SourcingCaseApplication.create_plan` 与 `confirm_plan` 只调用领域服务，不读取或预留额度，不触发 Workflow 或任何外部动作。
- 计划确认显式绑定 `case_id + plan_id + expected_plan_hash`；错误 Case 在任何状态变更前拒绝。
- P27 允许 `authorized` 且尚未运行时在 `verifying` 保存下一版本；旧计划与未确认新计划均不能运行。保存、确认与运行统一先锁 Case，关闭替换/确认/run 并发竞态；running 后禁止替换。
- run 先由 boss-only 公共服务重建精确活跃计划，再读取 tenant-bound 快照。只有当前租户、Tavily、FREE、paygo 精确 false、带时区 checked_at 且剩余额度足够时通过；未知、付费和耗尽分别返回 `quota_status_unknown`、`paid_usage_enabled`、`quota_exhausted`。
- 计划在领域事务中原子进入 running 后，仅投递 `{plan_id, plan_hash}`。提交后投递失败返回固定可重试错误；同参数重放不重复状态变更，并安全恢复并发或崩溃窗口。
- `AwaitPublicPlanStep` 入口只等待，只接受键集合精确的 `SourcingPlanConfirmed`，持久化安全计划 ID/哈希后进入 `public_search`。
- 核对命令严格只接受 `count_as_consumed`、有界理由、旧 Run/request、稳定核对 ID 和 Artifact 引用；额外 tenant、actor、凭证或 Provider 原始载荷被 Pydantic `extra=forbid` 拒绝。
- 领域服务通过 tenant-bound trusted reader 核验 Tavily 用量 Artifact 的租户、ID、Provider、哈希与观察时间；自由异常被丢弃，不进入日志或异常链。
- 核对顺序固定为：只增 `confirmed_consumed` 审计事实 → quota `uncertain -> consumed` → 安全重试事件 `{reconciliation_id, execution_id}`。旧 key 不删除、不退款、不复用，精确 consumed 重放为 no-op。
- `sourcing_search_reconciliations` 不再保存任意 `provider_receipt` JSON；改为同租户复合外键绑定不可变 `raw_artifacts`，并保留执行唯一约束和不可变触发器。

## 最终门禁

- Ruff（全部 touched Python 文件）：通过。
- Mypy（13 个 touched source 文件）：通过。
- `python3 scripts/check_boundaries.py`：7 项全部通过。
- `git diff --check`：通过。
- `scripts/scan_sensitive.py`（全部 touched 文件）：通过。
- 真实 PostgreSQL migration、repository、quota、service persistence、Workflow Engine 定向回归：通过。

## 残余风险

- Task 8 不执行真实公开搜索，也不创建新 request key；`public_search` 在 Task 9/10 接入前继续安全等待。
- 生产环境的 Provider 用量 Artifact reader 与 runtime composition 仍由后续装配任务提供；未注入时核对会 fail closed。
- `0047_sourcing_core.py` 仍属于未发布迁移，本任务按 P26 直接修订。已在本地应用旧 0047 形状的开发数据库必须受控重建或 downgrade/upgrade，不能只替换代码。
- 单部署 Tavily 账户的数据库全局唯一约束是设计要求；测试必须隔离该账户，不能通过为不同 tenant 复制额度来消除冲突。
- Git 持续输出共享对象库 `._pack-*.idx` 的 `non-monotonic index` 警告；所有相关命令退出码正常，本任务未修改共享 Git 对象库。
