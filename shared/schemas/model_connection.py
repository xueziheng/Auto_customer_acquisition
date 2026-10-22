"""模型入口的非秘密部署契约。"""
# 显式列出可接收模型凭证的官方入口；不能让编辑 URL 变成内网代理。
MODEL_ENDPOINTS = (
    "https://api.deepseek.com", "https://api.deepseek.com/v1",
    "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    "https://api.moonshot.cn/v1", "https://api.moonshot.ai/v1",
    "https://open.bigmodel.cn/api/paas/v4", "https://api.siliconflow.cn/v1",
    "https://api.siliconflow.com/v1", "https://api.openai.com/v1",
    "https://openrouter.ai/api/v1",
)

