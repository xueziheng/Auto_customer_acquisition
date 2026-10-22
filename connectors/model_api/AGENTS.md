# 可配置模型连接器

只做 Responses / Chat Completions 协议转换；通过 Tool Gateway 调用。
密钥惰性解析，不交给 Codex 子进程，不输出异常原文，不跟随重定向，不自动重试。
只允许明确列出的官方 HTTPS 入口；只交付最终 JSON 和实际用量。
