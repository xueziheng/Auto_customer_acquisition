# apps/notification_worker/ —— 通知投递进程

## 职责

订阅需要通知的领域事件（HandoffRequested、SendingIdentitySuspended、CommitmentOverdue、HandoffQueueBacklogged、ApprovalDecided…），组装 Notification（自带上下文），经 notification_gateway 路由投递。

## 约束

- 投递失败重试不阻塞任何业务流程
- dedup_key 幂等（事件重复投递不重复通知）
- 通知内容标准见 notification_gateway/AGENTS.md

## 入口

`main.py`：注册事件订阅 → 消费循环。

## Slice 4 演示与验收

- 演示脚本经真实 `notification_worker_runtime` 与 `run_notification_worker`
  消费熔断通知 job 并完成双渠道投递；验收测试断言 `notification_deliveries`
  各渠道 `delivered`。见 `scripts/demo_slice4_manual_send.py` 与
  `tests/integration/test_demo_slice4_manual_send.py`。
- 演示只直插 employee 与受控连接器配置前置；事务邮件仍经
  `notification.email.send` Tool Gateway 发送（TRANSACTIONAL 身份）。
