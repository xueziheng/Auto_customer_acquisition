# apps/notification_worker/ —— 通知投递进程

## 职责

订阅需要通知的领域事件（HandoffRequested、SendingIdentitySuspended、CommitmentOverdue、HandoffQueueBacklogged、ApprovalDecided…），组装 Notification（自带上下文），经 notification_gateway 路由投递。

## 约束

- 投递失败重试不阻塞任何业务流程
- dedup_key 幂等（事件重复投递不重复通知）
- 通知内容标准见 notification_gateway/AGENTS.md

## 入口

`main.py`：注册事件订阅 → 消费循环。

## 本机受控例外（ADR0027）

仅controlled专用入口可显式controlled_in_app，只注册站内，保留优先级/模板/受众及真实持久投递。
能力必须披露email disabled，job完成不代表邮件或多渠道送达。生产默认无邮件仍拒绝。
