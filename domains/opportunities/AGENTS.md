# domains/opportunities/ —— 贸易机会域

## 职责

维护 Trade Opportunity：**整个系统优化的目标对象**。

设计稿第 45.10 说明了北极星指标：不优化「搜了多少次」「找到多少家公司」「发了多少邮件」，而优化「已验证需求 → 合格商机 → 可供货商机 → 报价商机 → 成交」这条链，以及每个机会的成本。本域负责其中「合格商机」之后的全部环节。

## 核心业务公式

一笔有价值的机会必须同时满足：

```text
可接触的客户 × 真实需求 × 能找到的供应 × 可接受的利润 × 能执行的团队
```

任何一项为零，整体为零。所以打分**先过硬门槛，再算分数**——某一项不满足时，其他项分数再高也没意义。

## 状态机

```text
qualified ──→ assigned ──→ contacted ──→ sourcing ──→ quoted ──→ negotiating ──→ won
                                                                                  └──→ lost
```

任何状态都可以转到 `lost`，**且必须带 `LossReason` 和 `died_at_state`**。

为什么 `died_at_state` 和 `LossReason` 一样重要：「价格太高」发生在 quoted 阶段和发生在 contacted 阶段是两个完全不同的问题。前者是报价能力问题，后者说明客户一开始就没有预算——该改的是筛选门槛，不是报价。

## 打分：Phase 1 用硬门槛加三因子

设计稿第八节给了九项权重（20/15/15/10/10/10/10/5/5）。**Phase 1 不用它**，原因见 `docs/architecture/09-scoring-and-feedback.md`：

- 那些权重没有数据支撑，是拍出来的
- 九因子无法 debug——「为什么这个是 67 分」回答不了，销售就不会信这个分数

Phase 1 的做法：

```text
硬门槛（任一不过直接淘汰，不进入打分）
  1  能联系上（有已验证的联系方式）
  2  证据等级达标（纯 Agent 行业推断不够格）
  3  预计订单额过底线
  4  不是禁售或高风险类别

三因子（过门槛后排序用）
  证据强度  +  预计价值  +  供应可得性
```

**必须把打分时的输入快照存下来**（`ScoreSnapshot`）。等有几十条成交数据后回测权重、再加因子——没有快照就只能重跑历史数据，而历史数据已经变了。

接口设计要保证以后加因子不改调用方。

## 人工接管

接管是这套自动化的收口。设计稿第 45.6 指出：Agent 找到高意向客户后员工一天不处理，前面全部自动化都白费。

**接管包必须自带全部上下文**，禁止只发「有个高意向客户，请处理」。清单见 `HandoffPacket`。

SLA 指标：从 `HandoffRequested` 到 `HandoffAccepted` 的等待时长、队列深度、最久等待。Phase 1 只度量和通知；Phase 2 接积分后据此自动降低探索类任务预算（挂载点见 `ROADMAP.md`）。

## 依赖白名单

```text
允许   shared.events, shared.schemas, shared.errors
禁止   任何其他 domains/*
禁止   任何外部 SDK
```

需要需求信息时，由上层（`workflows/`）把 `ValidatedNeedView` 传进来，本域不去 import `domains.demand`。

## 发布的事件

`OpportunityQualified`、`OpportunityLost`、`OpportunityWon`、`HandoffRequested`、`HandoffAccepted`、`HandoffQueueBacklogged`

## 订阅的事件

`NeedValidated`（评估是否创建机会）、`SourcingCaseCompleted`（推进到 quoted 前一步）、`QuoteApproved`（转 quoted）

## 禁止事项

- 不发通知（那是 `notification_gateway`）
- 不算成本（那是 `domains/costing`）
- 不允许 `lost` 状态没有 `LossReason`
- 不允许员工审批自己负责的机会的低利润报价（见 `domains/approvals`）

## Phase 1 范围

机会模型、状态机、硬门槛加三因子打分、打分快照、Loss Reason、接管包、SLA 度量全部要有。

不做：九因子加权、自动反压、机会价值预测。

通知受众专用get_notification_audience_target仅返回当前account_id，NOTIFICATION_AUDIENCE_READ只SYSTEM且notification_opportunity_id必须为单一精确机会。先授权/核ID再tenant仓储查询；旧get仍不允许SYSTEM。canonical通知投影随后重读Employee当前ownership/active受众，不用事件旧assigned_to，不冒用boss。
