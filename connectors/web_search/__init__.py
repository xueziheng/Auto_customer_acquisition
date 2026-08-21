"""公开搜索 connector 插件。"""

from .client import (
    PageSnapshot,
    WebSearchConnector,
    WebSearchResult,
    WebSearchSecretResolver,
)
from .manifest import MANIFEST
from .transport import (
    BraveSearchApiTransport,
    BraveSearchTransport,
    PublicPageRejectedError,
    PublicPageResponse,
    PublicPageTransport,
    SafePublicPageHttpTransport,
    SearchHttpResponse,
    WebSearchAuthRequiredError,
    WebSearchProviderError,
    WebSearchRateLimitedError,
)

__all__ = (
    "MANIFEST",
    "BraveSearchApiTransport",
    "BraveSearchTransport",
    "PageSnapshot",
    "PublicPageRejectedError",
    "PublicPageResponse",
    "PublicPageTransport",
    "SafePublicPageHttpTransport",
    "SearchHttpResponse",
    "WebSearchAuthRequiredError",
    "WebSearchConnector",
    "WebSearchProviderError",
    "WebSearchRateLimitedError",
    "WebSearchResult",
    "WebSearchSecretResolver",
)
