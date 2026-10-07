# apps/email_feedback_worker/ —— 邮件反馈拉取进程

## 职责

本进程只编排 Gmail feedback 只读工具、整页反馈处理器、健康服务与生命周期资源。
业务判定留在 `workflows/email_feedback/` 和两个域服务，worker 不解析 MIME、不直接写业务表。

## 硬约束

- 以租户和 mailbox alias 派生 PostgreSQL session advisory lock；未持锁不得抓取。
- 每页固定顺序为：锁 heartbeat → 短 session 读 cursor → Tool Gateway 抓取 → 再次
  heartbeat → 整页原子提交。第二次 heartbeat 失败必须丢弃已抓页面。
- 只注册 `email.feedback.fetch`；禁止注册或调用发送 handler。
- bootstrap 固定 30 天，不接受环境变量覆盖；cursor 只能在整页事务提交时推进。
- SIGINT/SIGTERM 只设置 stop flag，不取消在途 fetch/transaction。
- 日志、health 与 metrics 只能包含固定分类、tenant、mailbox alias 和低基数枚举；
  禁止异常原文、OAuth、provider/message ID、邮箱地址、客户文本或 DSN。

## 健康语义

只暴露 `/health/live` 与 `/health/ready`，绑定 `0.0.0.0` 的严格端口并关闭 access
log。provider 暂时失败只标记 `degraded`，不把已通过 config/schema/DB/registry 的
readiness 改成失败；disabled 模式 live 但永不 ready，也不取锁或抓取。

## ADR0075 本人邮箱入口

`python -m apps.email_feedback_worker.mailbox` 是本进程的独立运行模式，只注册
`email.mailbox.fetch`，与原 feedback 模式互不装配。用户明确选择全账号历史同步时，
新模式不适用原反馈 30 天限制。仍强制租户＋邮箱 advisory lock、前后心跳和整页原子提交，
SIGINT/SIGTERM 不取消在途页。只读 OAuth 授权由操作者运行本机向导完成；不提供发信端口。
新模式通过本人 Web 页的计数、阶段、最近成功/尝试时间和固定错误展示状态；不复用旧模式健康端口。
