# Agent 运行时

用户只看到一个 **Trade Agent**。后台是一个 Trade Manager 加九个专业能力，不是上百个 Agent。

---

## 一、最重要的一条：Manager 不编排流水线

「一个可见 Agent + 后台专业能力」是**用户体验设计**，不是执行架构。

如果让 Manager 大模型驱动整条「发现 → 验证 → 寻源 → 报价」流程，会同时得到三个问题：慢（每步都要模型往返）、贵（长上下文反复重放）、不可复现（同样输入不同结果，无法排障）。

正确分工：

```text
流水线由确定性代码和 workflows/ 驱动
大模型只在明确的点上介入
```

**大模型负责**：理解老板指令、提出需求假设、设计搜索策略、分析网页与文件、提取聊单信息、生成开发邮件、选择下一个该问的问题、比较候选、总结员工工作、解释成本、识别风险。

**确定性代码负责**：权限、去重、客户归属、金额与汇率、状态转换、配额、审批、发送、退订、幂等、日志、置信度推导。

一句话：**Agent 是入口和解释者，不是编排者。**

---

## 二、结构

```text
agent-runtime/
├── trade-manager/            用户可见入口：理解意图、路由到能力、汇总解释
├── demand-intelligence/      需求信号解读、需求假设生成
├── account-discovery/        企业与人群发现
├── outreach-agent/           开发邮件撰写、序列内容
├── qualification-agent/      回复分类、下一问选择、需求完整度判断
├── sourcing-agent/           供应商候选分析（Phase 2 主用）
├── costing-agent/            成本解释与遗漏项提醒（不算最终数字）
├── team-operations/          员工工作摘要、承诺提取
├── compliance-agent/         合规与数据质量检查
├── context-builder/          按用户与任务裁剪上下文和工具
├── skill-router/             按 trigger 选技能，一次只加载所需
└── guardrails/               输出护栏
```

每个能力目录有自己的 `AGENTS.md`，写明输入、输出、可用工具、禁用工具。

所有能力继承统一的 `CapabilityAgent` 基类。**模型选择是基类的一个参数**，不建模型路由——只有一家供应商时那是空转（见 [ROADMAP](../../ROADMAP.md#长期挂载点)）。

---

## 三、Guardrails

写入业务库前必须通过的检查，任何一条不过就拒绝并要求重试：

| 检查 | 拦什么 |
|---|---|
| 事实/推断分离 | 把推断写进事实字段 |
| 证据齐备 | 无来源的断言（硬边界 5） |
| **禁止概率值** | 模型输出 `confidence: 0.67` 这类数字（硬边界 3） |
| 禁止承诺 | 价格、交期、认证、库存、付款条件等未审批承诺 |
| 金额来源 | 模型直接给出的最终金额（硬边界 2） |
| 价格基准 | 用 `indicative` 价格生成客户可见报价（硬边界 7） |
| 租户一致 | 跨租户数据出现在同一上下文（硬边界 8） |
| 语言合规 | 客户可见内容的语言与目标市场不符 |

Guardrail 拒绝要返回**结构化原因**，让 Agent 知道怎么改，而不是盲目重试。

---

## 四、技能加载

一次任务只加载需要的几个 Skill，不把所有 prompt 塞进系统提示词。

`skill-router` 按 trigger 匹配（例如 `new_company_discovered`、`company_event_detected`、`reply_received`），读 manifest 拿到 `allowed_tools` / `blocked_tools` / `evidence_required`，交给 `context-builder` 装配。

manifest 规范见 `skills/manifests/schema.yaml`。

---

## 五、Run 与 Change Set

每次工作形成一条 **Trade Run**，不是只输出一段聊天。Run 保存：

```text
目标   老板指令   员工   客户   需求   技能   模型调用   搜索记录
网页证据   文件   浏览器 Trace   成本   业务变化   审批   外部发送   错误   重试
```

Agent 完成任务后**先产出 Change Set，不直接改库**：

```yaml
need_changes:         [create_hypothesis, mark_validated]
lead_changes:         [create_account, assign_employee]
conversation_changes: [create_draft]
sourcing_changes:     [start_case]
task_changes:         [create_follow_up]
notification_changes: [notify_manager]
```

低风险变更自动应用，高风险变更经 `domains/approvals/` 审批后应用。

**为什么要这层间接**：可以先给人看「Agent 打算做什么」，可以整批回滚，也可以在应用前跑一遍 Guardrails。

---

## 六、开发邮件的目的是发现需求

第一封邮件不该是「我们有 XX 产品，价格好，你要吗」。更好的写法让对方说出他缺什么：

```text
我注意到贵公司正在经营/生产某类产品。
我们目前在协助中国供应链满足该领域的采购需求。
想确认一下，你们目前在哪些产品、零部件、包装或供应方面最难找到合适的选择？
```

有具体证据时可以更精准：

```text
看到贵公司最近新增了户外家具系列。
这类产品通常涉及耐腐蚀五金、定制包装和替换配件。
请问这些方面目前是否有采购或备用供应商需求？
```

`outreach-agent` 的 prompt 资产必须体现这个取向。**目标是让客户表达「我真正缺什么」，不是立刻卖货。**

---

## 七、追问要克制

`qualification-agent` 一次只问当前最关键的缺失信息，不要一次抛十几个问题。

客户说 "We may need hinges."

```text
好：Could you share the intended application and approximate size range?
    That will help us identify suitable materials and suppliers.

差：请提供数量、材质、尺寸、颜色、包装、认证、目标价、付款方式和交期。
```

选择依据是需求完整度（0–5）当前缺哪一级，见 [01-domain-model.md](01-domain-model.md#三需求完整度-05)。

---

## 八、评估集

模型或 prompt 每次变更都要重跑业务评估集。评估集统一放 `tests/evals/`（全库唯一位置），覆盖 11 类样本：

```text
普通询盘   模糊需求   目录外需求   高意向回复   拒绝邮件   退订
复杂规格   员工聊单截图   错误产品匹配   供应商诱导价   低利润报价
```

没有评估集就换模型，等于拿生产客户做实验。
