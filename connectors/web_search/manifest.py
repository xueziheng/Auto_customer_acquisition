"""公开搜索连接器注册元数据。"""

from connectors.base import ConnectorManifest

MANIFEST = ConnectorManifest(
    connector_id="brave_web_search",
    capabilities=("web.search", "web.read_page"),
    secret_refs=("WEB_SEARCH_API_KEY_REF",),
    rate_limit_note="Brave Search 配额由 Tool Gateway 的部署策略显式限制",
    compliance_note="只搜索和读取公开 HTTP(S) 页面；页面必须生成不可变快照",
)

__all__ = ("MANIFEST",)
