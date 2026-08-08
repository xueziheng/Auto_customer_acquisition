# workflows/outreach_campaign/ —— 触达流程（Phase 1 深）

## 触发

Campaign 激活（`activate` 成功后为每个入组 enrollment 启动一条 run，subject_ref = enrollment_id）。

## 状态与步骤

```text
draft_content        outreach_agent 生成本步邮件草稿（DISCOVERY intent 先行）
      ↓
prepare_send         outreach 域 prepare_send 综合检查
      │                （回复竞态/抑制/额度/身份许可，返回 SendAuthorization）
      ├── 未授权且原因是回复到达 → complete（序列由回复路径接管）
      ├── 未授权且原因是身份熔断 → wait_event(SendingIdentityActivated)
      ↓
send                 tool_gateway email.send（幂等键来自 SendAuthorization）
      ↓
record_sent          outreach 域 record_sent，推进步数
      ↓
wait_for_reply       WAITING_EVENT(ReplyReceived)，超时 = 步骤 wait_days
      ├── 回复到达 → complete（reply_qualification 流程接管）
      └── 超时且还有下一步 → draft_content（下一封）
          超时且序列走完 → complete（enrollment 转 completed）
```

## 关键约束

- **本流程不判断任何业务规则**：能不能发、发给谁、发几封全部由域服务回答，这里只编排
- 每步幂等键：`{enrollment_id}:{step_number}:{action}`
- Campaign 暂停/取消 → 本流程收 cancel，正在等待的步骤直接取消

## Phase 1 范围

以上全部。步骤 handler 在实现阶段落地（steps.py 占位）。
