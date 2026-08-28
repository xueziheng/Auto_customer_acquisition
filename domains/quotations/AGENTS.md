# domains/quotations/ —— 报价域

## 职责

报价的版本化管理，以及**禁止自动承诺清单**的权威定义。

## 本域的核心资产：FORBIDDEN_AUTO_COMMITMENTS

设计稿第十节列出的「即使 Campaign 已批准也不能自动承诺」的清单，在本域落地为注册表。`tool_gateway` 和 `agent_runtime/guardrails` 都对着它检查。

判断规则一句话：**只要输出会构成对客户的商业承诺，就必须人工过目。**

一封邮件说"价格大约 $2.5"和说"我们的产品很耐用"是完全不同的事：前者客户会拿着截图来要求兑现，后者只是营销措辞。清单管的是前者。

## 状态机

```text
draft ──→ pending_approval ──→ approved ──→ sent ──→ accepted
              │                                 ├──→ rejected
              └──→ rejected（审批不通过）        ├──→ expired
                                                └──→ superseded（被新版本替代）
```

**只有 `approved` 状态的报价可以发送**，门就是 `QuoteApproved` 事件——没有这个事件，`tool_gateway` 拒绝执行含报价内容的发送。

## 版本不可变

报价修改 = 新版本。旧版本永久保留，绑定它当时的成本表版本、汇率快照、价格快照。

原因和成本域一致：客户两周后回来砍价时，「我们上次报的 $2.80 是基于什么算的」必须能立刻回答。供应商中途改价不影响已发出的报价记录。

## 有效期必填

没有有效期的报价是开放式承诺：三个月后客户拿着旧价格下单，而汇率和供应商价格早变了。每个报价必须带 `valid_until`，过期自动转 `expired`。

## 价格基准校验

报价的每一行都要能追溯到 `QUOTED` 基准的价格快照（硬边界 7）。成本域的 `lock_for_quote` 是第一道门，本域创建报价时再验一次——两道门是因为代价不对称：多查一次很便宜，亏本报价很贵。

## 依赖白名单

```text
允许   shared.events, shared.schemas, shared.errors
禁止   任何其他 domains/*、任何外部 SDK
```

成本数据由上层从 costing 域取出后传入（`CostBreakdown` 的值），本域不直接调。

## 发布 / 订阅

发布：`QuoteApproved`
订阅：`ApprovalDecided`（审批结果落到报价状态）

## 禁止事项

- 不允许跳过审批直接 `sent`
- 不允许修改已发送的报价（开新版本）
- 不允许无 `valid_until` 的报价
- 不允许报价行引用 INDICATIVE 价格快照

## Phase 1 范围

报价模型、版本化、状态机、禁止承诺注册表、审批衔接。Phase 1 报价由人工起草，但走同一套结构和门禁。

不做：报价 PDF 生成（Phase 2）、多币种并列报价、自动折扣策略。

## Phase 2 报价准备用途

`QuoteBusinessContext`仅供可信内部应用；含完整Need原文摘录，不可直接序列化为HTTP/日志。
prepare/read_internal只开放当前在职boss/product/sourcing/finance，不借此扩大CRM、客户文件、
消息或原件权限。审批仍由approvals决定，客户文件仍按机会ABAC，不复用准备用途policy。

业务hash绑定完整Need来源、负责人、原起草人和抬头版本；不含本次actor、runtime或正常机会状态。
完整规格保留material/packaging等维度及None，供应商自由文本不能与canonical JSON猜测等价。
context lease按员工→机会→Need取SHARE，直到内部持久事务完成；外部bytes读取不得进入锁区间。
