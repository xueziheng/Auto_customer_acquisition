# Task 1 实现报告

状态：DONE（仅负责人提醒交付）。源码基线 `e6e08aac170670eff93d4f73188cb16672103568`；
分支 `codex/web-internal-pilot`；工作目录 `/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/web-core-completion`。
初轮实现提交：`acd89d23ca289f92bdbe5a280cbdf70fb05384f0`；审查 I1 修复见末尾 Fix round 1。没有 push、merge 或真实 profile 初始化。

## 交付行为

显式配置 7200 秒后，初始一次站内提醒，7199 秒无重复，7200/14400 秒各一轮；持久轮次、原
start 幂等键与绝对 UTC 锚点复用原引擎。重建 workflow/重复请求不重复同轮。只提醒当前在职
负责员工，不修改负责人，不产生经理/老板升级审计。新模式完整 scheduler 注册不再同时注册
原 `notification.handoff_requested` 投影，避免初始双写和向管理者抄送。

接受提交后，初始事件尚未扫描、初始通知已排队、周期通知已排队、接受 Outbox 尚未消费、后续
扫描这五类情形均不会增加该事项的真实站内行。历史通知保留。新原因码 owner_pending /
owner_reminder 用“待接管提醒”文案；生产路由也只为这两个原因选择站内，旧原因多渠道规则不变。

## 公共接口及配置

- `build_human_handoff_definition(t1, t2, *, owner_reminder_interval: timedelta | None = None)`。
- `build_human_handoff_step_handlers(..., owner_reminder_interval: timedelta | None = None)`，保留
  原签名的所有必填参数；新增 human_handoff.notify_pending_owner / human_handoff.remind_owner。
- `register_human_handoff(engine, registry, *, t1, t2, owner_reminder_interval=None)`；
  `register_complete_scheduler(..., owner_reminder_interval=None)`。
- Pilot `HandoffInput.owner_reminder_interval_seconds: int | None`；API `_HandoffPayload` 同名严格
  字段；`Phase1RuntimeSettings.owner_reminder_interval: timedelta | None`；scheduler
  `SchedulerWorkerConfig.handoff_owner_reminder_interval_seconds: int | None`。默认 None 保留 legacy。
  env 为 `TRADEOS_HANDOFF_OWNER_REMINDER_INTERVAL_SECONDS`。pilot → API/pilot.runtime_settings
  及 scheduler parser 的实际映射均验证，7200 未填成任何其他业务政策默认。
- `OpportunityService.handoff_notification_scope(tenant_id, handoff_id, opportunity_id, recipient, *,
  actor) -> AbstractAsyncContextManager[bool]`。独立 `HandoffNotificationServiceImpl` 构造只需
  UoW factory、authorizer、audit，notification worker 无需补造打分/SLA 政策。
- 内部 `HandoffRepository.lock_notification_facts(...) -> HandoffNotificationFacts | None`，
  包含 state / assigned_to / opportunity owner / account owner / employee active。
- `PostgresInAppNotificationStore(factory, *, handoff_guard: Callable[[InAppNotification],
  AbstractAsyncContextManager[bool]] | None = None)`；新原因缺 guard 时明确拒绝，旧种类兼容。
- `assert_handoff_reminder_compatibility(factory, tenant_id, owner_reminder_interval_seconds) -> None`
  是启动只读核验，已进入 API 真 lifespan、scheduler 真实资源入口及原 configured 入口。
- `NotificationJobHandoffNotifier` 移到 `apps/composition_support/handoff_notifications.py`，原
  scheduler 路径保持同类导出；API 新模式使用真实 job，旧 API 日志通知模式不变。

28 个精确提交文件列于同目录 `task-1-files.txt`。额外 apps/api/pilot.py、apps/api/runtime.py、
非进程 notifier 和相邻 AGENTS 扩展均由控制者明确批准。控制者修改的 plan/spec 一同提交；
没有提交 scratch 报告或测试执行脚本。

## 持久兼容策略

ADR0069 永久保留 human_handoff v1 旧结构；版本 2..2147483647 专用于当前两步结构的
整数周期秒数 + 1（7200→7201）。所有入口拒绝非整数、非正数与超出 2147483646 秒；
scheduler mapping 还拒绝 bool 冒充 int。未来结构必须新 workflow type，不能挪用此版本空间。

API/scheduler 启动遇到任何不兼容的活跃旧 v1、旧周期或新模式 Run 固定拒绝
`handoff_reminder_policy_conflict`；没有业务推进、通知或改写 Run。操作者需停止所有应用，
恢复原配置，完成原活跃运行后再切换；不是自动迁移。API/scheduler 必须同策略，不能运行时
热改配置。原配置的 v1 真实 PostgreSQL 行为测试保留并通过。没有修改通用 Workflow engine。

## 并发顺序与领域边界

1. 精确 SYSTEM opportunity scope 在 IO 前授权，仓储逐次 tenant 过滤。
2. 锁序为 Employee → Opportunity → OwnershipLock → Handoff。域比较接管指派、机会 owner、
   账户归属和 active 事实；旧受众/接受状态返回 False，缺事实抛固定错误，依赖失败不冒充成功。
3. scan 在事实 scope 内 enqueue。notification worker 在事实 scope 内执行站内另一连接的
   INSERT 和 commit；站内连接不会反向锁这些业务行，也不重入 guard。scope 退出才释放事实锁。
4. 接受仍由原真实 service.accept_handoff → accept_if_requested UPDATE → 同事务 Outbox 提交。
   UPDATE 与 guard 的 Handoff 行锁互斥：通知先占锁时，真实站内 commit 先完成，接受才能提交；
   接受先提交时，后续持锁读取看到 accepted，不产生站内行。
5. 实测采用事件屏障与两条独立 PostgreSQL 连接：投递持锁时接受在 0.15 秒有界等待内不能提交；
   释放后站内先提交、接受再提交；第二条提醒 append 返回 False，总站内数仍为一。另测试接受
   已提交后直接处理已排队记录仍为零。不以 fake notifier 或 job 数代替投递结论。
6. 初轮关于“没有逆向锁”的结论不成立：独立审查 I1 复现 transfer 历史 FK 与 Employee
   FOR UPDATE 的逆向等待及死锁。Fix round 1 改为兼容 FK KEY SHARE 的 Employee
   FOR NO KEY UPDATE；不改原归属逻辑，详见末尾。
   真实 OwnershipRepository.replace 转移后，即使 Opportunity.owner 仍旧值也抑制旧受众。
   不一致须人工修正，不自动改 owner/完成 handoff；本次不扩展原 accept/transfer 权限规则。

## RED → GREEN 与最终验证

解释器为 `.venv/bin/python3.12`（Python 3.12.14）。所有 PG 用例只跑独立临时
`pgvector/pgvector:pg16`，固定 `unix:///var/run/docker.sock`，不 pull、不接用户 DB。

主要 RED（均在相应实现前执行）：

- `.venv/bin/python3.12 -m pytest tests/unit/test_human_handoff_flow.py -q --tb=short`：
  新定义参数不存在，4 failed / 15 passed；实现后 19 passed。
- 同文件 `-k new_owner`：新 handler 参数不存在，1 failed；随后补当前事实 scope 调用。
- `.venv/bin/python3.12 -m pytest tests/unit/test_pilot_profile.py -k owner_reminder -q --tb=short`：
  profile 拒绝新字段，1 failed；补映射后与 flow 组 45 passed。
- owned runner 执行 `tests/integration/test_owner_handoff_reminders.py`：2 failed，真实新 store
  缺少 handoff_guard 参数；补真实持锁边界后 2 passed。随后加入完整时钟、startup、真实 roots
  和所有权矩阵；早期 2/3/4/12 通过数字均是同文件扩充中的重叠结果，不能累加。
- 新 startup 用例在 helper 不存在时 1 failed；编码溢出用例在未加上界时 1 failed；scheduler
  bool 输入拒绝用例 1 failed / 4 passed；生产路由新原因只选站内用例真实 RED 1 failed。

最终定向命令/结果：

```text
.venv/bin/python3.12 -m pytest tests/unit/test_human_handoff_flow.py tests/unit/test_pilot_profile.py tests/unit/test_api_runtime_config.py tests/unit/test_notification_templates.py tests/unit/test_notification_projection.py tests/unit/test_notification_worker.py tests/unit/test_api_runtime.py -q --tb=short
274 passed / 2.83s

.venv/bin/python3.12 -m pytest tests/unit/test_scheduler_worker_config.py tests/unit/test_scheduler_sourcing_runtime.py tests/unit/test_scheduler_quotation_activation.py tests/unit/test_opportunities_handoff.py tests/unit/test_crm_handoff_router.py -q --tb=short
107 passed / 1.85s

.venv/bin/python3.12 .superpowers/sdd/2026-09-08-owner-handoff-reminders/run_owned_tests.py tests/integration/test_owner_handoff_reminders.py tests/integration/test_human_handoff_workflow.py -k 'not 0007'
20 passed / 2 deselected / 5.94s
```

最后 PG 20 = 新提醒 14 + legacy v1 行为 6，全部实际执行、没有 skip。两个 0007 schema
迁移测试显式 deselected：本次没有 schema 改动，不重跑无关迁移 round-trip。新完整 roots
组覆盖真实配置 parser、API lifespan、CanonicalSchedulerBootstrap / SchedulerRuntimeFactory、
完整 Outbox 注册及 drain、真实业务 service、job/claim/template/router/站内存储；不以仅流程
构造代替 root 链路。测试时钟控制时间，不 sleep 两小时。

受影响 21 个 Python 文件 ruff check 通过；mypy 检查 17 个源码模块通过；
`.venv/bin/python3.12 scripts/check_boundaries.py` 七组全部通过；`git diff --check` 通过。
未跑全仓/浏览器/真实 Provider，未声称这些已经验收。

owned runner 用进程内随机凭证和 DSN 覆盖传入子进程的 DATABASE_URL / TEST_DATABASE_URL，
并设置 PYTHON_DOTENV_DISABLED=1；不读取继承的测试 URL。Docker 精确随机容器拥有专用 owner
label，每次 finally 核验同标签，remove(force=True, v=True) 仅清理该容器及其匿名卷；最后运行
返回 0，清理路径执行完成。不创建 MinIO，也没有全局 prune/删除卷/共享 Git 修复。

## 限制、审计与成本

- 只交付提醒。主动退回、Agent 接续、金额分档关闭与首个真实 profile 初始化仍未做。旧 SLA、
  积压阈值、金额分档及七档映射没有补业务默认；真实用户配置仍需独立解决缺项。
- 接受可能等待正在提交的站内事务；长期停机沿用 engine 的既有持久绝对轮次恢复，本次未增加
  补发合并/限频策略。通知任务完成只表示已消费，过时记录可被抑制；真实送达证据是站内行。
- 新根路径验证使用进程内合成 profile 与显式合成商业政策，临时私有 profile round-trip 单测
  仍是既有测试机制，不是用户 profile。没有真实邮件、客户数据或业务运营验收。
- 安全材料处理偏差如实记录：首次 DockerClient 构造在 try 外，固定 socket 尚未启动时显示了
  无凭证 FileNotFound 原始栈；立即将初始化移入捕获区。另读取既有通知单测文件开头时显示了
  其硬编码示例连接串；无真实凭证，未复制到报告。后续 PG 原始失败输出留在子进程内，仅呈现
  固定异常类别/测试名/计数。没有输出实际测试 DSN、随机凭证或 Cookie。
- 所有 Git 操作 capture_output，仅汇报 AppleDouble warning 数量；未修复共享 Git，未动历史
  output400。原工作目录 untracked 证据不纳入提交。本报告、harness、file list 均留给控制者归档。

## Fix round 1 — I1 隐式外键锁死锁

修复基线：`acd89d23ca289f92bdbe5a280cbdf70fb05384f0`。修复提交：`b52f833244560e7e22d18b562519a2120d2f277a`。
状态：实现完成，等待独立复审。本轮仅三个提交文件：
`infra/db/repositories/opportunities.py`、`tests/integration/test_owner_handoff_reminders.py`、
`docs/adr/0069-owner-handoff-reminders.md`。本报告仍由控制者归档，不提交 scratch。

I1 成立：原报告仅核对显式锁序，遗漏 OwnershipTransferHistory 的 from_owner / transferred_by
外键在 flush/commit 取得旧 Employee KEY SHARE。先锁账户归属的 transfer 与先持 Employee
FOR UPDATE 的 guard 存在循环等待；原“无逆向锁”结论已在正文及 ADR0069 明确撤销。

修复仅把 Employee 事实锁改成 `with_for_update(key_share=True)`；本机 SQLAlchemy/PostgreSQL
编译确认它在 read=False 下为 `FOR NO KEY UPDATE`。该锁兼容历史 FK KEY SHARE，仍与员工
停用 UPDATE 以及键更新/删除互斥。机会、归属、handoff 的锁模式、接受 UPDATE 串行边界和
所有当前事实/租户条件均不变，没有改 transfer、通用 engine 或其他生产功能。

新增三个持久回归：

- `test_transfer_history_and_owner_guard_commit_without_deadlock`：真实仓储 replace 先持归属行，
  guard 获取旧员工事实锁，事件屏障再放行真实历史 flush/commit。两条独立连接均完成：转移
  提交、guard 读到新归属并抑制旧站内提醒；检查唯一真实转移历史、新 owner、handoff 仍
  requested、实际站内数为零。失败只提取 SQLSTATE，不输出原始 DB 异常或参数。
- `test_employee_deactivation_waits_for_in_app_commit_then_suppresses`：真实 EmployeeRepository
  更新不能越过在途站内提交；解除屏障后先站内提交、再停用提交，后续站内 append 被抑制。
- `test_matching_active_run_allows_real_api_and_scheduler_startups`：通过实际 API 装配的
  workflow_engine 创建同版本 v7201 active Run，再实际进入 API lifespan 和 scheduler
  runtime 两个启动入口；均成功，Run 仍 active 且没有额外业务推进/站内记录。补齐原审查
  提示的正向入口证据，不再只以 compatibility helper 通过替代真实 startup。

RED（修改生产锁前）：

```text
.venv/bin/python3.12 .superpowers/sdd/2026-09-08-owner-handoff-reminders/run_owned_tests.py tests/integration/test_owner_handoff_reminders.py -k 'transfer_history_and_owner_guard or matching_active_run'
1 failed / 1 passed / 14 deselected / 3.58s

.venv/bin/python3.12 .superpowers/sdd/2026-09-08-owner-handoff-reminders/run_owned_tests.py tests/integration/test_owner_handoff_reminders.py -k transfer_history_and_owner_guard
1 failed / 15 deselected / 1.43s；outcomes.count("40P01") == 0 断言失败。
```

GREEN：锁改动后新增三例 `3 passed / 14 deselected / 2.79s`。最终受影响验收：

```text
.venv/bin/python3.12 .superpowers/sdd/2026-09-08-owner-handoff-reminders/run_owned_tests.py tests/integration/test_owner_handoff_reminders.py
17 passed / 6.33s，无 skip；包含原14条提醒/接受/启动/租户回归及新增3例。

.venv/bin/python3.12 -m pytest tests/unit/test_human_handoff_flow.py -q --tb=short
21 passed / 0.14s

.venv/bin/python3.12 -m ruff check infra/db/repositories/opportunities.py tests/integration/test_owner_handoff_reminders.py
All checks passed

.venv/bin/python3.12 -m mypy infra/db/repositories/opportunities.py
Success: no issues found in 1 source file

.venv/bin/python3.12 scripts/check_boundaries.py
七组全部通过

git diff --check
通过
```

未重跑此前274/107单测全组，也不把它们计入本轮验证。没有 schema 变化。仍使用原 owned
runner、固定 socket 和已有 pgvector 镜像；测试凭证/DSN 只在进程内传递，正常结束后按精确
owner label 删除唯一测试容器及其匿名卷，不 pull、不连接真实 profile。Git 输出仅统计
AppleDouble warning 数量；未改共享 Git 或任何历史 output 文件。本轮未发生原始数据库
异常/凭证输出。该修复仅消除已复现的 FK 锁循环，不宣称全库或未来锁关系均无死锁。
