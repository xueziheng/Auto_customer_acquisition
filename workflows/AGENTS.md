# workflows/ —— 长流程状态机

## 为什么存在

贸易流程充满长等待：客户几天后回复、经理稍后审批、供应商两天后确认、报价 24 小时后过期。这些不能靠一次 Agent 对话完成，需要跨天、跨进程重启存活的推进机制。

## Phase 1 实现模式：Postgres 状态机（ADR 0003）

```text
状态表（workflow_runs / workflow_steps）
    + scheduler_worker 定时扫描到期步骤
    + 幂等推进
```

不上 Temporal。但**接口保持可替换**：状态定义、转换守卫、超时、重试、等待人工这些概念在 `engine/` 的接口层就存在，换 Temporal 时只换实现，流程定义不动。这是 ADR 0003 成立的前提条件，不是可选项。

## 三条纪律

1. **流程定义只编排，不含领域规则。** 步骤 handler 调域服务；「完整度够不够寻源」这类判断在域里。workflows 出现业务 if 就是放错了。
2. **每次推进幂等。** scheduler 重扫、worker 崩溃重启都会导致重复触发；每步带幂等键，重复推进是 no-op。这是 Postgres 状态机方案最容易出错的地方。
3. **LLM/IO 只在步骤 handler 内。** 状态转换本身是纯逻辑——将来迁 Temporal 时这就是 Workflow/Activity 的分界线。

`email_feedback/` 额外遵守整页原子性：先取得 tenant＋mailbox advisory transaction lock，
再校验整页 cursor 与所有 receipt fingerprint；任一同 event 异 payload、跨租户、域服务或
审计失败都回滚整页，旧 cursor 不动。整页成功才同时提交 receipt/quarantine、Outreach、
Sending Identity、Action/outbox 与新 cursor。duplicate 只推进安全计数/游标，不重复业务
效果；quarantine 只保存固定 reason 与 provider ref digest，不保存 MIME/header/address。

## 十二条流程与 Phase

| 流程 | 触发 | Phase |
|---|---|---|
| `outreach_campaign/` | Campaign 激活 | **1（深）** |
| `reply_qualification/` | 收到回复 | **1（深）** |
| `demand_discovery/` | 探索排程 / 老板指令 | 1 |
| `account_discovery/` | 假设需要联系人 | 1 |
| `human_handoff/` | 接管触发条件 | 1 |
| `employee_work_intake/` | 员工上传 | 1 |
| `email_feedback/` | Gmail 投递反馈整页读取 | **1（深）** |
| `sending_identity_auth/` | 发件身份 DNS 认证请求 | 1 |
| `playbook_change/` | Company Playbook 独立审批 | 1 |
| `country_policy_change/` | 国家政策包独立审批与批准后激活 | 1 |
| `sourcing_case/` | 需求达寻源门槛（Phase 1 人工推进） | 1 骨架 / 2 自动 |
| `quote_approval/` | 报价提交审批 | 1 骨架 / 2 自动 |

## 依赖白名单

```text
允许   shared.*、domains 的 service.py、agent_runtime、tool_gateway
禁止   domains 的 models.py / repository.py、外部 SDK 直调
```

## 切换 Temporal 的条件（ADR 0003）

单流程状态数 > 20、需要并行分支合并、需要子工作流嵌套、scheduler 成为瓶颈、发生两次以上幂等相关生产事故——任一出现即评估切换。
