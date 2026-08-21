# connectors/hunter/ —— Hunter Provider 适配器

## 能力范围

Phase 1 的联系人补全与邮箱验证只允许使用 Hunter API v2。这个目录只负责固定协议转换，
不决定何时发现联系人、何时验证、是否允许触达，也不直接写业务表。

## 凭证与 PII

- 唯一密钥引用名是 `HUNTER_API_KEY_REF`；运行时由本 connector 内部解析。
- API Key 只进入固定主机请求的 `X-API-KEY` header，不得进入 URL、参数、DTO、repr、
  异常、日志、数据库或模型上下文。
- 邮箱、姓名、职位、source URI 和 Provider 原始 JSON 只允许存在于 `repr=False` 的 typed
  DTO 与同一受信调用栈内；不得持久化 Provider raw response。
- `confidence`、`score`、`decision_maker` 属于 Provider 推断，禁止进入公共 DTO、审计、
  outbox 或业务表。

## HTTP 边界

- 生产主机固定为 `https://api.hunter.io/v2`，调用方不得覆盖 host，不接受绝对 URL。
- 只允许 `/account`、`/domain-search`、`/email-verifier` 三个 GET endpoint；禁止重定向。
- 单次请求超时不超过 30 秒，响应最多 512 KiB，只接受 JSON object。
- Provider error body、完整 URL、query 和底层网络异常全部在本目录内丢弃；只向上返回固定
  typed 分类和合法的 1–3600 秒 `Retry-After`。
- `451 claimed_email` 是唯一可透出的 Provider error id；其他 id 固定归为 `other`。

## 成本与缓存

不在代码中硬编码 Hunter 套餐价格，只使用总纲规定的固定 cost label。四种邮箱验证结果均按
30 天缓存；`risky`、`invalid`、`unverified` 不能因失败而绕过缓存再次付费。

