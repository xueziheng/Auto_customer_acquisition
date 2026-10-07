# connectors/contact_enrichment/ —— 联系人补全（Phase 1）

## 能力范围

按公司域名查业务联系人（姓名、职位、业务邮箱线索），对外只暴露
provider-neutral frozen typed DTO/Protocol，不返回 raw dict。

## Phase 1 只接一家

不做多源瀑布——那是为不存在的第二家供应商提前抽象。Phase 2 接第二家时在上层加编排（按单价升序、命中即停），本 connector 不变。

## Provider 与密钥归属

Phase 1 只允许 Hunter，实现只解析 `HUNTER_API_KEY_REF`。密钥不得进入参数、DTO、
异常、日志、数据库或模型上下文；generic contract 不注册独立 manifest。

## 合规

返回的每条联系人线索必须带来源说明，供 prospecting 域组装 LegalBasisRecord。**具名业务邮箱是个人数据**，即使 provider 说是公开的。邮箱、姓名、职位与来源 URI
在 DTO 中全部 repr-disabled；Provider 的 `confidence`、`score`、`decision_maker`
不得进入公共对象或持久化数据。

## 成本

按次/按命中计费，只上报固定 cost label，不保存当前套餐价格或 Provider 任意文本。
