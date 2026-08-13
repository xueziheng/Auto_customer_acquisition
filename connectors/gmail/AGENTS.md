# connectors/gmail/ —— Gmail 连接器（深，参考实现范式）

## 能力范围

Phase 1 当前实现经 Tool Gateway 的**单封发送**、不确定结果的**只读搜索恢复**，
以及 Gmail RFC 3464 DSN 的 **typed 投递反馈读取**。回复正文拉取、投诉 FBL、标签和
DNS 检查仍未实现，不能在文档、演示或 UI 中声称已经可运行。这个目录仍是其他
Connector 的参考实现——写新 Connector 前先读这里。

## 密钥归属

OAuth token 经密钥服务管理，引用名 `GMAIL_OAUTH_TOKEN_REF`。本 connector 运行期解析，token 不落日志、不进返回值、不进异常消息（硬边界 1）。授权流程（用户点击同意）在 apps 层完成，connector 只消费已授权凭证。

## 幂等

发送请求必须包含由 Tool Gateway 派生的确定性 header：

```text
Message-ID: <确定性 HMAC 派生值>
X-TradeOS-Idempotency-V1: <确定性幂等摘要>
List-Unsubscribe: <安全退订 URL>
List-Unsubscribe-Post: List-Unsubscribe=One-Click
```

正常首次调用先按 `Message-ID` 与自定义 header 搜索；命中就返回既有 provider ref，
未命中才发送。恢复调用 `reconcile_once` **只搜索、不发送**。网络超时后的重试是
重复发送的最大来源——「发出去了但响应丢了」必须进入人工对账，不能按普通临时错误
自动重发。

## 投递反馈读取

只读取 `multipart/report; report-type=delivery-status`，并严格解析
`message/delivery-status` 的 `Status`：`5.x.x` 为 hard bounce，`4.x.x` 为 soft
bounce，其余候选固定成为 `UNPARSEABLE`，绝不根据 Subject、正文、收件人或诊断原文
猜测。原始 MIME 只在一次调用栈内存在；DTO 只保留 provider digest、ordinal、固定分类、
UTC 时间与 TradeOS 自有 correlation header。普通邮件返回空页结果，不产生反馈事实。

cursor 是 32 KiB 内的 v1 opaque 状态，只承载固定 30 天 bootstrap 边界、Gmail history
边界、provider 分页 token 与有界 pending ref；所有 provider 参数仍独立 URL 编码，cursor
不得进入日志。多 recipient block 跨页时重读不可变 Gmail message，并按 ordinal 跳过已交付
block，禁止持久化 MIME。

生产 transport 只连 `https://gmail.googleapis.com`。本地/测试必须显式启用 dev mode，且
只允许带端口的 `127.0.0.1`、`localhost` 或 `::1` HTTP；凭证、路径、query、fragment 和
任意生产替代域名一律拒绝。429 的 `Retry-After` 只接受 1–3600 秒，worker 在此期间保持
ready 并标记 provider degraded，不回退 cursor、不伪造成功。

## 错误分类

```text
401/403（授权失效）→ 不可重试，告警（需要人工重新授权）
429 / 配额         → RateLimited，带 Gmail 返回的 retry_after
5xx / 网络失败且确定未写入 → provider_transient
网络/HTTP 失败且可能已写入 → reconciliation_required
404（history cursor 过旧）→ provider_permanent，需人工重置 bootstrap
400（参数错）      → 不可重试（代码 bug）
```

## 接口（client.py 实现）

```text
configure()                                   # 只在应用 composition 内解析凭证
health_check()                                # 不返回 token/原始异常
send_once(GmailSendRequest) -> GmailSendResult
reconcile_once(GmailSendRequest) -> GmailSendResult  # 只搜索
fetch_feedback_page(alias, cursor, limit) -> EmailFeedbackPage
```

旧的 free-dict `fetch_new_messages`、`BounceEvent` 与 `parse_bounce` 合同已删除，不能恢复
第二套竞争接口。`add_label/check_dns_auth` 仍是 `NotImplementedError` 骨架。

## Phase 1 范围

单封人工批准/已批准 Campaign 边界内发送、确定性 header 搜索、DSN typed 读取、交付
确定性错误分类。不做：回复正文 worker、投诉 FBL、标签、DNS 检查、超过 30 天批量历史
导入、自动重发、Gmail 之外的 Google 服务。
