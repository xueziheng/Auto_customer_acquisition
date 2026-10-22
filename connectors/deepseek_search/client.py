"""搜索凭证仅由可信连接器解析；结果不是客户需求或页面证据。"""

from __future__ import annotations

from typing import Protocol
from urllib.parse import urlsplit

from connectors.search_contracts import SearchCapabilities, SearchResult
from shared.errors import ValidationError

from .manifest import MANIFEST
from .transport import (
    DeepSeekSearchError,
    DeepSeekSearchPermanentError,
    SearchTransport,
    validate_request,
)


class SecretResolver(Protocol):
    def resolve(self, reference: str) -> str: ...


class DeepSeekSearchConnector:
    manifest = MANIFEST
    capabilities = SearchCapabilities(supports_country_boost=False, maximum_results=20)
    maximum_query_chars = 128

    def __init__(
        self, transport: SearchTransport, resolver: SecretResolver, *, base_url: str
    ) -> None:
        if base_url not in {"https://api.deepseek.com", "https://api.deepseek.com/v1"}:
            raise ValidationError("搜索要求官方平台连接")
        self._transport, self._resolver = transport, resolver

    def __repr__(self) -> str:
        return "DeepSeekSearchConnector()"

    async def search(
        self, query: str, *, country: str, limit: int
    ) -> tuple[SearchResult, ...]:
        validate_request(query, country, limit)
        try:
            key = self._resolver.resolve("MODEL_API_KEY")
        except Exception:  # noqa: BLE001 -- 凭证解析异常必须脱敏。
            raise DeepSeekSearchPermanentError("authentication") from None
        payload = await self._transport.search(query, country, limit, api_key=key)
        rows = payload.get("results")
        if not isinstance(rows, list):
            raise DeepSeekSearchError("unknown")
        results = []
        for row in rows[:limit]:
            if not isinstance(row, dict) or not all(
                isinstance(row.get(k), str) for k in ("title", "url", "snippet")
            ):
                raise DeepSeekSearchError("unknown")
            url = row["url"]
            try:
                parsed = urlsplit(url)
                if (
                    parsed.scheme not in {"http", "https"}
                    or not parsed.hostname
                    or parsed.username
                    or parsed.password
                ):
                    raise ValueError
                results.append(
                    SearchResult(
                        " ".join(row["title"].split())[:500],
                        url,
                        " ".join(row["snippet"].split())[:2000],
                    )
                )
            except (ValueError, ValidationError):
                raise DeepSeekSearchError("unknown") from None
        return tuple(results)
