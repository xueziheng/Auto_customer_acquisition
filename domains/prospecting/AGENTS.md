# domains/prospecting/ —— 潜客域（浅）

## 职责

潜在企业、潜在联系人、联系方式，以及两个硬性机制：

**可达性验证门禁（硬边界 6）**——联系方式只有 `VERIFIED` 状态才能进序列。未验证地址产生硬退信，硬退信毁发件域名信誉。这条门禁拦的是连带损害。

**法律依据留痕**——每个联系人记录处理依据。GDPR 下 B2B 冷触达常靠 legitimate interest，但**依赖它的前提是能拿出评估记录，不是假设自己有**。监管问询时「我们以为可以」不是答案。这些字段 Phase 1 就要有：事后补要逐个联系人追溯来源，成本极高。

## 联系人补全：单 Provider，不做瀑布

Phase 1 只接一家数据源，所以只定义一个 `EnrichmentProvider` Protocol。**不要现在建多源瀑布编排**——为不存在的第二家供应商提前抽象，是给自己造维护负担。Phase 2 有第二家时再加瀑布（按单价升序、命中即停），Protocol 不用改。

每次补全记录成本——「每个已验证联系人花了多少」是评估数据源的依据。

## 企业消歧

Demand Signal 里的 `entity_name` 是原始文本。同一家公司会以「Acme Mfg」「Acme Manufacturing Inc.」两种写法出现，不消歧就会创建两个 account、被分给两个员工、收到两套邮件。消歧靠域名优先（网站域名相同即同一企业），名称相似度只做辅助。

## 依赖白名单

```text
允许   shared.*        禁止   其他 domains/*、外部 SDK
```

验证与补全的实际 API 调用在 `connectors/`（经 tool_gateway），结果传入本域。

## 事件

发布：`ContactPointVerified`、`ProspectAccountQualified`
订阅：无

## Phase 1 范围

企业/联系人/联系方式模型、验证状态机、法律依据记录、单 Provider 接口、消歧。不做：多源瀑布、社交渠道联系方式、自动组织架构挖掘。
