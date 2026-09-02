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

## 审查修复第 1 轮（独立后续提交）

### 修复内容

- 共享 `SourcingAdmissionStarter.admit_one` 现在显式接收可信 `completing_actor`。scheduler 只传
  `system/system` actor；人工入口只传 router 身份解析后、application 已做 tenant/role 门禁的
  boss/sourcing actor。`complete_admission.admitted_by` 与 domain authorization 都保留该真实员工，
  客户端没有可提交或伪造 actor 字符串的字段。
- 人工入口仍先读 tenant-bound admission 与 canonical Case。永久的 Case 缺失、冻结快照/hash 或
  case/need 绑定不一致，不再误报 503 并遗留 waiting：它们进入精确单项 claim 和同一 starter，落为
  `case_state_mismatch` blocked、零 Workflow start、HTTP 409。已知 transient 和未知读取异常均在 claim
  前固定脱敏 503；畸形 dependency 投影也按未知错误保守处理。
- Directive proposal 使用明确的 `DirectiveProposalNotFoundError` 表达 absent，router 只把该类型映射
  为 404；权限/状态冲突分别保持 403/409；持久化 shape、员工投影、submit 后回读及未知依赖异常统一
  去链为固定 503，避免把内部 `ValidationError` 错分为客户端错误或暴露内部消息。
- 专用 `submit_sourcing_admission_proposal` 契约新增可信 `submitted_by`，service 在创建 UoW 前调用
  boss/tenant authorizer。router 与 service 构成双层门禁；直接绕过 router 的非 boss 或 tenant mismatch
  调用均被拒绝且 UoW 进入次数为零。
- 0053 admission evidence 清理由 `yield` fixture 的 `finally` 保证，即使断言失败仍执行。由于
  `sourcing_priority_snapshots` 有 immutable DELETE trigger，且 admission/snapshot 之间存在约束，只能
  在仓库默认串行 pytest（未配置 xdist）的文件边界内清理精确两张表：
  `TRUNCATE sourcing_admissions, sourcing_priority_snapshots CASCADE`；没有无保护的测试尾部清理。

### RED / GREEN 证据

- 第一组纯 RED：`8 failed, 27 passed`，覆盖 boss/sourcing 审计 actor、permanent Case validation、
  command-center internal projection 503、submit service 双重鉴权、typed not-found。修正测试自身
  `NameError` 后复跑仍为相同 8 个预期产品失败。
- 最小实现后：`35 passed`。
- 补充 canonical Case 缺失场景先得到 `1 failed`（既有实现错误返回 503），修复后 application
  `10 passed`。
- 真实 PostgreSQL 场景先暴露测试假数据 hash 非 canonical，再暴露 complete authorization 被硬编码
  为 system；分别修正测试输入和 production scope 后六个 scheduler/manual 参数场景 `6 passed`。

### 审查轮验证

- Task 9 API/OpenAPI 指定套件：`63 passed in 14.60s`。
- Task 8 shared application/driver：`37 passed in 0.35s`。
- Directives Task 3 定点 service/persistence：`22 passed in 3.44s`。
- sourcing service/repository/PostgreSQL/permissions：`258 passed in 8.10s`。
- 完整 sourcing runtime composition：`12 passed in 5.15s`。
- 0053 evidence 反序验证：enabled runtime 后接 migration downgrade 为
  `2 passed in 6.66s`；六个 admission PG 场景后接 downgrade 为 `7 passed in 6.22s`。
- Task 3 历史报告命令共选中 84 个用例：`83 passed, 1 failed, 35 deselected in 47.75s`。唯一失败为
  `tests/integration/test_repositories.py::test_orm_metadata_parity_with_head`：0053 已创建索引
  `ix_sourcing_admissions_queue` 与 `ix_sourcing_priority_snapshots_order`，但旧 parity expected set 未更新。
  Task 9 相对 base `4e729a83205a40cd30cd3210170031d9f2c5044f` 未触及 tables、migration 或该 expected set，源码
  静态证明为既有门禁缺口；按任务裁决记录到 Task 12 全量门禁修复清单，本轮不扩 scope。
- review 受影响文件 `ruff check` 通过；8 个 production 文件 `mypy` 通过；boundaries 七项、敏感信息扫描
  与 `git diff --check` 均通过。
- 提交前 fresh gate：review 相关 unit/directives persistence `121 passed in 8.01s`；六个真实 PG
  scheduler/manual 场景紧接 0053 downgrade `7 passed in 5.64s`；Task 9 API/OpenAPI 四文件
  `77 passed in 9.94s`；随后再次执行 Ruff、mypy、boundaries 与敏感信息扫描均以 0 退出。

## 审查修复第 2 轮（独立后续提交）

### 持久审计方案与权限收口

- 新增内部、nullable 的 `SourcingAdmission.admission_requested_by`，由已通过 tenant/role 鉴权的
  `claim_manual_admission` 从 `actor.actor_id` 写入，HTTP body/DTO/OpenAPI 均不存在可提交该字段的入口。
  `waiting`/`starting` 可以持有它，已知 transient release、租约过期 release 与 scheduler reclaim 都
  原样保留；同 request id 由另一 actor 重放固定冲突。
- 新增 0054 migration 与 ORM/check parity。进入 `admitted` 时 repository 在单条 UPDATE 中以持久人工
  actor 优先、SYSTEM actor 为自动路径 fallback 写入 `admitted_by`，随后清空临时字段；进入 `blocked`
  同样清空。0054 downgrade 在存在任一非空人工审计意图时拒绝，避免静默丢失等待恢复中的证据。
- boss/sourcing 的低层 `ADMISSION_COMPLETE` 权限已撤回，恢复为仅 SYSTEM。公开 service complete
  契约不再接收 `admitted_by`；SYSTEM 鉴权发生在 UoW 前。shared starter 始终使用构造时注入且验证过的
  私有 SYSTEM actor，人工权限只用于精确 claim，因此 tenant actor 不能直调 complete 或伪造 Run 的
  审计执行者。
- 真实 PostgreSQL 测试覆盖 boss 与 sourcing：第一次 engine start 已提交 canonical Run、bind 返回未知，
  随后重新构造 service 与 workflow engine，scheduler 在租约到期后恢复；两种角色都只有一个 Run，
  最终 `admitted_by` 仍为原人工 actor。另覆盖 transient release 后恢复、自动准入记 SYSTEM、跨 tenant、
  same-key different-actor 冲突与 tenant actor complete 的 zero-UoW 拒绝。

### RED / GREEN 证据

- 第一组纯 RED：`35 failed, 58 passed`。失败分别指向缺少 durable 字段/0054、manual claim 未写 actor、
  release/reclaim 后审计丢失、tenant actor 仍可 complete，以及旧 complete/admitted_by 签名。
- 最小 domain/service/repository/application 实现后：`93 passed`；扩大相关 unit：`265 passed`。
- 0054 migration、ORM parity 与真实 repository 首轮因新 migration 测试未在断言失败路径清除 0053
  evidence 得到 `59 passed, 1 failed`；将精确 admissions/snapshots 清理放入 `finally` 后复跑
  `60 passed`。append-only snapshot 使这两张表只能在仓库默认串行测试边界内成对 TRUNCATE；所有新
  runtime/迁移证据均有 fixture 或 `finally`，并对各自唯一 tenant 的 Case/Need/Run 做定向删除。
- 真实 runtime 跨 service/engine 重建：boss/sourcing `2 passed`。

### 最终验证

- Task 8/9 service、API、repository、real PG 组合：`405 passed in 18.69s`。首跑唯一失败是旧测试仍
  期待已撤除的 caller-supplied `system:sourcing`；修正为可信 SYSTEM actor id 后全绿。
- Task 4/5/8 brief focused 合并套件：`417 passed in 80.37s`，包含 0054 migration head、check、ORM
  parity、upgrade/downgrade、并发 repository 与 scheduler lifecycle。
- Task 9 API/OpenAPI 指定四文件：`63 passed in 16.55s`；额外显式断言
  `SourcingAdmissionReadView` 的 OpenAPI properties 不含内部 actor carrier。
- 人为反序验证（先运行两个人工 unknown-bind runtime，再执行全库 head downgrade/upgrade、0054
  lossy-downgrade guard 和 ORM parity）：`5 passed in 8.95s`，证明测试结束后没有持久 evidence 泄漏。
- 受影响文件 `ruff check` 通过；15 个 production source 的 `mypy` 通过；boundaries 七项、敏感信息
  扫描与 `git diff --check` 均通过。
- 提交前 fresh 相关测试（API、shared starter/driver、service/repository、两角色跨进程恢复、0054 与
  head roundtrip）：`404 passed in 20.90s`；随后重新执行全部上述静态门禁，均以 0 退出。

仓库仍打印既有 AppleDouble pack index 警告；本轮未读取、修改或清理该共享文件。没有修改 Task 10
前端或 Tool Gateway。

## 审查修复第 3 轮（独立后续提交）

本轮只修测试隔离，不改任何生产文件。reviewer 指定反序 pair 稳定复现为 `1 passed, 1 failed`：
`test_admission_service_persists_canonical_snapshots_and_token_transitions` 写入 0053 admission/snapshot
证据后没有清理，紧接的 `test_0047_to_0049_roundtrip` 因 evidence guard 拒绝 downgrade 0046。

`test_sourcing_service_persistence.py` 现统一使用文件级 autouse async fixture：每个测试节点前成对清空
`sourcing_admissions` 与 append-only `sourcing_priority_snapshots`，并在 `try/yield/finally` 的
`finally` 中再次成对清理，因此测试断言失败也不会污染后续 migration。该 TRUNCATE 只适用于仓库当前
默认、未配置 xdist 的串行 integration 测试边界；不在并行 worker 间删除其他测试的数据。

验证结果：

- reviewer 最小反序 pair：RED `1 passed, 1 failed`；GREEN `2 passed in 5.39s`。
- 该文件全部 admission 命名节点后紧接 migration roundtrip：`6 passed, 16 deselected in 4.10s`。
- 完整 sourcing persistence + sourcing migrations 两文件组合：`44 passed in 30.23s`。
- Task 8 shared starter/driver 与 Task 9 API/OpenAPI/runtime composition：`117 passed in 15.34s`。
- 仅改动测试文件的 Ruff、全库 boundaries 七项与 `git diff --check` 均通过。
- 提交前将完整 persistence/migrations 与 Task 8/9 重点套件合并 fresh 复跑：
  `161 passed in 41.60s`；随后 Ruff、boundaries 与 diff-check 再次全部以 0 退出。

仓库仍打印既有 AppleDouble pack index 警告；本轮未触碰。Task 10、Tool Gateway 与生产代码均未修改。
