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

## Phase 2 创建与恢复

`QuoteApplicationService`只编排公开端口：先当前四角色权限和完整key/intent重放，再读scope/context，
在context SHARE内进入报价机会锁session并重新查询同key，preflight通过才真实freeze/显式basis映射/quote提交。
完成操作在全部context/quote锁退出后执行，`PersistentQuoteCreationCompletionReader`只读真实持久报价；
未写入报价不得生成成功receipt。quote已提交而complete失败只允许原key恢复，不重冻或增加版本。
旧pending只能原prepared_by继续写，已持久成功可由当前有内部读取权的员工恢复；不扩张CRM或原件权限。

`basis_adapter`逐字段映射完整冻结快照，费用不伪装采购，cost_fx_rates与quote_fx分开；不能重算上游hash。
实际抬头由`PersistentQuoteIssuerReader`读取老板确认版本，不补样例公司。新scope/抬头不改写历史quote。
这些入口不完成T5审批，也不证明T8发送/客户文件授权；不可从纯客户投影或成功创建推断可以对客发送。
