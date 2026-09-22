# workflows/catalog_product_proposal/ —— 目录产品提案编排

## 职责

本目录只负责把各域公开服务的窄事实连接起来。Demand 拥有已核验需求簇事实与聚合规则；
Catalog Policy、评估、培养审批和 Products 写入只能进入专用编排文件，不得进入
`account_facts.py` 账户事实适配器。

## 依赖与数据边界

- 只允许导入 `domains/*/service.py` 的公开契约与 `shared.*`；禁止导入任何域的 models、
  schemas 或 repository。
- Prospecting 适配器只调用 `ProspectingService.get_account`，逐字段映射，不使用
  `model_dump()`、`asdict()` 或对象递归复制。
- 国家只接受带完整非 Agent Provenance 的、精确大写且已分配的 ISO-2；不做别名、TLD、
  搜索国家、网站或模型推断，其他输入统一为未知。
- 跨域依赖异常只输出固定可重试/不可重试错误，不保留原异常文本或 context。
- `application.py`、`evaluation_*`、`proposal_*` 和 `mapping.py` 只能通过 Demand、Products、
  Approvals 的 `service.py` 公共契约协作；域间 DTO 必须逐字段映射。
- durable context 与事件只保存 ID、hash 和固定状态码；每个步骤重读 canonical 事实，
  不把策略、规则、证据、Provenance 或需求事实正文写入 context。
- 培养审批必须先提交 Products 业务效果，成功后才写 Approval applied 收据；未知提交结果
  固定可重试，不能猜测成功或记录 apply-failed。

## 禁止事项

- `account_facts.py` 不读取 Catalog Policy、不决定是否提案，也不创建或更新 Product。
- 专用编排不得创建/晋升正式 Product，不得创建 Sourcing Case；只能调用 Products 公共服务
  记录确定性评估、提案审批状态和培养 Case。
- 不保存事实值以外的原始 Provenance 正文、URL 内容、客户消息或模型 reasoning。
- 不接触价格、报价、联系人、发送、凭证、DSN 或任何外部副作用。
- 所有调用必须显式携带 `tenant_id`，返回 tenant/account 不一致时失败关闭。
