# tests/ —— 测试与业务评估集

## 分层

```text
tests/
├── unit/           纯函数与域逻辑：状态机转换表、置信度推导、
│                   Decimal 成本计算、完整度推导、打分门槛、
│                   幂等键构造。不碰数据库。
├── integration/    带 Postgres 的仓储与流程测试：租户过滤、
│                   唯一约束（归属锁、幂等键）、状态机推进、
│                   outbox 投递。
└── evals/          业务评估集（**全库唯一位置**，Agent 能力与
                    Skill 共用）。
```

## 必须优先覆盖的单元测试（骨架期就定下）

这些函数承载硬边界，实现之日就是测试之日：

```text
shared.schemas.evidence.derive_confidence    每条推导规则一组用例
shared.schemas.money                          Decimal 校验、币种不匹配、float 拒绝
domains.demand  完整度推导、can_promote_to_validated（推断不许晋升）
domains.opportunities  check_gates（全查不短路）、mark_lost 必带原因
domains.sending_identity  预热曲线、熔断阈值、样本下限、角色隔离
domains.quotations  contains_forbidden_commitment、状态机无审批旁路
domains.approvals  自批禁止、过期不可批、幂等应用
tool_gateway  stage 顺序、结构化拒绝、幂等短路
```

## 业务评估集 `evals/`

设计稿第 45.9 节的 11 类样本。**每次更换模型或修改 prompt 都必须重跑**——没有评估就换模型，等于拿生产客户做实验。

```text
evals/
├── inquiry/normal/            普通询盘
├── inquiry/vague/             模糊需求（"We may need hinges"）
├── inquiry/off_catalog/       目录外需求
├── replies/high_intent/       高意向回复
├── replies/rejection/         拒绝邮件
├── replies/unsubscribe/       退订（含各种委婉写法——误判代价最高）
├── replies/auto_reply/        自动回复（必须不算回复）
├── specs/complex/             复杂规格
├── uploads/chat_screenshots/  员工聊单截图
├── matching/wrong_match/      错误产品匹配案例
├── sourcing/bait_price/       供应商诱导价
├── quoting/low_margin/        低利润报价
├── hypothesis/                需求假设生成（无凭据断言的陷阱样本）
└── outreach/                  开发信（禁止承诺的陷阱样本）
```

每类样本 = 输入 + 期望行为（分类值 / 必须拦截 / 必须提取的字段）。样本来源优先真实数据脱敏；人工纠正过的分类（`correct_classification`）自动成为候选样本。

## 纪律

- 测试断言**行为**不断言实现（断言"推断不能晋升"，不断言内部调用了哪个方法）
- 评估集样本只增不改——改样本等于移动球门
- CI 顺序：unit → integration → evals（模型相关变更时）
