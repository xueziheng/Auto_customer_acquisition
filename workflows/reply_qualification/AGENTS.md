# workflows/reply_qualification/ —— 回复处理流程（Phase 1 深）

## 触发

`ReplyReceived` 事件（conversations 域在入站消息落库并分类后发布）。subject_ref = message_id。

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
