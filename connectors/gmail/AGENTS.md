# connectors/gmail/ —— Gmail 连接器（深，参考实现范式）

## 能力范围

Phase 1 当前只实现经 Tool Gateway 的**单封发送**与不确定结果的**只读搜索恢复**。
回复拉取、退信/投诉解析、标签和 DNS 检查只保留接口骨架，不能在文档、演示或 UI 中
声称已经可运行。这个目录仍是其他 Connector 的参考实现——写新 Connector 前先读这里。

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

## 退信与投诉回调（未实现）

未来退信邮件（MAILER-DAEMON）和投诉反馈应解析成结构化事件，产出
`MessageBounced` / `ComplaintReceived`（带 dedup_key）交给域。解析必须保守：
判断不了硬/软时按软处理并标记待人工看。Phase 1 目前没有该 worker。

## 错误分类

```text
401/403（授权失效）→ 不可重试，告警（需要人工重新授权）
429 / 配额         → RateLimited，带 Gmail 返回的 retry_after
5xx / 网络失败且确定未写入 → provider_transient
网络/HTTP 失败且可能已写入 → reconciliation_required
400（参数错）      → 不可重试（代码 bug）
```

## 接口（client.py 实现）

```text
configure()                                   # 只在应用 composition 内解析凭证
health()                                      # 不返回 token/原始异常
send_once(GmailSendRequest) -> GmailSendResult
reconcile_once(GmailSendRequest) -> GmailSendResult  # 只搜索
close()
```

旧的 `send/fetch_new_messages/parse_bounce/add_label/check_dns_auth` 目前仍为
`NotImplementedError` 骨架。

## Phase 1 范围

单封人工批准/已批准 Campaign 边界内发送、确定性 header 搜索、交付确定性错误分类。
不做：回复 worker、退信/投诉 worker、标签、DNS 检查、批量历史导入、自动重发、
Gmail 之外的 Google 服务。
