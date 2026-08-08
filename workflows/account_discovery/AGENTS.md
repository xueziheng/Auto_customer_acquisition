# workflows/account_discovery/ —— 企业与联系人流程（Phase 1 浅）

## 触发

需求假设通过打分门槛但缺可联系的联系人。

## 主要状态

```text
find_company_details（account_discovery 能力：官网、域名）
→ resolve_account（prospecting 消歧，域名优先）
→ find_contacts（contact.enrich 经 tool_gateway）
→ verify_contacts（contact.verify；risky/unknown 丢弃并记成本）
→ record_legal_basis → assign_owner（employees 归属锁）→ complete
```

## 关键约束

验证不通过的联系人不入库为可用状态（硬边界 6）；每个联系人成本上报；企业已有归属锁则跳过 assign_owner。
