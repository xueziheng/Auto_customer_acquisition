# ADR 0080：业务回复的专用模型调用身份

日期：2026-10-05。状态：采纳，按已确认的回复模型接入计划实施。

## 背景

现有 QualificationAgent 能验证分类和逐字证据，但日常规则分类器不能提取需求字段。
直接借用聊天或研究身份无法证明当前回复属于哪个工作流、员工和付费调用。ADR0070 的
调用幂等身份包含配置版本；重启后盲目使用新版本可能为同一回复制造第二次调用。

## 决策

- 在共享 ModelCapability 增加 `reply_qualification`，保留现有身份字段、事件、Provenance
  和 Gateway 核心管线。回复身份固定使用 canonical Run、当前员工及其 user_id、配置版本，
  `turn_id=None`、`sequence=0`。
- 仅原 singleton scheduler 的显式 standalone 回复组合可构造身份。按 tenant 读取 Run、
  入站消息、出站 Attempt 和 Enrollment，验证当前 classify 步骤、完整关联以及
  `reply:{message_id}` 幂等键；不从邮件或模型输出接受身份。
- 当前员工回复权限与现有模型配置授权取交集；模型执行前后重新校验。查询借用原 session
  factory，不在另一连接重取 engine 已持有的 Run 行锁。
- 读取同 tenant、Run、回复 capability、sequence=0 的全部版本调用历史；员工、用户、模型、
  配置版本不一致或存在冲突时拒绝。付费预留、计量和 unknown 语义仍由既有 Gateway 管理，
  配置变化不能使原调用自动重发。
- 通过现有 `classifier=` 接口装配 QualificationAgent；领域规则继续决定退订、需求建档和
  人工接管。模型只分类并提取带客户原话的候选字段，不产生商业承诺或执行动作。

## 后果与验收

保持保守的失败语义：已付费但结果丢失可能留下待核对记录，不能以再次请求取回结果。
这次契约扩展不授权新 Campaign、修改发件身份或发送客户邮件。配置和明确限额仍由操作者提供。

真实 Postgres 测试覆盖租户、业务关联、行锁和跨版本历史；Gateway 测试覆盖撤权、配额和
未知调用不重发；完整业务链、160 条冻结回复语料和网页接管分别提供验收证据。
受控 Provider 结果不得冒充真实模型验收，演示数据不得作为日常客户事实。
