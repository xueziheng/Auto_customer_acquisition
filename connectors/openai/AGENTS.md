# connectors/openai/ —— 结构化模型调用（Phase 1）

## 能力范围

只把统一的结构化 JSON 模型请求转换为 OpenAI Responses API 调用。这里不放业务
prompt、不决定业务动作、不解析业务字段，也不直接写库。

## 凭证与日志

- 只接受密钥逻辑引用，密钥值由 connector 内部惰性解析并直接交给 SDK。
- 输入正文、响应正文、密钥和 Provider 原始异常都不得写入日志或异常消息。
- 认证/参数错误不可重试；网络、限流和 5xx 分类为可重试错误。

## 调用边界

只允许 worker/composition root 构造本连接器，再以
`agent_runtime.model_client.StructuredJsonModelClient` 窄端口注入能力。
能力代码不得 import 本目录。
