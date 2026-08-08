# costing_agent/ —— 成本解释（不算数字）

## 职责

四件事：对照 CostItemType 全清单识别可能的遗漏项、解释计算结果（人话）、提醒低利润风险、生成客户可读的价格说明。

## 最重要的边界

**不产出任何进入计算的数字**（硬边界 2）。发现「可能漏了报关费」时，产出的是待确认建议（entered_by 为空的 CostItem 提案），由人确认后才参与计算。guardrails 的 no_model_money 会拦截违规输出。

## 输入 / 输出

输入：CostSheet 视图、CostBreakdown（确定性代码算好的）、机会上下文。
输出：ChangeSet（suggest_cost_item / risk_note / customer_explanation 条目）。

## 禁用工具

一切写入类工具（本能力只产建议与文本）
