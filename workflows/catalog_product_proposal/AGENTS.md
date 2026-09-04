# workflows/catalog_product_proposal/ —— 目录产品提案编排

## 职责

本目录只负责把各域公开服务的窄事实连接起来。Demand 拥有已核验需求簇事实与聚合规则；
后续 Catalog Policy、提案生命周期和 Products 写入不得进入账户事实适配器。

## 依赖与数据边界

- 只允许导入 `domains/*/service.py` 的公开契约与 `shared.*`；禁止导入任何域的 models、
  schemas 或 repository。
- Prospecting 适配器只调用 `ProspectingService.get_account`，逐字段映射，不使用
  `model_dump()`、`asdict()` 或对象递归复制。
- 国家只接受带完整非 Agent Provenance 的、精确大写且已分配的 ISO-2；不做别名、TLD、
  搜索国家、网站或模型推断，其他输入统一为未知。
- 跨域依赖异常只输出固定可重试/不可重试错误，不保留原异常文本或 context。

## 禁止事项

- 不读取 Catalog Policy，不决定是否提案，不创建或更新 Product。
- 不保存事实值以外的原始 Provenance 正文、URL 内容、客户消息或模型 reasoning。
- 不接触价格、报价、联系人、发送、凭证、DSN 或任何外部副作用。
- 所有调用必须显式携带 `tenant_id`，返回 tenant/account 不一致时失败关闭。
