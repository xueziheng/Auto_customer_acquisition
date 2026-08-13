# artifact_store/ —— 不可变资料与派生产物

## 职责

Artifact Store 是内容寻址的受信基础设施，公共契约严格拆成两类：

- `RawArtifactStore`：邮件原文、聊天截图、PDF、Word、Excel、网页快照、图片、音频；
- `GeneratedArtifactStore`：系统生成的派生产物，Phase 1 当前只有 `email_draft`。

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

## Phase 1 范围

真实 PostgreSQL metadata、S3/MinIO bytes、租户隔离、不可变幂等与完整性校验。内容理解、
生命周期归档、公开删除和跨租户运维接口均不在本阶段。
