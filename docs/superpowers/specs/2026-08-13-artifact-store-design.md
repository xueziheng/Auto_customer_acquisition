# Artifact Store 持久化设计

**状态：已批准**

**日期：2026-08-13**

**范围：Phase 1 原始资料与系统生成产物的不可变、租户隔离存储**

## 一、背景与目标

Campaign 自动序列的 `draft_content → send` 之间需要一个可跨进程重启恢复的
`draft_ref`。邮件主题与正文不能进入 workflow context、Outreach Message Attempt、
Tool Gateway ledger、outbox、审计或日志；否则会扩大客户内容的持久化与泄漏面。

仓库已有 `artifact_store.ArtifactStore` Protocol、MinIO 本地编排与 boto3 依赖，但没有
concrete store、元数据表或事务语义。同时，现有 `ArtifactStore` 明确只代表原始证据，
不能把 Agent 生成的邮件草稿伪装成 `email_raw`。数据库架构也分别列出
`raw_artifacts` 与 `artifacts`。

本设计建立两套逻辑 Store，共用一个受控 S3/MinIO 传输层：

```text
同一 S3 / MinIO 后端
├── RawArtifactStore
│   ├── raw_artifacts 元数据
│   └── 邮件原文、截图、PDF、网页快照等原始证据
└── GeneratedArtifactStore
    ├── artifacts 元数据
    └── email_draft 等系统生成产物
```

完成后，Campaign 工作流只在 `run.context["draft_ref"]` 保存 `ArtifactId`；真正内容由
`GeneratedArtifactStore` 持久化和校验。后续回复原文、员工上传、网页快照复用
`RawArtifactStore`，不再建立竞争接口。

## 二、非目标

- 不在本切片实现 Campaign 工作流、Outreach Agent 或回复识别。
- 不提供 update、delete、跨租户 list 或公共运维后门。
- 不实现生命周期归档、对象复制、多区域、CDN、全文检索或语义检索。
- 不把 subject、body、原始邮件、附件内容或对象存储凭证写入 PostgreSQL。
- 不把派生产物当成 Provenance 的原始证据。
- 不在代码里提供存储大小、endpoint、bucket 或凭证默认值。

## 三、模块边界

### 3.1 公共契约

`artifact_store/` 只定义无凭证、无业务授权逻辑的接口：

- `RawArtifactStore`
- `GeneratedArtifactStore`
- `RawArtifactMeta`
- `GeneratedArtifactMeta`
- `RawArtifactKind`
- `GeneratedArtifactKind`
- `ObjectBlobTransport`（包内基础传输 Protocol）
- 固定的 artifact 错误类型

调用方只能提交 tenant、typed kind、bytes、MIME、安全资源绑定与幂等键。调用方不能
提交 endpoint、bucket、object key、access key 或 secret。

### 3.2 Concrete adapters

```text
artifact_store/
    store.py                 两个 Store Protocol、DTO 与 typed kind
    transport.py             ObjectBlobTransport Protocol
    errors.py                固定安全错误

infra/db/
    artifacts.py             ORM rows 与 tenant-scoped repositories
    artifact_uow.py          PostgreSQL 事务边界

infra/object_store/
    s3.py                    boto3 S3/MinIO adapter

migrations/versions/
    0014_artifact_store.py   raw_artifacts + artifacts
```

依赖方向固定为：

```text
apps / workflows → artifact_store Protocol
apps composition → infra concrete adapters
infra adapters → artifact_store + shared
```

workflows、domains 与 agent_runtime 不导入 boto3。S3 凭证只在 concrete adapter 内部
解析和持有；模型、Agent、DTO、返回值、日志和异常都不可见。

## 四、公共数据契约

### 4.1 原始资料

`RawArtifactMeta` 至少包含：

```text
tenant_id
artifact_id
kind
content_hash
size_bytes
mime_type
uploaded_by
uploaded_at
```

`RawArtifactKind` 只包含已批准的原始资料类别；它与
`GeneratedArtifactKind.EMAIL_DRAFT` 是不同 enum，构造时不能互换。

原始资料在同租户内按 `(kind, content_hash)` 去重。相同内容跨租户仍产生独立元数据与
对象，不允许借全局内容寻址推断其他租户是否拥有相同资料。

### 4.2 系统生成产物

`GeneratedArtifactMeta` 至少包含：

```text
tenant_id
artifact_id
kind
content_hash
size_bytes
mime_type
workflow_run_id
subject_ref
sequence_number
idempotency_key
generated_by
generated_at
```

Campaign 草稿使用：

```text
kind = email_draft
workflow_run_id = 当前 Workflow Run
subject_ref = EnrollmentId
sequence_number = Campaign step_number
idempotency_key = {enrollment_id}:{step_number}:draft
```

Artifact Store 不解析草稿 JSON，也不知道邮件业务规则。Campaign workflow 负责把
subject/body 编码成 canonical bytes，并在读取后解码；Store 只校验 bytes、MIME、哈希和
资源绑定。

同一 `(tenant_id, idempotency_key)`：

- 相同 kind、绑定、MIME、大小与 hash 返回既有 winner；
- 任一字段不同抛固定 `ArtifactConflictError`；
- 禁止覆盖、追加新版本或任选一条继续。

## 五、数据库模型

### 5.1 `raw_artifacts`

- 复合主键：`(tenant_id, artifact_id)`；
- 唯一约束：`(tenant_id, kind, content_hash)`；
- `content_hash`：lowercase 64-hex SHA-256；
- `size_bytes`：正整数且不超过显式配置上限；
- `object_key`：非空安全相对键，不含 URL、凭证、控制字符；
- `uploaded_at`：UTC `TIMESTAMPTZ`；
- 不含内容列或自由 JSON payload。

### 5.2 `artifacts`

- 复合主键：`(tenant_id, artifact_id)`；
- 唯一约束：`(tenant_id, idempotency_key)`；
- 保存 typed kind、hash、size、MIME、对象键和安全 producer/subject 绑定；
- `sequence_number` 为正整数；
- `generated_at` 为 UTC `TIMESTAMPTZ`；
- 不含 subject/body 或其他客户内容列。

两张表都由 tenant-bound repository 访问。公共 repository 不提供无 tenant 查询、list all、
update 或 delete。

## 六、配置与凭证

运行时必须显式提供：

```text
S3_ENDPOINT
S3_BUCKET_ARTIFACTS
S3_ACCESS_KEY_REF
S3_SECRET_KEY_REF
S3_REGION
RAW_ARTIFACT_MAX_BYTES
GENERATED_ARTIFACT_MAX_BYTES
```

规则：

- 生产 endpoint 必须为 HTTPS；
- 只有 `TRADEOS_DEV_MODE=true` 才允许带显式端口的 loopback HTTP；
- endpoint 禁止 username、password、query、fragment；
- bucket、region、密钥引用和两个大小上限均不得为空；
- 大小上限必须为严格正整数，拒绝 bool、float、NaN、无穷与隐式字符串容错；
- access/secret 环境值是密钥引用名，不是原值；
- `EnvironmentSecretResolver` 只把解析能力交给 S3 adapter；
- adapter 的 `repr`、日志与异常不包含 endpoint、bucket、object key、DSN 或凭证。

MIME 与 kind 的允许关系由代码中的 typed policy 固定；字节大小由显式部署配置控制，避免
在业务代码里藏运营阈值。

## 七、对象键与写入事务

对象键只能由服务端生成：

```text
raw/{tenant_id}/{artifact_id}
generated/{tenant_id}/{artifact_id}
```

调用方输入永远不参与路径拼接。`tenant_id` 与 `artifact_id` 必须先通过 typed ID 校验，
不允许 `/`、`..`、控制字符或 URL 形状。

### 7.1 Raw Artifact 写入

```text
put_raw
→ 验证 tenant / kind / MIME / size
→ 计算 SHA-256
→ 查询同租户 kind+hash
   ├── 已存在：返回既有 meta，不访问 S3
   └── 不存在：
       → 生成 art_ ULID
       → 上传 raw/{tenant}/{artifact_id}
       → INSERT raw_artifacts
       → commit
```

并发相同内容由数据库唯一约束决定 winner。loser 只删除自己随机 ID 对应的对象，再返回
winner；不能解析数据库异常文本决定冲突类型。

### 7.2 Generated Artifact 写入

```text
put_generated
→ 验证 tenant / kind / MIME / size / 安全绑定
→ 计算 SHA-256
→ 查询 tenant+idempotency_key
   ├── 相同 payload+绑定：返回既有 meta
   ├── 不同 payload/绑定：ArtifactConflictError
   └── 不存在：
       → 生成 art_ ULID
       → 上传 generated/{tenant}/{artifact_id}
       → INSERT artifacts
       → commit
```

S3 写完成后才可提交 PostgreSQL 元数据。S3 失败不写 metadata；数据库提交失败时只清理
本次随机 artifact ID 的对象。清理失败写固定安全告警，但不能覆盖 primary error。

同步 boto3 调用在受控线程执行。外部 cancellation 发生时，store 必须等待该次 S3 调用
达到确定结果，再执行必要补偿，最后原样传播 `CancelledError`，不能留下“可能写入”的
不确定状态。

## 八、读取与完整性

```text
get
→ tenant-scoped metadata lookup
→ 读取精确 object_key
→ 校验实际长度
→ 重新计算 SHA-256
→ 全部一致后才返回 bytes
```

跨租户、metadata 不存在与对象不存在统一抛固定 `ArtifactNotFoundError`，避免资源枚举。
长度或哈希不一致时不返回任何内容，抛 `ArtifactIntegrityError`，并向固定安全 logger 写
一次 CRITICAL。告警只含 tenant、artifact ID、typed kind 和固定 rule，不含对象键、正文或
底层异常文本。

## 九、权限与租户隔离

Artifact Store 是受信基础设施，不自行发明 Outreach、Conversation 或 Employee RBAC；
调用之前由对应 app/domain/workflow 完成 actor 授权。但 Store 自身必须独立保证：

- repository 构造时绑定 tenant；
- DTO tenant 与 bound tenant 完全一致；
- 每条 SQL 都含 tenant predicate；
- 跨租户读取表现为不存在；
- object key 只从已验证 tenant 和服务端 ID 派生；
- 不提供 cross-tenant list 或公共运维后门；
- 同 hash 跨 tenant 不能共享 metadata 或 object key。

## 十、错误分类

| 情况 | 对外错误 |
|---|---|
| 类型、MIME、大小、时间、绑定非法 | `ValidationError` |
| 幂等键异内容或异绑定 | `ArtifactConflictError` |
| 不存在或跨租户 | `ArtifactNotFoundError` |
| 哈希或长度不一致 | `ArtifactIntegrityError` |
| PostgreSQL/S3 暂时不可用 | 固定脱敏 `TransientError` |
| 凭证或配置非法 | 启动阶段固定 `PolicyViolation` |
| cancellation | 完成确定性补偿后原样传播 `CancelledError` |

任何错误都不得带 endpoint、bucket、object key、DSN、凭证、原始内容或底层异常文本。

## 十一、测试与验收

### 11.1 单元测试

- DTO frozen、类型与 UTC 校验；
- raw/generated typed kind 不能混用；
- MIME、大小、ID、幂等键边界；
- SHA-256 必须是 lowercase 64-hex；
- DTO/adapter `repr` 不含内容、endpoint、bucket、key 或凭证；
- 生产 HTTPS、dev loopback HTTP 与 URL 拒绝矩阵；
- primary error 不被 rollback/cleanup error 或 BaseException 覆盖；
- cancellation 等待确定性 S3 结果后再补偿并传播原对象。

### 11.2 真实 PostgreSQL + MinIO 集成测试

- `0014 → 0013 → 0014` migration roundtrip；
- ORM/schema parity 与数据库 CHECK/UNIQUE 真实约束；
- 同租户 raw 内容去重；
- generated 同键同内容幂等、同键异内容冲突；
- 20 并发只产生一个 winner；
- 同内容跨租户严格隔离；
- 跨租户读取与不存在相同；
- 对象损坏后读取固定失败并产生安全 CRITICAL；
- S3 失败不产生 metadata；
- DB commit failure 清理本次对象；
- cleanup failure 保留 primary error；
- 查询 PostgreSQL 证明没有持久化测试正文 marker。

### 11.3 演示与全门禁

新增 `scripts/demo_artifact_store.py`，用真实 PostgreSQL + MinIO 连续运行两次，证明幂等、
完整性与随机租户隔离。成功 stdout 只输出安全 ID、hash 和 count；无效 DSN/S3 配置只输出
固定中文失败消息。

每个实施任务完成后运行与其风险匹配的 focused tests，再运行：

```bash
make check
pytest tests/integration -q -W error
python3 scripts/check_boundaries.py
python3 scripts/scan_sensitive.py
git diff --check
```

迁移任务额外执行真实 upgrade → downgrade → upgrade；新文件最终必须为 Git mode
`100644`。每个独立小任务普通 commit、push 当前分支，并等待精确 commit SHA 的 GitHub
Actions 全部成功后才进入下一任务。

## 十二、实施拆分

1. Artifact contracts、严格配置与单元测试。
2. `0014` 迁移、ORM、tenant-scoped repositories/UoW。
3. S3/MinIO adapter、两个 concrete stores、补偿与并发测试。
4. 真实演示、运维文档和完整验收。

四项完成后再设计 Campaign 自动序列。届时 `draft_ref` 已有真实、持久、可校验且不泄密
的实现，不需要把正文塞进 workflow context 或建立临时内存旁路。
