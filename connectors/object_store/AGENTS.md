# connectors/object_store/ —— S3 / MinIO 传输适配器

## 能力边界

本目录只把 `put / get / delete` 对象操作适配成 `artifact_store.transport` 的
传输契约。这里不判断 Artifact 类型、MIME、租户、去重、幂等、保留期或业务权限。

## SDK 与凭证

- boto3 只能在本目录的 concrete adapter 中导入；`artifact_store/`、`infra/db/`、
  domains 与 workflows 均不得接触 SDK。
- 配置只保存 access/secret 的环境变量引用名。凭证原值只在 adapter 构造时由注入的
  resolver 解析，不进入参数 DTO、返回值、属性 repr、日志或异常。
- endpoint、bucket、object key 与底层 SDK 异常文本同样禁止进入日志、异常和 repr。

## 错误分类

- S3 `NoSuchKey / 404` 转成固定 typed blob-not-found 信号；
- 其他 SDK、认证、网络或流读取失败统一转成固定、可重试的 `TransientError`；
- connector 不解析数据库错误，也不实现重试、补偿或 winner 选择。

## Phase 1 配置与运行

- 唯一 concrete adapter 是 bucket-bound `S3ObjectBlobTransport`，支持 `put / get / delete`；
  不提供枚举、复制、公开 URL 或 bucket 管理。
- 配置必须显式提供 endpoint、bucket、region、两项 secret ref 与 Raw/Generated 大小上限；
  无默认 endpoint、bucket、凭证或生产回退值。
- 所有 boto3 调用在线程中执行。caller cancellation 必须等待底层调用完成再传播，保证
  Store 能确定是否需要补偿；SDK 原始异常不得穿过 connector 边界。
