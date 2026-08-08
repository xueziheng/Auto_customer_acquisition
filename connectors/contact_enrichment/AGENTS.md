# connectors/contact_enrichment/ —— 联系人补全（Phase 1）

## 能力范围

按公司域名查业务联系人（姓名、职位、业务邮箱线索）。实现 `domains/prospecting` 的 `EnrichmentProvider` Protocol。

## Phase 1 只接一家

不做多源瀑布——那是为不存在的第二家供应商提前抽象。Phase 2 接第二家时在上层加编排（按单价升序、命中即停），本 connector 不变。

## 密钥归属

`CONTACT_ENRICH_API_KEY_REF`。

## 合规

返回的每条联系人线索必须带来源说明，供 prospecting 域组装 LegalBasisRecord。**具名业务邮箱是个人数据**，即使 provider 说是公开的。

## 成本

按次/按命中计费，逐次上报。
