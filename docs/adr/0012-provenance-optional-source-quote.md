# ADR 0012：Provenance 可选保存逐字来源摘录

- 状态：已接受
- 日期：2026-08-25

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

Conversation 来源的新回复字段保存经过输入原文逐字包含校验的 quote；既有来源和无法提供
可靠摘录的历史事实继续使用 `None`。

## 理由

可选公共字段让所有事实字段使用同一套来源结构，并能同时保留「定位原件」和「快速展示
相关逐字证据」两层能力。强制所有 Provenance 都带 quote 会使网页、API 和历史人工输入
无法兼容；仅保存整封正文则扩大敏感内容复制面。

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

quote 不是新的原始资料副本，也不能替代 artifact 引用；它必须保持短摘录语义。未来如需
长度上限或脱敏策略，应在公共契约中统一增加，而不是由各域自行截断。

## 何时重新审视

当 quote 造成业务行体积显著增长、出现删除/脱敏法规要求，或需要对摘录本身做不可变哈希
校验时重新审视；优先迁移到 tenant-bound evidence record 引用，不允许回退到 workflow/
outbox 复制正文。
