# artifact_store/ —— 不可变资料与派生产物

## 职责

Artifact Store 是内容寻址的受信基础设施，公共契约严格拆成两类：

- `RawArtifactStore`：邮件原文、聊天截图、PDF、Word、Excel、网页快照、图片、音频；
- `GeneratedArtifactStore`：系统生成的派生产物，`email_draft`与Phase 2新增`quote_pdf`。

生成草稿不是客户原话，也不能作为原始证据或 Provenance 链终点。禁止恢复一个混合
Raw/Generated 的宽泛 `ArtifactStore` 接口。

## 硬边界

1. **不可变。** 公共接口没有 update、delete 或 cross-tenant list。合规删除属于独立、
   经审计的管理路径，不是业务 Store 能力。
2. **完整性。** 写入计算 SHA-256；每次读取重算长度与 hash，异常固定告警且不返回内容。
3. **租户隔离。** metadata 查询和 object key 都绑定 tenant；跨租户与不存在对外表现相同。
4. **内容不进元数据。** 数据库、日志、错误、repr、workflow context 与 outbox 不得包含
   原始 bytes、邮件 subject/body 或对象键。
5. **分层幂等。** Raw 在同租户按 `(kind, hash)` 去重；Generated 按 tenant+幂等键比较
   全部安全绑定，异内容或异绑定固定冲突，禁止覆盖 winner。

## 依赖与凭证归属

```text
允许：shared.*、本目录 Protocol
禁止：boto3 / SQLAlchemy、domains 内部、业务授权判断
```

S3/MinIO SDK、endpoint/bucket 传输和凭证解析只属于 `connectors/object_store/`。
PostgreSQL metadata 只由 `infra/db/` 实现。本目录只依赖两个窄 Protocol，不持有凭证。

## Phase 2 报价PDF边界

`quote_pdf`与邮件草稿的MIME/subject/key/模板规则互斥，原Raw.PDF仍是证据。
模板只读共享`shared.schemas.quote_files`注册集合；Store验证内容完整性和幂等绑定，
不判断run是否真实、报价是否获批、员工是否可使用客户文件。

仅QUOTE_PDF在object put尝试后的未知提交/关闭/传输错误或取消时保留candidate bytes。
非取消固定artifact_commit_unknown，不因rollback成功或暂时查不到metadata就删除。
只有已确认EXISTING loser可以清理自身未引用candidate；Raw/email旧补偿保持。
未知状态不得自动重试、换key或重新渲染；可能留下孤立bytes，本期无清扫器。

`get_meta_by_key`仅供受信Gateway恢复按原稳定键读取安全metadata，不访问对象。
None不证明未写入；实际读取仍必须`get`重算bytes hash与length。此窄接口不对HTTP开放。
当前正式生成/下载、历史用途授权和全部bytes Gateway编排由T8装配，本任务未提供许可。

## Phase 1 范围

真实 PostgreSQL metadata、S3/MinIO bytes、租户隔离、不可变幂等与完整性校验。内容理解、
生命周期归档、公开删除和跨租户运维接口均不在本阶段。

## 持久化与验收

- PostgreSQL 只保存安全 metadata；`raw_artifacts` 与 `artifacts` 均禁止内容、邮件正文、
  endpoint、bucket 或 credential 列。
- bytes 只能由 `ObjectBlobTransport` 进入 tenant-bound key。每次读取必须对数据库记录的
  长度与 SHA-256 重新校验，不能信任对象存储返回值。
- `scripts/demo_artifact_store.py` 是离线验收 composition，不是 production runtime factory；
  它只能通过注入的 settings、secret resolver、UoW 与 Store 公开接口运行。
