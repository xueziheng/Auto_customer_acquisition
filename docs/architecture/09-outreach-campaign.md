# 触达 Campaign 与邮件反馈闭环

本页只描述 Phase 1 已交付能力。Campaign 是老板批准的可执行边界，不是待发邮箱列表；
4C2 的反馈运营 UI、人工 cursor 重置界面和 Campaign 自动重试策略均未实现。

## 发送链路

```text
Campaign 精确版本批准
  → Enrollment 当前事实与全局抑制检查
  → MessageAttempt reserved / sending / sent
  → Tool Gateway 六阶段与 canonical ledger
  → Gmail 确定性 Message-ID / X-TradeOS-Idempotency-V1
```

Attempt 只能绑定一组 route-scoped correlation；同值重试幂等，改写或两个键指向不同
Attempt 固定拒绝。Connector 结果不确定时只允许搜索恢复，禁止自动再次 send。

## 投递反馈链路

`apps/email_feedback_worker` 按 tenant＋mailbox 获取 advisory lock，经 Tool Gateway typed read
调用 Gmail Connector。首次 cursor 固定 bootstrap 最近 30 天；之后只走 Gmail history。
Connector 只接受 RFC 3464 `multipart/report`：`5.x.x` 是 hard bounce，`4.x.x` 是 soft
bounce，无法确定的候选进入固定 quarantine，不读 Subject/正文/诊断文本猜测。

整页在一个 PostgreSQL 事务内提交：

```text
cursor/fingerprint 整页预检
  → receipt / quarantine
  → hard: ContactPoint 抑制＋停止活跃 Enrollment＋Sending Identity hard-bounce fact
  → soft: receipt only
  → Action / outbox / 新 cursor
```

同 provider event 同 payload 是 duplicate；同 event 异 payload 整页失败且零域调用。原始
MIME、邮箱地址、header、OAuth、token、provider cursor 不持久化、不进日志。

## one-click 与运维

RFC 8058 GET 不写入；合法 POST 消费一次性 90 天 token，并原子写 ContactPoint 级退订
抑制。key 轮换必须保留仍有未过期 token 的旧 key。worker 收到 SIGTERM 要完成在途 page
再退出；429 保持 ready 但标记 provider degraded，并按 1–3600 秒 Retry-After 重试。

需要人工处理的情况：OAuth 401/403、Gmail history cursor 过旧、持续 provider permanent、
跨 route correlation 安全告警。当前没有 UI；只能依据固定健康端点、低基数 metrics 与安全
数据库事实执行 runbook，不能手改 receipt/cursor 来伪造恢复。
