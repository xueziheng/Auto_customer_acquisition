# Task 9 实施报告：寻源准入策略与人工准入 HTTP

## 结论

已交付五条实际 API 路径：

- `POST /commands/sourcing-admission-proposals`
- `POST /commands/sourcing-admission-proposals/{proposal_id}/confirm`
- `GET /sourcing-admissions`
- `GET /sourcing-admissions/{admission_id}`
- `POST /sourcing-admissions/{admission_id}/admit`

提案创建/确认仅限 boss；队列读取沿用 boss/product/sourcing/finance 契约；人工准入仅限
boss/sourcing。tenant 与角色门禁均先于 service IO。确认只激活 Directive 并返回实际版本，零
Workflow start。没有修改 Tool Gateway、前端或 Task 10 生成类型。

## 关键实现与依赖裁决

Task 8 的单项启动原本是 `apps/scheduler_worker` 私有方法，API 若直接复用会形成 apps 进程间
反向/横向依赖。按依赖边界，将公开的单项 start/bind 编排下沉到
`workflows/sourcing_case/application.py`：

- `SourcingAdmissionStarter.admit_one(...)` 保留 Task 8 的 frozen Case snapshot、稳定业务键、
  transient release、permanent block、unknown lease recovery 与 start-before-bind 恢复语义；
- scheduler driver 继续负责 Directive 门禁、过期租约释放和固定批量 claim，只把每条已 claim
  admission 委托给该 starter；
- API application 先读取 tenant-bound admission 和 canonical Case，再精确 claim 单条并调用同一
  starter；blocked invalid 固定 409，已 admitted 重放返回 canonical safe view；
- Directive policy reader 也下沉到 workflow application，scheduler 原模块仅兼容重导出，API 不导入
  另一个 app；
- 最小增加 domain/repository `claim_manual_admission` / `claim_one`，避免人工准入误 claim 全局排序
  队首。

真实 API composition 复用 production `DemandServiceImpl` priority reader、同一 sourcing service、
Directive policy reader 和同一 Postgres Workflow engine。准入对象构造只校验/保存依赖，不 claim、
不 start、不解析 secret、不访问网络。

## HTTP 契约

- proposal body 使用 strict/extra-forbid Pydantic：`mode` 只能为 `cluster_ranked`，bool/int 不接受
  coercion，`batch_limit` 为精确整数 `1..50`；预计行为明确显示启用/关闭与精确批量。
- confirm 要求恰好一个未经框架合并或修剪的原始 `Idempotency-Key`；生效 Directive section 必须
  与提案完全一致，确认流程不接触 Workflow engine。
- list/detail 仅返回 `SourcingAdmissionReadView`，并在 application 层投影 `enabled`、
  `policy_not_configured`、`automatic_admission_disabled`、`policy_status_unknown`；没有把 scheduler
  stop concern 写回 sourcing domain。
- manual admit 要求恰好一个严格原始 key；返回 safe canonical admitted view，不返回 claim token、
  lease、Workflow context、完整 Need 或底层错误。
- 403/404/409/503 走固定 API 错误；未知 Directive、storage、starter 异常统一去链并脱敏为 503。
  OpenAPI 明示新路径、schema、必填 header 与错误响应。前端类型按计划留给 Task 10。

## TDD 证据

按 RED → GREEN 执行：

1. 既有 API 基线：`49 passed`。
2. router 纯 RED：`11 failed, 13 passed`，失败原因仅为五条路由缺失；随后假 application 路由
   `18 passed`。
3. domain 精确人工 claim RED：`2 failed`（service 方法缺失），GREEN：`2 passed`。
4. PostgreSQL 定点 claim RED：`1 failed`（repository `claim_one` 缺失），GREEN：`1 passed`。
5. shared application RED：`7 failed`（application 尚无构造/实现），GREEN：`7 passed`。
6. production API composition RED：`1 failed`（准入 application 未装配），GREEN：`1 passed`。
7. 未分类 Directive 异常 RED：`1 failed`（500），收敛为固定 503 后 command router
   `7 passed`。

## 验证结果

- 指定 API/OpenAPI 套件：`60 passed`；增加异常门禁后对应 command/router 单套继续通过。
- application + 原 Task 8 driver：`34 passed`，证明批处理、崩溃恢复和单项 seam 语义不变。
- 扩大回归（API、domain、repository、scheduler、runtime composition）：`381 passed`。
- 人为反序隔离验证：
  - `test_admission_driver_blocks_real_pg_case_snapshot_hash_drift` 两个参数实例后接 API downgrade：
    `3 passed`；
  - `test_scheduler_runtime_factory_enabled_root_binds_typed_model_and_all_sourcing_events` 后接 API
    downgrade：`2 passed`。
- `ruff check`（本任务涉及文件）：通过。
- `mypy`（12 个受影响生产文件）：通过。
- `python3 scripts/check_boundaries.py`：全部通过。
- `git diff --check`：通过。

Task 9 新增的唯一真实 PostgreSQL claim 用例受文件级 autouse 前后
`TRUNCATE sourcing_admissions, sourcing_priority_snapshots CASCADE` 保护；其他新增测试只使用内存 fake
或只读检查组合对象。另修复了三个既有 Task 8 composition node 的 0053 evidence 清理，避免人为文件
顺序下 immutable admission evidence 阻止 API migration downgrade 测试。

仓库仍会打印既有 AppleDouble pack index 警告；本任务未读取、修改或清理该共享文件。
