# domains/demand/ —— 需求域

## 职责

维护需求的四层演进，并守住层与层之间的门槛。

```text
Demand Signal      需求信号：观察到的事实，不代表有人会买
      ↓
Need Hypothesis    需求假设：由信号推出的推断，仍不是事实
      ↓
Validated Need     已验证需求：客户本人确认过
      ↓
Need Cluster       需求簇：多条相似需求聚合
```

这是整个系统最重要的域。设计稿把「以需求为中心而不是以产品为中心」定为核心纠正，这个域就是那个纠正的落地。

## 最关键的一条约束

**Hypothesis → Validated 的门槛不可降低。**

只有以下情况能创建 `ValidatedNeed`：

```text
客户回复里说了
客户提交了表单
客户发来规格表
员工与客户沟通后确认
```

Agent 的推断再有道理都不行。**这是硬边界 5 在本域最重要的体现。**

为什么这条不能松：整个系统的商业价值取决于"已验证需求"这个数字是否可信。一旦允许推断直接晋升，这个数字会迅速膨胀成噪音，老板对系统的信任会一次性崩掉，而且很难恢复。宁可少算，不可多算。

## 状态机

**DemandSignal**

```text
captured ──→ linked_to_hypothesis
    └──────→ discarded          （噪音或不相关）
```

信号**不可变**：一旦 captured 就不改内容。观察到新情况是新信号，不是更新旧信号。这样才能回答"我们当时看到的是什么"。

**NeedHypothesis**

```text
inferred ──→ contacting ──→ validated    （客户确认，晋升为 ValidatedNeed）
    │            └────────→ rejected
    └──────────────────────→ rejected
```

`rejected` 必须带原因（用 `LossReason` 枚举）。哪类假设最容易被证伪，直接决定下一轮探索策略往哪调。

**ValidatedNeed**

```text
validated ──→ sourcing_ready ──→ handed_to_sourcing
    │                                    │
    └──→ withdrawn （客户取消）           └──→ fulfilled / lost
```

`validated → sourcing_ready` 需要完整度达到 3 级以上（数量明确）。信息不足就去寻源，会浪费寻源成本，还会拿到供应商无法报价的模糊询问。

## 需求完整度 0–5

```text
0  只有模糊兴趣
1  产品类别明确
2  用途或规格初步明确
3  数量明确               ← 寻源门槛
4  时间和目的地明确
5  具备寻源或报价条件
```

完整度**由字段推导，不手动设置**。手动设置会被乐观地填高，然后寻源门槛失效。

## 依赖白名单

```text
允许   shared.events, shared.schemas, shared.errors
禁止   任何其他 domains/*
禁止   任何外部 SDK（httpx、openai、playwright…）
```

## 发布的事件

`DemandSignalCaptured`、`NeedHypothesisCreated`、`NeedHypothesisRejected`、`NeedValidated`、`NeedClusterFormed`

## 订阅的事件

`ReplyReceived` —— 客户回复可能包含需求信息，触发假设晋升或需求补全。

## 禁止事项

- 不在本域发邮件、搜网页、调模型 —— 那是 `agent_runtime/` 和 `connectors/` 的事
- 不存 confidence 小数（硬边界 3），需要时用 `shared.schemas.evidence.derive_confidence` 现算
- 不允许没有 `EvidenceItem` 的 `NeedHypothesis`
- 不允许 `ValidatedNeed` 的来源是 `AGENT_INFERENCE`

## Phase 1 范围

四层模型、状态机、完整度推导、证据管理全部要有。

`NeedCluster` 只建模型和聚类接口，**不做聚类驱动的寻源优先级**——Phase 1 已验证需求只有个位数，聚不出东西。Phase 2 再接（见 `ROADMAP.md`）。
