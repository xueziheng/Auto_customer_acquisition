"""DeepSeek 插件的安全能力声明。"""

from connectors.base import ConnectorManifest

MANIFEST = ConnectorManifest(
    connector_id="deepseek",
    capabilities=("model.generate",),
    secret_refs=("DEEPSEEK_API_KEY_REF",),
    rate_limit_note="租户及员工持久配额；无 SDK 自动重试",
    compliance_note="固定官方 HTTPS；只交付完成的结构化 JSON，无工具执行",
)
