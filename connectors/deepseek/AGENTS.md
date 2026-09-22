# DeepSeek 结构化模型 Connector

只做固定官方 Responses API 协议转换，遵守根硬边界 1、2、3、8、9。
只由 Tool Gateway 调用；无业务 prompt、数据库访问、工具执行、自动健康探测。
凭证按受信逻辑引用惰性解析，关闭 SDK 自动重试与跨站重定向。thinking 显式关闭；
只返回完成的最终 JSON 和实际 usage，不返回推理、原始异常或 HTTP 内容。
发送后未知保守归类，429 不意味着未计费。认证、余额、协议分别给固定分类。
