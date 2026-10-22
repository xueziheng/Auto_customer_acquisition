# workflows/account_discovery/ —— 企业与联系人流程（Phase 1 浅）

## 触发

需求假设通过打分门槛但缺可联系的联系人。

## 主要状态

```text
bind_campaign（v2：在 Provider 成本前绑定精确 Campaign 版本）
→ find_company_details（account_discovery 能力：官网、域名）
→ resolve_account（prospecting 消歧，域名优先）
→ find_contacts（contact.enrich 经 tool_gateway）
→ verify_contacts（contact.verify；risky/unknown 丢弃并记成本）
→ record_legal_basis → assign_owner（employees 归属锁）
→ await_campaign_activation（耐久事件等待）→ enroll_campaign → complete
```

## 关键约束

验证不通过的联系人不入库为可用状态（硬边界 6）；每个联系人成本上报；企业已有归属锁则跳过 assign_owner。

worker 必须同时注册精确 v1 与 v2 definition。v1 保留原 handler ref、直接
`assign_owner → enroll_campaign` 转换以及无 `campaign_version` context 的 legacy 版本语义；
新 run 选择 v2。active 事件只有在事件版本、run 绑定版本和当前持久 Campaign 版本三者一致时
才能唤醒 v2，Enrollment 最终仍由 Outreach 公共服务在行锁内复核。

ADR0094：assign_owner须持久保存员工域返回的owner_id与owner_locked_at；v1/v2入组都提交
此原始归属版本，由Outreach在账户advisory锁内比较后写入。旧任务缺字段必须停止，不能
以当前owner或当前时间补造，也不能因老板scope而跳过；转交后须重新明确安排。
