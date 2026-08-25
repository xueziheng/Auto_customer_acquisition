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

Hunter 联系人工具的生产装配还必须同时满足：环境中的安全配置哈希与 durable
Provider readiness 当前配置完全一致，且状态已验证；启动装配不得解析 Hunter 凭证，
不得调用 Provider。`runtime_composed` 只能由成功取得 advisory lock、并确认仍是同一
dedicated backend connection 的 scheduler 副本写入，且必须早于第一轮 cycle；未获锁、
锁已丢失或双工具未精确注册时一律不得写。激活失败必须释放同一把锁、零 cycle 退出，
日志不得包含异常原文、凭证引用、配置哈希或凭证值。

验证只能由授权真人经 `provider.hunter.validate` 逐次触发，scheduler 不得代跑或自动重试。
validation passed 后必须重启 singleton scheduler；Settings 只有在 matching
`runtime_composed` 提交后才可显示 ready。密钥轮换、认证修复和回滚均创建新的配置版本，
不得复用历史 validation/runtime；运行中的旧 adapter 必须用 live guard 在 Connector 创建和
凭证解析前关闭。Provider ready 也不替代每次国家政策、Playbook、suppression 与 quota 检查。
运维证据只能使用 `docs/operations/hunter-provider-readiness.md` 的安全 allowlist。

## 入口

`main.py`：装配依赖 → 注册流程定义 → 循环。优雅停机：收到信号后完成当前批再退出，不中断在途事务。
