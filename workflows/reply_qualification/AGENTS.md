# workflows/reply_qualification/ —— 回复处理流程（Phase 1 深）

## 触发

`InboundMessageStored` 事件（conversations 域在入站消息落库的同一事务发布）。
原 scheduler consumer 校验真实出站关联后启动 subject_ref = message_id 的原流程。
`ReplyReceived` 是分类落库后的结果事件，不能反向触发本流程。

## 状态与步骤

```text
classify             qualification_agent 分类（若入站时未分类）
      ↓
apply_actions        按 conversations.REPLY_ACTIONS 执行动作序列：
      │                stop_sequence / suppress / route_bounce …
      │                （每个动作调对应域服务，逐个幂等）
      ↓
extract_need         回复含需求信息时提取字段 → demand 域
      │                （update_need_fields 或 promote_to_validated）
      ↓
decide_next          三分支：
      ├── 命中接管触发条件 → request_handoff → complete
      ├── 需求未完整且客户在对话 → draft_follow_up（下一问，
      │       经审批边界后发送）→ complete
      └── 无需动作（拒绝/退订等已在 apply_actions 处理）→ complete
```

## 关键约束

- AUTO_REPLY 分类在 classify 步骤短路 complete——自动回复不触发任何后续
- extract_need 的每个字段带 provenance 指向本条消息
- 追问邮件同样过 guardrails 与 tool_gateway（含承诺内容会被拦）
- 全流程幂等：同一 message_id 重复触发是 no-op

## Phase 1 范围

以上全部。

## 耐久邮件入站（ADR0026）

inbound.py 只编排完整 ArchivedInboundPage，所有域服务共享外层事务；opaque cursor不解码。
inbound_management.py 只负责真人绑定、当前active boss权限、只读待核对与带版本原位重试；
不得接受正文、任意cursor、换身份或跳水位。关联仅精确In-Reply-To和真实SENT，不能视为客户认证或已验证需求。
