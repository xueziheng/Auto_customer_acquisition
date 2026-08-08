# agent_runtime/ —— Agent 运行时

## 定位（最重要的一句话）

**Agent 是入口和解释者，不是编排者。** 流水线由确定性代码和 `workflows/` 驱动，大模型只在明确的点介入。让模型驱动整条流程会同时得到慢、贵、不可复现三个问题。设计文档：`docs/architecture/06-agent-runtime.md`。

## 分工表（每个能力的 AGENTS.md 都要遵守）

**模型负责**：理解老板指令、提需求假设、设计搜索策略、分析网页与文件、提取聊单、生成邮件、选下一问、比较候选、总结员工工作、解释成本、识别风险。

**确定性代码负责**：权限、去重、客户归属、金额与汇率、状态转换、配额、审批、发送、退订、幂等、日志、置信度推导。

## 结构

```text
trade_manager/        用户可见入口：理解意图、路由到能力、汇总解释
demand_intelligence/  信号解读、需求假设生成
account_discovery/    企业与人群发现
outreach_agent/       开发邮件撰写（第一封以发现需求为目标）
qualification_agent/  回复分类、下一问选择、需求提取
sourcing_agent/       供应商候选分析（Phase 2 主用）
costing_agent/        成本解释与遗漏提醒（不算数字）
team_operations/      员工上传提取、承诺提取、工作摘要
compliance_agent/     合规与数据质量检查
context_builder/      按用户与任务裁剪上下文（深）
skill_router/         技能选择与加载（深）
guardrails/           输出护栏（深）
base.py               CapabilityAgent 统一基类
```

模型选择是 `CapabilityAgent` 的参数，**不建 model_router**——只有一家供应商时那是空转。

## 三条运行时纪律

1. **所有输出过 Guardrails 再落库。** 无证据断言、概率值、未审批承诺、模型产出的金额，一律拦截并结构化退回。
2. **所有外部动作走 Tool Gateway。** 能力代码里不出现任何 SDK 直调。
3. **所有工作产出 Change Set，不直接改库。** 低风险变更自动应用，高风险走 `domains/approvals`。这层间接让「Agent 打算做什么」可以先给人看、可以整批回滚、可以在应用前再跑一遍护栏。

## Run 留痕

每次运行一条 Trade Run：目标、指令、技能、模型调用、搜索记录、证据、成本、变更集、审批、错误、重试。老板在 Run Center 看到的就是这个。

## 依赖白名单

```text
允许   shared.*、domains 的 service.py 接口、tool_gateway、skills 的 manifest
禁止   domains 的 models.py / repository.py
禁止   任何模型 SDK 直调之外的外部 SDK（模型调用本身也要经统一封装计量）
```

## 评估纪律

模型或 prompt 变更必须重跑 `tests/evals/` 的业务评估集。没有评估就换模型，等于拿生产客户做实验。

## Phase 1 范围

深：context_builder、skill_router、guardrails、base。
浅：九个能力目录各一份 AGENTS.md + 入口类。sourcing_agent 与 costing_agent 在 Phase 1 只服务人工流程（解释与提醒），不驱动自动寻源。
