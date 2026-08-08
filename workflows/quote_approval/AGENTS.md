# workflows/quote_approval/ —— 报价审批流程（Phase 1 骨架）

## 触发

报价提交审批（quotations.submit_for_approval）。

## 主要状态

```text
assemble_package（组装审批包：报价+成本+利润+历史版本，costing_agent 生成解释）
→ wait_decision（WAITING_EVENT(ApprovalDecided)，过期走 approvals 的 expire）
   ├── 通过 → notify_prepared_by → complete（发送由人工/后续流程执行）
   └── 否决 → notify_prepared_by（带原因）→ complete
```

## 关键约束

审批人排除起草人与机会负责人（approvals 域强制）；低于利润底线的在包里显著标出；过期不自动重提。
