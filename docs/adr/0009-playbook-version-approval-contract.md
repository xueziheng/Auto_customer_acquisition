# ADR 0009：Company Playbook 版本审批契约

- 状态：已接受
- 日期：2026-08-24

## 背景

Company Playbook 决定排除品类、目标国家、交易金额底线与额外人工审批动作，直接影响需求探索和客户触达。旧公共接口 `update_playbook` 没有表达候选版本、独立审批、精确批准内容与生效顺序，无法证明某次配置为什么生效，也不能安全处理审批事件重放。

## 决策

1. 移除直接 `update_playbook`，不保留兼容方法、bootstrap、force 或 apply-now 旁路；首次配置和后续修改都要求独立审批人。
2. Playbook 内容写入不可变版本；生效另写 append-only activation。版本 payload 与 activation 是分离事实，批准不回写候选版本。
3. 组织域不导入审批域。工作流只向组织域传入窄 `PlaybookApprovalFact`，组织域逐字段核对审批类型、版本内容哈希和 `change_set_ref`。
4. 首次配置也必须由不同于提交人的 boss 或 manager 审批，系统不能自行补默认 Playbook。
5. 公共强类型 ID 新增 `PlaybookVersionId` 与 `PlaybookActivationId`。
6. 人工录入 Provenance 保存在不可变版本上，source 指向精确版本与提交员工；人工批准的 `approved_by/approved_at` 和系统实际应用的 `activated_by/activated_at` 分别保存在 activation 中，不混用时间语义。

## 结果

系统可以复原候选内容、提交基准、批准依据和实际生效顺序；同一审批事件重放可按版本与 approval ID 幂等识别。代价是读取当前 Playbook 时需要组合版本与 activation，提案和激活也必须共享租户级事务锁。该代价换取了审计性、陈旧基准保护和无旁路审批边界。
