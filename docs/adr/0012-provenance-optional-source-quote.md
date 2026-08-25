# ADR 0012：Provenance 可选保存逐字来源摘录

- 状态：已接受
- 日期：2026-08-25
- 修订：2026-08-26（回复候选 500 code point 持久边界与跨消息字段来源）

## 背景

回复分类可以从客户原话提取数量、规格、目的地等关键事实。既有 `Provenance` 只保存来源
类型、来源 ID、提取者与时间，能够定位消息，却不能在 Need 查询和人工接管时直接展示
「这个字段对应原文中的哪一段」。把逐字摘录单独塞进某个业务域会形成不兼容的平行
Provenance 结构；把整封正文复制到 workflow、outbox 或日志又违反原始资料隔离边界。

`Provenance` 是 shared 公共契约，新增字段必须同时说明历史数据兼容和持久化策略。

## 决策

在 shared `Provenance` 增加可选 `source_quote: str | None`。非 `None` 时必须为非空白
逐字摘录；它只随 tenant-bound 业务事实保存。客户完整正文仍只保存在 artifact store，
workflow context、事件、outbox 与日志不得携带 `source_quote` 或正文。

Conversation 来源的新回复字段保存经过输入原文逐字包含校验的 quote；模型候选 quote
还必须在 QualificationAgent 与 Conversation 的 durable candidate contract 两层都满足
**不超过 500 个 Unicode code point**。超长候选使整份模型输出 fail-closed，禁止截断后
落库。既有来源和无法提供可靠摘录的历史事实继续使用 `None`。

reply handoff writer 的摘录策略固定如下：

1. 上限为 **500 个 Unicode code point**，不是 500 bytes；
2. 按持久化 `candidate_fields` 的稳定顺序，先验证 quote 未超过上限且在重新读取正文中
   仍可精确找到，再只移除末尾空白；候选 quote 的前导空白属于已验证原文边界，必须保留。
   若防御层读到超长候选，整次 handoff fail-closed，不静默截断或跳过；
3. 没有可用 quote 时，从正文首个非空白字符开始取最多 500 个 code point；
4. 只允许截取和移除摘录末尾空白，不做摘要、拼接或同义改写。因此结果必须非空，并且是
   当前 artifact 原文的精确连续子串；
5. 同一摘录同时写入 handoff `customer_verbatim` 与 Provenance `source_quote`，完整正文只
   通过 `raw_artifact_ref` / `evidence_links` 在授权后重读。

Opportunity 首次创建时，Need 可能由多条已验证客户消息逐步补全。intake 必须按每个字段
自己的 `source_ref` 重读 tenant-bound、同企业的持久入站分类，并以该分类的
`classified_by` / `classified_at` 和该字段自己的 `source_quote` 构造 Provenance；不得把
所有字段伪装成来自触发本次 intake 的当前消息。若字段缺来源、缺逐字证据、找不到对应
持久分类或逐字证据不匹配，首次创建 fail-closed。若该 Need 已有唯一 Opportunity，后续
回复只幂等恢复负责人和 `qualified → assigned` 状态，不把历史 Need 字段重新解释为当前
消息字段，也不因此阻断当前消息的 handoff。

## 理由

可选公共字段让所有事实字段使用同一套来源结构，并能同时保留「定位原件」和「快速展示
相关逐字证据」两层能力。强制所有 Provenance 都带 quote 会使网页、API 和历史人工输入
无法兼容；仅保存整封正文则扩大敏感内容复制面。

500 code point 足以容纳人工判断数量、规格、报价/样品请求所需的局部上下文，同时把误把
邮件线程、签名档和历史往来复制进业务表的最坏暴露面固定在可审计上限。这里不做启发式
PII 涂黑：涂黑后的文本不再是原文精确子串，会削弱 Provenance 的可核验性，也容易因语言
和格式差异漏删。当前控制方式是最小化长度、tenant-bound 访问与通过 artifact 授权重读；
若法规或客户请求要求删除/脱敏，应由专门、留痕的 retention/redaction 流程同时处理业务
摘录和原件，而不是在 writer 中静默改写证据。

## 放弃的选项

- **只保存 message ID，不保存摘录。** 历史兼容最好，但人工审核每个字段都必须重新打开
  原件，无法直接证明提取值与原话的对应关系。
- **在 Demand 域自造 quote 字段。** 改动局部，但会让 Provenance 在各域发散，拒绝。
- **把完整回复正文写入业务行或 workflow context。** 重读方便，但扩大 PII 持久化面，
  违反数据平面分层，拒绝。

## 后果

这是向后兼容的 JSON/对象扩展：旧记录缺字段时读取为 `None`，不回填、不重写历史数据；
新 writer 可写 quote，旧 reader 若采用严格未知字段拒绝策略，必须先升级 reader 再升级
writer。Need 仓储序列化必须保留该字段，UI 仍需通过租户授权读取。

quote 不是新的原始资料副本，也不能替代 artifact 引用；它必须保持短摘录语义。500 code
point 是 Conversation reply candidate 及 reply handoff 的公共持久边界，不扩张为网页、
上传和历史人工输入等所有 `Provenance.source_quote` 的 shared 全局上限。调用方不能绕过
agent/domain contract；handoff 对遗留或损坏的超长候选再次 fail-closed。

摘录不建立独立保留期限：它与 tenant-bound handoff/Provenance 业务事实同生命周期；完整
消息则继续服从 artifact store 的保留与删除策略。任一侧到期都不构成把完整正文复制到另一
侧的理由。当前 schema 没有安全的独立摘录 TTL，因此不虚构天数；未来引入 retention job
时必须协调处理 handoff 字段、Provenance quote 与 artifact 引用，并保留不含原文的审计
事实。

## 何时重新审视

当 quote 造成业务行体积显著增长、出现删除/脱敏法规要求，或需要对摘录本身做不可变哈希
校验时重新审视；优先迁移到 tenant-bound evidence record 引用，不允许回退到 workflow/
outbox 复制正文。
