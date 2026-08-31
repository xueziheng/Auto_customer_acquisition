"""共享公开页面 URL 契约的 hostile-host 回归。"""

from __future__ import annotations

import pytest

from shared.public_page_url import canonical_public_page_url
from tests.public_page_url_fixtures import HOSTILE_PUBLIC_PAGE_URLS


@pytest.mark.parametrize(
    "url",
    HOSTILE_PUBLIC_PAGE_URLS,
)
def test_shared_canonicalizer_rejects_task9_hostile_or_special_targets(
    url: str,
) -> None:
    """若删掉 legacy/special-host 门禁，持久化网页证据会重新接受 SSRF 目标。"""

    with pytest.raises(ValueError, match="invalid public page URL"):
        canonical_public_page_url(url)


@pytest.mark.parametrize(
    ("url", "canonical"),
    (
        (
            "https://Supplier.Example:443/products/hinge",
            "https://supplier.example/products/hinge",
        ),
        (
            "http://supplier.example:80",
            "http://supplier.example/",
        ),
        (
            "https://Supplier.Example./products/hinge",
            "https://supplier.example/products/hinge",
        ),
    ),
)
def test_shared_canonicalizer_keeps_ordinary_dns_shape_valid(
    url: str, canonical: str
) -> None:
    assert canonical_public_page_url(url) == canonical
