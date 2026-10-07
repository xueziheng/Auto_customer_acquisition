# Task 1 独立审查

## Spec Compliance

- ❌ Issues found：1 个 Important。新事实锁与现有归属转移的外键锁形成逆序等待，未满足 brief 对并发边界“无反向等待”的要求；定位 `infra/db/repositories/opportunities.py:584`。其余审查结论绑定 `e6e08aac170670eff93d4f73188cb16672103568..acd89d23ca289f92bdbe5a280cbdf70fb05384f0`，仅为 Task 1 gate。
- ⚠️ 验证范围：活跃同版本配置在 `tests/integration/test_owner_handoff_reminders.py:393` 通过真实 PG compatibility helper 验证；无 active 的真实 API/scheduler 启动在 `:447` 验证；三种不兼容 active 的真实启动拒绝在 `:503` 验证。未看到“已有同版本 active Run 时，两个真实 startup 均成功”的独立正向用例，不能把 helper 通过表述成该实际入口用例已运行。

## Strengths

- `workflows/human_handoff/flow.py:81` 使用明确可选周期和独立的两个步骤，v1 兼容保留；`:221` 每轮委托当前事实 scope，初始与周期采用稳定轮次键，不调用经理/老板升级。`apps/scheduler_worker/runtime.py:808` 在新模式取消旧初始事件投影，避免双写及管理者受众。
- `infra/db/repositories/in_app_notifications.py:35` 在真实 append 最后边界执行 guard，缺 guard 拒绝；`:48` 的独立事务在 guard 释放前 commit。`domains/opportunities/service_impl.py:1523` 以当前接管指派、机会 owner、账户 owner 和员工 active 决定允许，不在通知层复制领域判断，也不修改归属。
- `tests/integration/test_owner_handoff_reminders.py:166`、`:183`、`:222`、`:447` 检查真实站内行，覆盖排队后接受、接受 Outbox 延迟、7199/7200/14400 秒、重建/重放及历史保留，不以 fake notifier 数量代替投递结果。
- `infra/db/repositories/opportunities.py:905` 的 tenant-bound 版本检查进入 API lifespan（`apps/api/runtime.py:145`）及 scheduler 真实资源入口（`apps/scheduler_worker/runtime.py:1121`）；输入、v1 保留、周期编码和停机切换成本由 ADR0069 明确记录，没有改通用 engine。

## Issues

### Critical

- 0。

### Important

- **I1 — `infra/db/repositories/opportunities.py:584`：Employee 的 `FOR UPDATE` 与归属转移历史外键形成真实死锁。** Guard 先锁当前员工，再等待账户归属行（`:606`）。现有 `OwnershipRepositoryImpl.replace` 先 UPDATE 账户归属（`infra/db/repositories/employees.py:364`），再追加历史（`:382`）；历史的 `from_owner`、`transferred_by` 外键（`infra/db/tables.py:2801`、`:2811`）在 flush/commit 时请求旧员工行的 KEY SHARE。若转移已取得归属行、guard 已取得旧员工行，两者循环等待。审查者在独立 owned PostgreSQL 上以事件屏障和有界 timeout 成功复现 SQLSTATE `40P01`，不是仅凭理论推测。转移被选为牺牲事务会失败；提醒被选为牺牲事务时，异常到达既有 engine 的通用非 TransientError 分支（`infra/db/workflow_engine.py:802`），会使提醒 Run 失败。现有串行 account_owner 测试 `tests/integration/test_owner_handoff_reminders.py:538` 不覆盖该竞争。请使事实锁与隐式 FK 锁兼容或统一锁序；可评估仅将 Employee 事实锁改成 `FOR NO KEY UPDATE`，它仍与员工停用 UPDATE 互斥，却兼容外键 KEY SHARE。修复必须用当前真实 replace/历史提交与 guard 的多连接竞争验证，并保留接受、员工停用后的抑制行为。这不是版本编码等控制者设计取舍问题；ADR0069 中“没有逆向”的结论漏掉了隐式 FK 锁。

### Minor

- 0；上述真实 startup 正向覆盖差异作为验证范围提示，不另重复计缺陷。

## 具体检查与局限

- 按段审阅提供的 28 文件 safe diff；首次合并输出被截断后只补读缺失内容。未重新推导 git diff、未改源码/index/HEAD、未提交或推送。只新增本报告与控制者同意的 scratch probe。
- **具名风险：锁序及实际提交。** 核对既有 `accept_handoff`（`domains/opportunities/service_impl.py:999`）与 `accept_if_requested`（`infra/db/repositories/opportunities.py:743`）、机会 assign（`:371`）、员工 update（`infra/db/repositories/employees.py:174`）、transfer/replace（`domains/employees/service_impl.py:398`、`infra/db/repositories/employees.py:339`）、相关表 FK 及 UoW commit（`infra/db/unit_of_work.py:47`）。因此识别 I1；没有扩展为全库锁审计。
- **具名风险：独立站内连接反向等锁。** `infra/db/tables.py:2664` 站内表只以 FK 指向 job，未引用已锁员工/机会；job claim 在投递前提交（`infra/db/repositories/notification_jobs.py:38`）。`notification_gateway/channels/in_app.py:55` 直接委托 append，无 guard 重入。
- **具名风险：其他交付入口绕过。** 检索所有 apps 内 store/notifier 装配。新 API handler 选择持久 job（`apps/api/composition/runtime.py:1327`）；旧 API structured log 路由未冒充站内。真实 notification worker 的 guard 在 `apps/notification_worker/runtime.py:324` 装配，新原因生产路由仅选站内（`:154`）。
- **具名风险：持久轮次与版本选择。** 核对未改 engine 最新注册版本选择（`infra/db/workflow_engine.py:265`）、start 幂等/锚点（`:275`）、持久 reminder_index 推进（`:766`）和继承原计划锚点（`:858`）。新的稳定 dedup key 经 `apps/composition_support/handoff_notifications.py:53` 指纹化，job 与站内分别有持久唯一约束（`infra/db/tables.py:2628`、`:2664`）。
- **具名风险：真实入口丢配置/启动检查失效。** 对照 pilot 映射、API parser/composition/lifespan 与 scheduler parser/resources 的 diff，以及新增真实 roots 测试断言。`tests/unit/test_pilot_profile.py:310` 覆盖 profile 重读与 pilot API/scheduler 解析；`:335`、`:346` 覆盖非法输入。正向 active startup 的证据边界如上。
- **复用的实现者证据，未由审查者重跑：** 最终源码的两组单测 274+107 passed，PG 20 passed（新增14+legacy6；2个未改 schema 测试 deselected，无 skip），ruff 21文件、mypy 17模块、boundaries、diff-check 通过。没有把旧阶段或本机历史全仓计数纳入本任务结论。报告中的早期安全材料处理偏差已有明示；审查未读这些历史原始材料。
- **审查者实际运行的唯一新增 probe：** `.venv/bin/python3.12 .superpowers/sdd/2026-09-08-owner-handoff-reminders/run_owned_tests.py .superpowers/sdd/2026-09-08-owner-handoff-reminders/reviewer_lock_probe.py`，结果 `1 passed in 1.59s`、exit 0。该探针的通过断言是竞争结果包含 `40P01`，即成功复现 I1，绝不是功能验证通过。探针使用迁移后的独立 owned PG、真实 repository.replace 和 scope，原始异常只在进程内提取 SQLSTATE；未输出凭证、DSN、参数或原始异常。owned runner 正常退出并执行精确容器清理路径。
- 未运行全库/浏览器/真实 Provider/真实 profile 或真实业务验收。主动退回、Agent 接续、金额分档关闭仍不属于本次交付。未修改或评估通用 engine 的既有失败/补发政策。

## Assessment

**Task quality：Needs fixes。**

核心接受后投递抑制和持久配置链路具有可审查的真实行为证据，但已复现的并发死锁使当前 Task 1 不能通过。计数：Critical 0 / Important 1 / Minor 0；spec 与 quality 均待 I1 修复后复审。
