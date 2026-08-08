# connectors/gmail/ —— Gmail 连接器（深，参考实现范式）

## 能力范围

发送邮件、拉取回复线程、接收退信/投诉信号、加标签。**这是其他 connector 的参考实现**——写新 connector 前先读这个目录。

## 密钥归属

OAuth token 经密钥服务管理，引用名 `GMAIL_OAUTH_TOKEN_REF`。本 connector 运行期解析，token 不落日志、不进返回值、不进异常消息（硬边界 1）。授权流程（用户点击同意）在 apps 层完成，connector 只消费已授权凭证。

## 幂等

发送接口要求调用方传幂等键，connector 层再用 Gmail 的 thread/message 语义做二次防重：同一幂等键的重试**先查有没有发出去过**（搜索自定义 header），查到就返回既有 message_id 而不是再发。网络超时后的重试是重复发送的最大来源——「发出去了但响应丢了」必须按已发送处理。

## 退信与投诉回调

退信邮件（MAILER-DAEMON）和投诉反馈解析成结构化事件，产出 `MessageBounced` / `ComplaintReceived`（带 dedup_key）交给域。**解析要保守**：判断不了硬/软的按软处理并标记待人工看，误判硬退信会错误抑制有效联系人。

## 错误分类

```text
401/403（授权失效）→ 不可重试，告警（需要人工重新授权）
429 / 配额         → RateLimited，带 Gmail 返回的 retry_after
5xx / 网络超时     → TransientError
400（参数错）      → 不可重试（代码 bug）
```

## 接口（client.py 实现）

```text
send(idempotency_key, from_identity, to, subject, body, unsubscribe_url) -> message_ref
fetch_new_messages(since_cursor) -> (messages, next_cursor)
parse_bounce(raw_message) -> BounceEvent | None
add_label(message_ref, label) -> None
check_dns_auth(domain) -> {spf, dkim, dmarc}   # 发件域认证校验
```

## Phase 1 范围

以上全部。不做：Gmail 之外的 Google 服务、批量导入历史邮件。
