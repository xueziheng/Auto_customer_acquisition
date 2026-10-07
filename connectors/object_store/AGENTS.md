# connectors/object_store/ —— S3 / MinIO 传输适配器

## 能力边界

本目录只把 `put / get / delete` 对象操作适配成 `artifact_store.transport` 的
传输契约。这里不判断 Artifact 类型、MIME、租户、去重、幂等、保留期或业务权限。

## SDK 与凭证

- boto3 只能在本目录的 concrete adapter 中导入；`artifact_store/`、`infra/db/`、
  domains 与 workflows 均不得接触 SDK。
- 配置只保存 access/secret 的环境变量引用名。凭证原值只在 adapter 构造时由注入的
  resolver 解析（此为旧S3ObjectBlobTransport行为；新增bounded reader/报价writer在执行期解析），
  不进入参数 DTO、返回值、属性 repr、日志或异常。
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

旧上传可用DeferredS3ObjectBlobTransport延迟创建上述delegate：构造零resolver/SDK，
先按旧规则检查key/bytes，首个合法操作（含并发）才初始化一次；失败不缓存坏delegate。
这不是新的IO实现，旧无界get、线程取消收口与Raw/EMAIL_DRAFT补偿原样保留。
新来源只走bounded，新QUOTE_PDF只走专属writer，不能借惰性wrapper绕过预算。

## 来源有界读取增量

`S3BoundedObjectBlobTransport`只提供来源专用get_bounded，与旧adapter/取消补偿互不改变。
构造仅保存settings与resolver，执行阶段才解析凭证并创建专用client；该操作须由Gateway
完成授权与EXECUTING提交后触发。显式connect/read/total deadline、有限attempts及有限read(n)，
累计最多上限加一哨兵，忽略不可信ContentLength。typed BlobReadLimitExceeded是新增非重试
例外，必须保留，不转换为TransientError。其他SDK错误仍按旧固定分类。
取消置每调用stop标记，有限SDK等待返回后停止流并关闭body/client，线程收口后才传播取消。
不声称Python可以强杀SDK线程；连接/读取timeout和有限attempts是阻塞等待边界。

## 报价PDF单次writer增量

`S3QuotePdfObjectBlobTransport`构造仅保存settings/resolver/显式QuotePdfWriteLimits，零取密钥、
零SDK初始化；每次put/delete执行期才创建专用client，须在Gateway授权及EXECUTING提交后调用。
只允许有限connect/read/total与attempts=1的单次SDK请求，无multipart/自动重试；get固定
read_unsupported，实际下载只经独立bounded reader。开始对象写后的SDK/close/deadline未知
固定outcome_unknown，不把取消当作未写；线程收口后传播取消。adapter不判winner、不删除
未知candidate、不改metadata补偿规则，安全错误不含endpoint/bucket/key/SDK原文。见ADR0021。

## 持久本机内测

仅真实本机 `pilot` 入口可显式调用 `S3ObjectStoreSettings.from_pilot_environ`，
保留 `dev_mode=False`，只接受带显式端口的精确 `http://127.0.0.1` endpoint。
此例外仅用于本 profile 对象存储，不开放远端 HTTP、任意 HTTPS 或开发身份断言；
默认 `from_environ` 的全部校验保持不变。原 Gateway/凭证/预算/传输契约不变。
