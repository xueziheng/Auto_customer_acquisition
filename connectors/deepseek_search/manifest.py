"""真实独立搜索能力；不宣称免费。"""

from connectors.base import ConnectorManifest

MANIFEST = ConnectorManifest(
    connector_id="deepseek_search",
    capabilities=("web.search",),
    secret_refs=("MODEL_API_KEY",),
    rate_limit_note="固定官方Search；逐次持久预留，未知结果不重试",
    compliance_note="公开条件检索，摘要仅定位；页面证据另经原Gateway归档",
)
