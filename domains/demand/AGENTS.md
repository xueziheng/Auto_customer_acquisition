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

`DemandSignalCaptured`、`NeedHypothesisCreated`、`NeedHypothesisRejected`、`NeedValidated`、`NeedBecameSourcingReady`、`NeedClusterFormed`、`NeedClusterMembershipChanged`

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

## Phase 2 公开研究证据

ResearchEvidence 是需求信号的不可变来源归属，不是新增 Lead。查询国家与真实所在地分离；
缺官网/所在地自述证据保留 pending_verification，域服务也禁止据此创建假设。研究公开
RFQ不算客户回复证据。旧信号没有 research_evidence 时保留历史解释，不补写证据。
明确所在地原文中的ISO2代码按完整已分配集合校验，不能受有限英文国名别名表限制；
代码有效不是国家合规授权，原政策门禁不变。

公开社交公司页、贸易索引、采购门户、会员/参展商简介均先视为第三方来源。
即使正文有“We are”与所在地，也不能把平台或展会域名绑定为客户官网；这类
独立信号保持pending_verification，不因资料不完整而要求先跑完其他来源才留存。

## Phase 2 客户数量单位事实

`NeedUnitService` 是独立人工确认入口；旧晋升/更新字段词表、完整度0–5、寻源门槛与研究/
Campaign解释均不变。旧Need的unit与绑定保持NULL，不从供应商单位猜测、不做箱件换算，
也不把单位确认解释为商业承诺审批（根硬边4/5/7）。

单位事实绑定当前数量的完整Provenance哈希；数量来源任一字段变化（即使数值相同）都会使
原单位失效，但不得覆盖旧receipt。确认/三列/旧历史表同事务；同键只读首次receipt，不
重写确认时间、不恢复stale单位。确认表只增，有数据时0042降级须拒绝并要求授权归档处理。

原始客户消息读取必须在全部Need/权限锁外，经注入Gateway来源端口核验租户、客户、原件、
hash、逐字摘录、定位与数量单位关系。权限check在来源IO前，guard保护当前员工/机会范围
直到Need事务提交；历史receipt和幂等重放另验当前来源阅读权，不借持有ID绕过消息ACL。
T3A受控reader/authorizer不注册生产，真实适配与HTTP/UI由后续装配验收。

新冻结消费者使用`NeedQuoteFacts`及公开纯函数，保留全部Provenance，不从展示摘要补造来源。
缺单位、未确认、绑定stale须阻断新冻结；不影响旧流程读取。

T3B将`NeedQuoteFacts`纯DTO迁至shared并保持原公开名称同class重导出；旧字节hash和错误码不变。
单位有效性/确认规则仍仅在demand，报价准备内部guard不能被解释为绕过T3A人工确认来源权限。

## Phase 2 安全单位准备读取

`assess_quote_preparation`仅本域分类正常缺项，并保留原数量/完整事实hash字节。
损坏typed事实必须facts_corrupt，不伪装待补单位；历史零数量仍有真实hash但不可报价。
HTTP仅输出白名单值及ProvenanceSummary，receipt原文/locator仍须独立来源ACL。
NeedUnitScopeReader只提供真实员工与Need/机会/account绑定；业务交集授权在上层组合，
其Employee→Opportunity SHARE不能提前锁Need，不要求owner、issuer或unit已存在。

## Sourcing V2 就绪事件

完整度是代码从事实字段推导的 0–5，不可人工抬高；`3` 是唯一的寻源门槛。首次晋升仍发布
`NeedValidated`；已验证 Need 在后续补全中第一次跨过该门槛时发布一次
`NeedBecameSourcingReady`。两个事件仅表达事实变化，不能按 NeedCluster 排序或暗示已经开始询价。
`model` 为可选 Need 事实，缺失不得由产品或网页补造。

## Phase 2 需求簇准入事实

`get_cluster_priority_facts` 只返回已核验的 Need ID、簇 ID、累计成员数与观察时间；不暴露数量或
账户字段，也不在此读取入口套用完整度 3 的寻源门槛。未归簇 Need 固定为一成员事实；归簇 Need 必须
同时满足簇→Need 和 Need→簇的双向成员链。每条 Need 首次归簇后，在同一事务发布
`NeedClusterMembershipChanged`；既有 `NeedClusterFormed` 仍仅表达第二成员首次形成多成员簇，二者均不
表示已经开始寻源、询价或报价。优先级事实的观察时间是底层版本：归簇后必须使用经双向成员链核验、
严格 UTC 且不早于创建时间的 `NeedCluster.updated_at`；未归簇时使用严格 UTC 的
`ValidatedNeed.created_at`。禁止用读取时钟制造新版本，也禁止在遗留时间缺失/非法时回退当前时间。

## Phase 2 目录提案需求事实

`recurring_requirement` 是客户明确表达的三态事实：`True`、`False` 与未知 `None` 不得合并，
并须沿用 `FactualField`/`Provenance` 的直接来源门禁。它不参与完整度 0–5、寻源或报价准备推导，
也不推进 Need 状态。quantity、unit 或 recurrence 真正变化后，业务事实、历史与
`NeedCatalogFactsChanged` 必须在同一租户事务提交；相同命令重放不得重复发布。事件只携带
Need/当前簇定位与变更种类，未归簇时 `cluster_id=None`，不得伪造单成员 Catalog 簇。

目录事实读取必须一次取得租户绑定的完整双向成员快照；簇→Need 与 Need→簇集合不相等、
成员租户不一致或成员产品类别不等于簇类别时失败关闭，且不得产出 `facts_hash`。未归簇 Need
不属于目录簇。

所有计数先按 account 聚合：同账户多条 Need 的数量覆盖视为未知，复购 True+False 只计一个
True 账户并输出固定 mixed display code。数量只纳入正整数、人工确认且仍绑定当前完整数量事实
哈希的单位；只做既有单位契约允许的空白/大小写规范化，不换算，混合单位不合计。

国家只接受 workflow 提供的完整非 Agent 证据与精确大写已分配 ISO-2。证据摘要仅携带安全
Provenance 元数据和绑定事实值的内容哈希，不得带值、原话、URL 内容或 reasoning。`facts_observed_at`
取所有参与持久事实的最新时间，不使用读取时钟；`facts_hash` 对稳定排序后的决策字段做 canonical
SHA-256，排除展示文案和读取时间。Demand 不读取 Catalog Policy，也不决定是否创建提案或 Product。
