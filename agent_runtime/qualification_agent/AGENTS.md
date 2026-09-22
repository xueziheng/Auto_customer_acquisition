# qualification_agent/ —— 回复识别与需求确认

## 职责

三件事：回复分类（14 类，conversations 域的枚举）、需求字段提取、下一问选择。

## 关键纪律

- **分类偏保守**：退订/投诉宁可误判为是（误判成拒绝还继续发就是投诉）；AUTO_REPLY 判定要准（误判会错停序列）
- 提取的字段全部带 provenance 指向具体消息；数量、时间等关键字段附客户原话摘录
- `unit` 与 `recurring_requirement` 不属于当前模型自动提取词表。现有
  `ReplyFieldEvidence.value` 只是字符串，不得将其猜测或强制转换为复购布尔值；
  `recurring_requirement` 只能由未来明确设计的 typed deterministic parser 或人工确认流程提供
- 下一问最多两个主题（conversations 域 NextQuestionSuggestion 的约束），基于需求完整度缺口选择
- 客户只说 "We may need hinges" 时问用途和尺寸范围，不问价格和交期

## 输入 / 输出

输入：入站消息原文（artifact 引用）、会话历史、当前需求完整度。
输出：ChangeSet（record_classification / update_need_fields / create_draft(追问) / request_handoff 条目）。

## 禁用工具

email.send、quote.*
