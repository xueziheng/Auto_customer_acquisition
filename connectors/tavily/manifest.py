"""Tavily 连接器注册元数据。"""

from connectors.base import ConnectorManifest

MANIFEST = ConnectorManifest(
    connector_id="tavily_search",
    capabilities=("web.search", "web.search.usage"),
    secret_refs=("TAVILY_API_KEY_REF",),
    rate_limit_note="只使用 Tavily basic；额度由后续 Gateway 依据 `/usage` 保守预留",
    compliance_note="只返回公开网页定位结果；证据必须由现有安全页面读取器快照生成",
)

__all__ = ("MANIFEST",)
