# domains/outreach/ —— 触达域

## 职责

Campaign 边界、发送序列、抑制名单。回答「向谁、以什么节奏、在什么边界内发」。

**不负责**：写邮件内容（`agent_runtime/outreach_agent`）、实际发送（`connectors/` 经 `tool_gateway/`）、身份信誉（`domains/sending_identity`）。

## 核心设计：Campaign 是被批准的边界，不是发送队列

这是「自主但有界」的落地。老板**一次性批准一个边界清晰的 Campaign**，而不是逐封审批开发信：

```text
campaign:
  markets: [United States, Germany]
  target_entity_types: [importer, manufacturer, distributor]
  allowed_categories: [hardware, packaging]
  senders: [si_001, si_002]          # 只能选 COLD_OUTREACH 身份
  sequence:
    max_messages: 3
    stop_on_reply: true
  daily_limits:
    new_contacts: 50
    total_messages: 100
  handoff_triggers: [quantity_provided, sample_requested, quote_requested]
```

Agent 在边界内自主工作；越界的动作被 `tool_gateway` 拒绝。**改边界 = 新的 Campaign 版本，需要重新批准。**

即使在已批准边界内，包含价格等承诺内容的邮件仍要逐次审批（见根 AGENTS.md 第六节）。

## 第一封邮件的目标是发现需求，不是卖货

序列步骤带 `intent` 字段（`discovery` / `presentation`）。第一步必须是 `discovery`：

```text
差：我们有 XX 产品，价格很好，你需要吗？
好：你们目前在哪些产品、零部件、包装或供应方面最难找到合适的选择？
```

原因：这套系统的中心是需求，不是产品。以卖货开场会把对话锁死在「要/不要」，以需求开场能让客户说出「我真正缺什么」——那才是后续一切的原料。`agent_runtime/outreach_agent` 生成内容时读这个字段。

## Campaign 与 Enrollment 状态机

Campaign current row 只允许以下精确边：

```text
draft → pending_approval / cancelled
pending_approval → active / cancelled
active → paused / completed / cancelled / pending_approval
paused → active / completed / cancelled / pending_approval
completed / cancelled → 无后继
```

Campaign 边界保存在不可变 version row。修改边界必须追加新版本并清空旧审批绑定；新版本重新获得**精确版本**批准后才能激活，不能复用旧版本 approval。

每个联系人一条 Enrollment，`enrolled`、`in_sequence` 是仅有的活跃状态；其余都是无后继终态：

```text
enrolled → in_sequence → in_sequence → completed
    │            │
    └────────────┴→ replied / stopped_suppressed / stopped_bounced /
                    stopped_manual / stopped_identity_unavailable
```

**`stop_on_reply` 要在发送时二次检查，不能只依赖回复事件**：回复可能在第 2 步发送前几秒到达，事件还没处理完。发送前查一次「该会话是否已有回复」，竞态就关掉了。

## 抑制名单：全局生效

```text
范围   contact（联系人级）｜ account（企业级）
原因   unsubscribe / complaint / hard_bounce / manual_block /
       competitor / existing_customer_conflict
```

**跨 Campaign、跨发件身份、跨员工生效，永不删除记录。** 公共服务没有 update、delete 或 unsuppress 路径；同一幂等键只在 typed target、reason、source_ref、occurred_at 全部一致时返回原事实。

六个固定原因是：`unsubscribe`、`complaint`、`hard_bounce`、`manual_block`、`competitor`、`existing_customer_conflict`。SYSTEM 只能对精确单一 target 写前三个自动原因；后三个只能由人工权限写入。

客户说「不要再联系我们公司」时必须抑制整个企业——只抑制回信那个人，换个联系人继续发，既失礼又有法律风险（退订请求覆盖的是「你们公司别再发了」，不是「别发给我这个邮箱」）。

## 硬边界 6：未验证的联系方式进不了序列

enrollment 创建时校验联系方式的 `ContactPointVerified` 状态。未验证地址产生硬退信 → 退信率升高 → 触发身份熔断。这条门禁拦的是连带损害，不只是单封失败。

## 依赖白名单

```text
允许   shared.events, shared.schemas, shared.errors
禁止   任何其他 domains/*、任何外部 SDK
```

发送许可（今日额度、身份状态）由上层调 `sending_identity` 的服务后传入，本域不直接查。

联系人资格、发件身份资格、Campaign 审批与当前回复状态都来自 `service.py` 暴露的只读 Provider Protocol；provider 不可用、tenant/resource 不匹配一律 fail closed。持久化只经 tenant-scoped Repository/UoW；本域不调用 connector、SDK 或其他域内部实现。

`MessageAttempt` 只是 durable、幂等的发送准备记录，**不是发送授权**。4B-2 Tool Gateway 在实际外发前必须重新检查当前 Campaign、Enrollment、回复、抑制、联系人资格、发件身份和额度；不能因 Attempt 为 `reserved` 就直接发送。

Attempt 的发送状态精确为：

```text
reserved → sending → sent
reserved / sending → failed_transient / failed_permanent
```

迁移 `0011` 增加 `send_claimed_at`。claim 必须在同一事务里锁定 Campaign →
Enrollment → Attempt；批量 Enrollment 按 ID 排序。随后 Sending Identity 额度沿用
domain → identity → reservation 的锁序。`sending` 是 Connector 可能已经开始的持久证据，
不能回写成 `reserved` 来触发重发。

每次发送准备都从当前事实重新计算。`sent` Attempt 可以生成终态 preflight，供 Tool
Gateway 用 canonical ledger 返回既有结果；但不能再次 claim。若 canonical ledger 丢失或
不一致，必须固定失败，不能借终态 bypass 重发。Attempt 完成失败时 Connector 结果仍按
不确定交付处理，由 Tool Gateway 搜索恢复；本域不调用 Gmail。

Attempt / audit / outbox 只记录安全 ID、typed state/category、provider reference；不得
持久化或记录邮箱地址、主题、正文、OAuth token、完整请求与客户原话。

## 发布的事件

`MessageSent`、`SuppressionAdded`

## 订阅的事件

`ReplyReceived`（停序列）、`MessageBounced`（硬退信 → 抑制；软退信计数，连续 3 次按硬处理）、`ComplaintReceived`（立即抑制）、`UnsubscribeReceived`（抑制，按请求范围决定级别）、`SendingIdentityThrottled` / `SendingIdentitySuspended`（暂停该身份下的发送计划）

## 禁止事项

- 不允许绕过抑制名单（没有任何白名单机制）
- 不允许未验证联系方式入组
- 不允许序列超过 Campaign 的 `max_messages`
- 不允许移除抑制记录（Phase 1 没有公共 removal API）

## Phase 1 范围

Campaign 边界模型与版本化、序列状态机、抑制名单、每日限额计数、发送 Attempt claim
与 terminal completion。

不做：积分限额（Phase 3 挂载点，字段位置留好）、多渠道序列（只有邮件）、A/B 测试。
