# workflows/human_handoff/ —— 人工接管流程（Phase 1 浅）

## 触发

`HandoffRequested` 事件。

## 主要状态

```text
notify_owner（notification URGENT）
→ wait_acceptance（WAITING_EVENT(HandoffAccepted)，超时阈值 T1）
   ├── 接受 → complete
   ├── 超时 T1 → escalate_manager（通知经理）→ wait_acceptance(T2)
   └── 超时 T2 → escalate_boss → wait_acceptance（不再升级，持续提醒）
```

## 关键约束

超时升级不换负责人（换人是经理的决定，流程只通知）；每次升级记录进 handoff（SLA 违约审计）；队列积压统计由 opportunities 域的 get_queue_stats 负责，本流程只管单条。

## ADR0069 负责人提醒模式

显式 `owner_reminder_interval` 启用 notify_owner → wait_owner_acceptance 两步结构。
只通知当前在职负责员工，持续等待 HandoffAccepted；每轮先经机会公共持锁事实 scope。
此模式不调用经理/老板升级、不写 SLA 升级审计、不修改归属。周期由配置提供，没有业务默认。
原 v1 保留；版本 2..2147483647 永久编码本结构的整数周期秒数+1，未来结构必须新 type。
API/scheduler 启动拒绝与当前活跃 Run 不兼容的模式或周期，不能静默重解释旧运行。
