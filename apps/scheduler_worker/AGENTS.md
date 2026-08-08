# apps/scheduler_worker/ —— 调度进程（Phase 1 核心）

## 职责

Postgres 状态机的驱动器 + 全部定时任务。它停了，所有流程都停——**必须有「停止推进」的告警**（docs/architecture/11 的告警清单）。

## 主循环

```text
每 N 秒：
  workflow_engine.poll_due()        推进到期步骤
  outbox 投递                        事件总线的 outbox 扫描
  quotations.expire_overdue()       报价过期
  approvals.expire_overdue()        审批过期
  commitments.scan_overdue()        承诺逾期与升级
  sending_identity 周期评估          信誉窗口重算
  opportunities.get_queue_stats()   接管队列积压检查
```

## 唯一的硬约束

**单副本运行**（或先拿分布式锁）。重复扫描 = 重复推进状态机 = 重复发送。幂等键是兜底，不是许可证。

## 入口

`main.py`：装配依赖 → 注册流程定义 → 循环。优雅停机：收到信号后完成当前批再退出，不中断在途事务。
