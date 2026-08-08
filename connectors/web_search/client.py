"""公开搜索连接器骨架。"""

from __future__ import annotations

from typing import Any

from connectors.base import ConnectorManifest

MANIFEST = ConnectorManifest(
    connector_id="web_search",
    capabilities=("web.search", "web.read_page"),
    secret_refs=("WEB_SEARCH_API_KEY_REF",),
)


class WebSearchConnector:
    manifest = MANIFEST

    async def configure(self, secret_resolver: Any) -> None:
        raise NotImplementedError

    async def health_check(self) -> bool:
        raise NotImplementedError

    async def search(self, query: str, country: str | None = None) -> list[dict]:
        raise NotImplementedError

    async def read_page(self, url: str) -> dict:
        """返回 {text, url, observed_at, content_hash, snapshot_artifact_ref}。
        四件套缺一不可——下游所有证据链依赖它们。"""
        raise NotImplementedError
