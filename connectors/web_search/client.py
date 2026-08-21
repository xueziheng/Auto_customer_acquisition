"""Brave 搜索与公开页面不可变快照连接器。"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Protocol, runtime_checkable

from artifact_store.store import RawArtifactKind, RawArtifactStore
from shared.errors import ValidationError
from shared.schemas.identifiers import ArtifactId, TenantId, UserId

from .manifest import MANIFEST
from .transport import BraveSearchTransport, PublicPageTransport

_COUNTRY = re.compile(r"[A-Z]{2}")
_ARTIFACT = re.compile(r"art_[0-7][0-9A-HJKMNP-TV-Z]{25}")
_MAX_TEXT_CHARACTERS = 200_000


@runtime_checkable
class WebSearchSecretResolver(Protocol):
    def resolve(self, secret_ref: str) -> str: ...


@dataclass(frozen=True, repr=False)
class WebSearchResult:
    title: str
    url: str
    description: str = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not _safe_text(self.title, 500)
            or not _safe_text(self.url, 2_048)
            or not _safe_text(self.description, 2_000, allow_empty=True)
        ):
            raise ValidationError("公开搜索结果无效")


@dataclass(frozen=True, repr=False)
class PageSnapshot:
    text: str = field(repr=False)
    url: str
    observed_at: datetime
    content_hash: str
    snapshot_artifact_ref: ArtifactId

    def __post_init__(self) -> None:
        if (
            not _safe_content(self.text, _MAX_TEXT_CHARACTERS)
            or not _safe_text(self.url, 2_048)
            or not isinstance(self.observed_at, datetime)
            or self.observed_at.tzinfo is not UTC
            or re.fullmatch(r"[0-9a-f]{64}", self.content_hash) is None
            or not isinstance(self.snapshot_artifact_ref, str)
            or _ARTIFACT.fullmatch(self.snapshot_artifact_ref) is None
        ):
            raise ValidationError("公开页面快照无效")


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._hidden_depth = 0
        self._parts: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        del attrs
        if tag.casefold() in {"script", "style", "noscript", "template"}:
            self._hidden_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if (
            tag.casefold() in {"script", "style", "noscript", "template"}
            and self._hidden_depth > 0
        ):
            self._hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._hidden_depth == 0:
            value = " ".join(data.split())
            if value:
                self._parts.append(value)

    def text(self) -> str:
        return "\n".join(self._parts)[:_MAX_TEXT_CHARACTERS]


class WebSearchConnector:
    """外部内容只经 typed DTO 返回；原 HTML 立即写不可变 Artifact。"""

    manifest = MANIFEST

    def __init__(
        self,
        search_transport: BraveSearchTransport,
        page_transport: PublicPageTransport,
        artifacts: RawArtifactStore,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if (
            not isinstance(search_transport, BraveSearchTransport)
            or not isinstance(page_transport, PublicPageTransport)
            or not isinstance(artifacts, RawArtifactStore)
            or not callable(now)
        ):
            raise ValidationError("公开搜索连接器依赖无效")
        self._search_transport = search_transport
        self._page_transport = page_transport
        self._artifacts = artifacts
        self._now = now
        self._api_key: str | None = None

    def __repr__(self) -> str:
        return "WebSearchConnector()"

    async def configure(self, secret_resolver: WebSearchSecretResolver) -> None:
        if not isinstance(secret_resolver, WebSearchSecretResolver):
            raise ValidationError("公开搜索凭证解析器无效")
        failed = False
        value: object = None
        try:
            value = secret_resolver.resolve("WEB_SEARCH_API_KEY_REF")
        except Exception:  # noqa: BLE001 - 凭证边界必须脱敏
            failed = True
        if (
            failed
            or not isinstance(value, str)
            or len(value) < 16
            or value != value.strip()
            or any(ord(character) < 33 or ord(character) == 127 for character in value)
        ):
            raise ValidationError("公开搜索凭证配置无效")
        self._api_key = value

    async def health_check(self) -> bool:
        return self._api_key is not None

    async def search(
        self,
        query: str,
        *,
        country: str,
        limit: int,
    ) -> tuple[WebSearchResult, ...]:
        if self._api_key is None:
            raise ValidationError("公开搜索连接器尚未配置")
        if (
            not _safe_text(query, 400)
            or len(query.split()) > 50
            or not isinstance(country, str)
            or _COUNTRY.fullmatch(country) is None
            or type(limit) is not int
            or not 1 <= limit <= 20
        ):
            raise ValidationError("公开搜索参数无效")
        response = await self._search_transport.search(
            query, country, limit, api_key=self._api_key
        )
        web = response.payload.get("web")
        raw_results = web.get("results") if isinstance(web, dict) else None
        if not isinstance(raw_results, list):
            raise ValidationError("公开搜索响应无效")
        results: list[WebSearchResult] = []
        for item in raw_results[:limit]:
            if not isinstance(item, dict):
                raise ValidationError("公开搜索响应无效")
            title = item.get("title")
            url = item.get("url")
            description = item.get("description", "")
            if (
                not isinstance(title, str)
                or not isinstance(url, str)
                or not isinstance(description, str)
            ):
                raise ValidationError("公开搜索响应无效")
            canonical_url = await self._page_transport.validate_url(url)
            results.append(WebSearchResult(title, canonical_url, description))
        return tuple(results)

    async def read_page(
        self,
        tenant_id: TenantId,
        url: str,
        *,
        uploaded_by: UserId | None = None,
    ) -> PageSnapshot:
        if not isinstance(tenant_id, str) or not tenant_id:
            raise ValidationError("公开页面租户无效")
        observed_at = self._now()
        if not isinstance(observed_at, datetime) or observed_at.tzinfo is not UTC:
            raise ValidationError("公开页面观察时间无效")
        response = await self._page_transport.fetch(url)
        content_hash = hashlib.sha256(response.body).hexdigest()
        meta = await self._artifacts.put(
            tenant_id,
            RawArtifactKind.WEB_SNAPSHOT,
            response.body,
            "text/html",
            uploaded_by,
        )
        if meta.tenant_id != tenant_id or meta.content_hash != content_hash:
            raise ValidationError("公开页面 Artifact 绑定无效")
        parser = _VisibleTextParser()
        try:
            parser.feed(response.body.decode("utf-8", errors="replace"))
            parser.close()
        except Exception:  # noqa: BLE001 - 任意异常都不得泄漏页面内容
            raise ValidationError("公开页面正文解析失败") from None
        text = parser.text()
        if not text:
            raise ValidationError("公开页面没有可用正文")
        return PageSnapshot(
            text,
            response.url,
            observed_at,
            content_hash,
            meta.artifact_id,
        )


def _safe_text(value: object, maximum: int, *, allow_empty: bool = False) -> bool:
    return (
        isinstance(value, str)
        and (allow_empty or bool(value))
        and len(value) <= maximum
        and value == value.strip()
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


def _safe_content(value: object, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= maximum
        and all(
            ord(character) >= 32 or character in {"\n", "\t"}
            for character in value
        )
        and "\x7f" not in value
    )


__all__ = (
    "PageSnapshot",
    "WebSearchConnector",
    "WebSearchResult",
    "WebSearchSecretResolver",
)
