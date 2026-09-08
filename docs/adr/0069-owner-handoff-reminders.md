# ADR0069：待接管负责人周期提醒与接受提交边界

日期：2026-09-08。状态：已接受；仅实现站内负责人提醒，不代表真实 profile 已启用。

## 决策

新增显式 `owner_reminder_interval_seconds`，未设置沿用原 T1/T2 模式。已确认的本次值是
7200 秒；代码无隐藏的两小时默认，也不把它当业务 SLA。仅提醒尚未接受且接管指派、机会 owner、
账户 OwnershipLock 三者一致的在职员工。缺失当前事实拒绝；不一致或已接受时抑制，归属不变。

复用 `human_handoff` 类型、持久提醒轮次和绝对 UTC 锚点。v1 永久保留旧定义；版本
2..2147483647 永久保留给当前两步结构，以 `周期整数秒 + 1` 编码（7200 秒对应 v7201）。
输入限定 1..2147483646 整数秒，防 PostgreSQL Integer 溢出。今后流程结构变更必须使用新
workflow type，不得挪用此版本空间。既有 start 幂等键保留，跨版本重放原事件由引擎拒绝。

API lifespan 与 scheduler 资源启动阶段按 tenant 检查非 completed/failed/cancelled 的
human_handoff Run。活跃版本与所选模式/周期不一致即固定 `handoff_reminder_policy_conflict`，
在业务推进前退出。包括新模式遇旧 v1、旧模式遇新版本、新周期遇旧周期。必须停止所有应用，
恢复原配置并完成旧运行，再选新模式；没有自动迁移、回填或混跑支持。已终结事件的重放仍必须
保留原策略语义，不能用更改幂等键来制造新 Run。API 与 scheduler 必须使用同一 profile。

## 并发边界

`OpportunityService.handoff_notification_scope(tenant_id, handoff_id, opportunity_id, recipient, *, actor)`
返回异步上下文管理器。SYSTEM actor 必须精确限定 opportunity；在任何读取前授权。
窄 `HandoffNotificationServiceImpl` 不依赖打分、SLA 或任何商业默认，供通知 worker 装配。

Infra 以固定顺序持有员工、机会、账户归属、handoff 行锁；所有查询均限定 tenant。域解释当前事实，
不导入员工域内部。扫描在该 scope 内写 job；真实站内 append 同样在 scope 内完成另一连接的
INSERT **和 commit**，再释放事实锁。接受路径的 `accept_if_requested` UPDATE 必须取得同一
handoff 行锁，因此：通知先持锁时，站内提交先于接受提交；接受先提交时，通知重读得到 accepted，
不写站内行。不是 check-then-send，也不以 Outbox 是否已消费作为接受事实。

机会分配只写机会行，账户 transfer 只原子替换归属行，员工停用只更新员工事实；当前没有逆向
取得上述整条锁链的路径。scope 内不调用模板、router 或任何会重新请求同一事实锁的服务。
独立站内连接不反向锁业务行。待投递旧 job 可被消费但不产生新站内记录；缺失事实/依赖失败
仍走原 retry/reject，不能当作已投递。历史站内记录保留。

## 通知装配与兼容成本

API 与 scheduler 复用非进程 `apps/composition_support/handoff_notifications.py` 的机械映射。
scheduler 保留旧 notifier 导出；新模式完整注册只由 workflow 生成初始通知，不再同时注册
`notification.handoff_requested` 投影，避免原投影把经理/老板也作为受众及初始双写。
新原因码为 `owner_pending` 与 `owner_reminder`，文案为“待接管提醒”，不称为升级或 SLA 违约。
新模式经本机 `LOCAL_IN_APP` 真实 job/claim/template/router/store 链交付；新原因码在生产路由也仅选站内，避免排队后接受而另一渠道继续发送。原原因码的多渠道规则不变，不新增邮件授权。

代价：多一组短事务行锁与站内独立连接；接受可能等待正在提交的站内通知。长时间停机会沿用
引擎既有绝对轮次补进语义，不能宣称有本次未实现的补发合并。版本编码不是通用工作流迁移方案。
双归属不一致需人工修正已有事实；本次不修改 transfer/accept 的既有业务权限，不自动换负责人。
主动退回、Agent 接续、金额分档关闭、真实 profile 初始化均未实现。
