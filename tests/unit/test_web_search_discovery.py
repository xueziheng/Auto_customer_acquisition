"""公开网页 SSRF/redirect 与 task-local opaque handle 验收。"""

from __future__ import annotations

import asyncio
import socket
from datetime import UTC, datetime

import pytest

from connectors.web_search.client import PageSnapshot, WebSearchResult
from connectors.web_search.transport import (
    PublicPageRejectedError,
    PublicPageRejectedReason,
    SafePublicPageHttpTransport,
    WebSearchRateLimitedError,
)
from shared.errors import TransientError, ValidationError
from shared.schemas.identifiers import ArtifactId, TenantId, new_id
from tests.public_page_url_fixtures import HOSTILE_PUBLIC_PAGE_URLS
from tool_gateway.errors import ToolErrorCategory
from tool_gateway.handlers.web_search import map_web_provider_error
from tool_gateway.handlers.web_slots import WebPageSnapshotSlot, WebSearchResultSlot

NOW = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("reason", "category"),
    (
        (
            PublicPageRejectedReason.PAGE_ACCESS_FORBIDDEN,
            ToolErrorCategory.PAGE_ACCESS_FORBIDDEN,
        ),
        (
            PublicPageRejectedReason.LOGIN_OR_CAPTCHA,
            ToolErrorCategory.LOGIN_OR_CAPTCHA,
        ),
        (
            PublicPageRejectedReason.UNSAFE_REDIRECT,
            ToolErrorCategory.UNSAFE_REDIRECT,
        ),
    ),
)
def test_public_page_rejection_reason_survives_gateway_mapping(
    reason: PublicPageRejectedReason, category: ToolErrorCategory
) -> None:
    error = PublicPageRejectedError(reason)

    mapped = map_web_provider_error(error)

    assert mapped.category is category
    assert reason.value not in repr(error)


@pytest.mark.parametrize(
    "body",
    [
        b"<html><title>Sign in required</title><form><input type='password'></form></html>",
        b"<html><title>Verify you are human</title><div class='g-recaptcha'></div></html>",
        b"<html><h1>Access denied</h1>Automated access is prohibited.</html>",
        b"<html><title>Account Portal</title><form><label>Username</label><input name='username'><input type='password'><button>Login</button></form></html>",
    ],
)
async def test_blocked_pages_never_become_public_snapshots(monkeypatch, body):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    transport = SafePublicPageHttpTransport()
    monkeypatch.setattr(
        transport,
        "_request_once",
        lambda parsed: (
            (404, None, "text/plain", None, b"")
            if parsed.path == "/robots.txt"
            else (200, None, "text/html", None, body)
        ),
    )
    with pytest.raises(PublicPageRejectedError):
        await transport.fetch("https://example.com/about")


@pytest.mark.parametrize(
    ("body", "reason"),
    (
        (
            b"<html><title>Sign in required</title><form><input type='password'></form></html>",
            PublicPageRejectedReason.LOGIN_OR_CAPTCHA,
        ),
        (
            b"<html><title>Verify you are human</title><div class='g-recaptcha'></div></html>",
            PublicPageRejectedReason.LOGIN_OR_CAPTCHA,
        ),
        (
            b"<html><h1>Access denied</h1>Automated access is prohibited.</html>",
            PublicPageRejectedReason.PAGE_ACCESS_FORBIDDEN,
        ),
    ),
)
async def test_blocked_page_kind_is_preserved_without_response_text(
    monkeypatch, body: bytes, reason: PublicPageRejectedReason
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    transport = SafePublicPageHttpTransport()
    monkeypatch.setattr(
        transport,
        "_request_once",
        lambda parsed: (
            (404, None, "text/plain", None, b"")
            if parsed.path == "/robots.txt"
            else (200, None, "text/html", None, body)
        ),
    )

    with pytest.raises(PublicPageRejectedError) as caught:
        await transport.fetch("https://example.com/about")

    assert caught.value.reason is reason
    assert body.decode() not in repr(caught.value)


async def test_robots_disallow_prevents_target_read(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    transport = SafePublicPageHttpTransport()
    paths = []

    def response(parsed):
        paths.append(parsed.path)
        if parsed.path == "/robots.txt":
            return 200, None, "text/plain", None, b"User-agent: *\nDisallow: /private\n"
        return 200, None, "text/html", None, b"<html>Public company</html>"

    monkeypatch.setattr(transport, "_request_once", response)
    with pytest.raises(PublicPageRejectedError):
        await transport.fetch("https://example.com/private")
    assert paths == ["/robots.txt"]


async def test_public_login_link_or_contact_captcha_is_not_a_login_wall(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    transport = SafePublicPageHttpTransport()
    body = b"<html><nav><a href='/login'>Login</a></nav><h1>Acme importer</h1><p>Our contact form uses CAPTCHA to prevent spam.</p><div class='g-recaptcha'></div></html>"
    monkeypatch.setattr(
        transport,
        "_request_once",
        lambda parsed: (
            (404, None, "text/plain", None, b"")
            if parsed.path == "/robots.txt"
            else (200, None, "text/html", None, body)
        ),
    )
    assert (await transport.fetch("https://example.com/about")).body == body


def test_public_company_prose_with_embedded_login_form_is_not_a_wall():
    from connectors.web_search.page_policy import is_restricted_page

    assert not is_restricted_page(
        b"<html><title>Acme importer</title><article>We are Acme Tools, an importer and distributor of high quality hinges. We are based in Germany and serve industrial customers.</article><form><input name='username'><input type='password'><button>Login</button></form></html>"
    )


@pytest.mark.parametrize(
    "status,body",
    [
        (403, b""),
        (451, b""),
        (200, b"<html>Login required</html>"),
    ],
)
async def test_robots_unavailable_or_not_a_rules_document_fails_closed(
    monkeypatch, status, body
):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    transport = SafePublicPageHttpTransport()
    paths = []

    def response(parsed):
        paths.append(parsed.path)
        return status, None, "text/plain", None, body

    monkeypatch.setattr(transport, "_request_once", response)
    with pytest.raises(PublicPageRejectedError):
        await transport.fetch("https://example.com/about")
    assert paths == ["/robots.txt"]


@pytest.mark.parametrize("robots", [False, True])
async def test_public_page_429_is_rate_limited(monkeypatch, robots: bool) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    transport = SafePublicPageHttpTransport()

    def response(parsed):
        if parsed.path == "/robots.txt" and not robots:
            return 404, None, "text/plain", None, b""
        return 429, None, "text/plain", None, b""

    monkeypatch.setattr(transport, "_request_once", response)
    with pytest.raises(WebSearchRateLimitedError):
        await transport.fetch("https://example.com/about")


@pytest.mark.parametrize("robots", [False, True])
async def test_public_page_5xx_is_transient(monkeypatch, robots: bool) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    transport = SafePublicPageHttpTransport()

    def response(parsed):
        if parsed.path == "/robots.txt" and not robots:
            return 404, None, "text/plain", None, b""
        return 503, None, "text/plain", None, b""

    monkeypatch.setattr(transport, "_request_once", response)
    with pytest.raises(TransientError):
        await transport.fetch("https://example.com/about")


async def test_public_cross_origin_redirect_cannot_escape_search_source(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    transport = SafePublicPageHttpTransport()
    requested = []

    def response(parsed):
        requested.append(parsed.hostname)
        if parsed.path == "/robots.txt":
            return 404, None, "text/plain", None, b""
        return 302, "https://other.example/about", None, None, None

    monkeypatch.setattr(transport, "_request_once", response)
    with pytest.raises(PublicPageRejectedError) as caught:
        await transport.fetch("https://example.com/about")
    assert caught.value.reason is PublicPageRejectedReason.UNSAFE_REDIRECT
    assert set(requested) == {"example.com"}


@pytest.mark.parametrize(
    "rules,path,allowed",
    [
        (
            "User-agent: *\nDisallow: /private\nAllow: /private/public",
            "/private/public",
            True,
        ),
        ("User-agent: *\nDisallow: /*?token=*\n", "/page?token=x", False),
        ("User-agent: TradeOS-Agent\nDisallow: /\nUser-agent: *\nAllow: /", "/", False),
        ("User-agent: *\nDisallow: /shop$\n", "/shop/products", True),
        ("User-agent: *\nCrawl-delay: 5\n", "/", False),
        ("User-agent: \nAllow: /\nUser-agent: *\nDisallow: /", "/", False),
        (
            "User-agent: *\nDisallow: /private/\nAllow: /private%2Fpublic",
            "/private/public",
            False,
        ),
        (
            "User-agent: *\nDisallow: /private/\nAllow: /private%2fpublic",
            "/private%2Fpublic",
            True,
        ),
        (
            "User-agent: *\nDisallow: /private/\nAllow: /private/%70ublic",
            "/private/public",
            True,
        ),
        (
            "User-agent: *\nDisallow: /private/\nAllow: /private/%2A",
            "/private/secret",
            False,
        ),
        (
            "User-agent: *\nDisallow: /private/\nAllow: /private/public%24",
            "/private/public",
            False,
        ),
        (
            "User-agent: *\nDisallow: /private/\nAllow: /private/%FF",
            "/private/secret",
            False,
        ),
        ("User-agent: *\nDisallow: /private/\nAllow: /private/%GG", "/public", False),
        ("User-agent: *\nDisallow: /private/", "/public%", False),
    ],
)
def test_robots_specific_groups_and_path_restrictions(rules, path, allowed):
    from connectors.web_search.page_policy import robots_allows

    assert robots_allows(rules.encode(), "https://example.com" + path) is allowed


@pytest.mark.parametrize(
    "pattern,path,allowed",
    [
        ("/path/file-with-a-%2A.html", "/path/file-with-a-*.html", False),
        ("/path/file-with-a-%2A.html", "/path/file-with-a-%2a.html", False),
        ("/path/file-with-a-%2A.html", "/path/file-with-a-name.html", True),
        ("/path/foo-%24", "/path/foo-$", False),
        ("/path/foo-%24", "/path/foo-%24", False),
        ("/path/foo-%24", "/path/foo-", True),
        ("/path/file-with-a-*.html", "/path/file-with-a-name.html", False),
        ("/path/file-with-a-*.html", "/path/file-with-a-*.html", False),
        ("/path/file-with-a-*.html", "/path/file-with-a-%2A.html", False),
        ("/path/foo$", "/path/foo", False),
        ("/path/foo$", "/path/foo$", True),
        ("/path/foo$", "/path/foo/child", True),
        ("/path/foo-$bar", "/path/foo-$bar", False),
        ("/path/foo-%24$", "/path/foo-$/child", True),
        ("/path/foo-%24$", "/path/foo-$", False),
    ],
)
def test_robots_distinguishes_rule_operators_from_literal_uri_characters(
    pattern: str,
    path: str,
    allowed: bool,
) -> None:
    from connectors.web_search.page_policy import robots_allows

    rules = f"User-agent: *\nDisallow: {pattern}"
    assert robots_allows(rules.encode(), "https://example.com" + path) is allowed


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/admin",
        "https://user:pass@example.com/",
        "file:///etc/passwd",
    ],
)
async def test_ssrf_shapes_are_blocked_before_http_request(url: str) -> None:
    transport = SafePublicPageHttpTransport()

    with pytest.raises(PublicPageRejectedError):
        await transport.validate_url(url)


async def test_dns_resolving_to_private_ip_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", 443))
        ],
    )

    with pytest.raises(PublicPageRejectedError):
        await SafePublicPageHttpTransport().validate_url(
            "https://public-looking.example/path"
        )


@pytest.mark.parametrize(
    "url",
    HOSTILE_PUBLIC_PAGE_URLS,
)
async def test_gateway_rejects_task9_legacy_and_multicast_hosts_before_dns(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    """删除共享 host 形状门禁会让 Gateway 再次把特殊目标带到 DNS。"""

    def unexpected_resolution(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("unsafe host must not reach DNS")

    monkeypatch.setattr(socket, "getaddrinfo", unexpected_resolution)
    with pytest.raises(PublicPageRejectedError):
        await SafePublicPageHttpTransport().validate_url(url)


@pytest.mark.parametrize(
    "address",
    ("127.0.0.1", "169.254.169.254", "224.0.0.1", "ff02::1", "::"),
)
async def test_gateway_rejects_any_special_answer_in_public_dns_set(
    monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    """若只检查首个 DNS answer，rebinding 可把 fetch 引向特殊地址。"""

    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (family, socket.SOCK_STREAM, 6, "", (address, 443)),
        ],
    )
    with pytest.raises(PublicPageRejectedError):
        await SafePublicPageHttpTransport().validate_url(
            "https://ordinary.example/path"
        )


async def test_redirect_to_private_target_is_revalidated_and_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    transport = SafePublicPageHttpTransport()
    calls: list[str] = []

    def redirect_once(parsed):
        calls.append(parsed.geturl())
        return (302, "http://127.0.0.1/admin", None, None, None)

    monkeypatch.setattr(transport, "_request_once", redirect_once)

    with pytest.raises(PublicPageRejectedError):
        await transport.fetch("https://example.com/start")
    assert len(calls) == 1


async def test_search_batch_handle_is_task_local_and_release_is_terminal() -> None:
    slot = WebSearchResultSlot(new_id, maximum_batches=2)
    tenant = TenantId(new_id("tn"))
    batch = slot.put(
        tenant,
        "US",
        "hinges",
        (WebSearchResult("Acme", "https://example.com/", "public result"),),
    )
    assert batch.handle.startswith("wsb_")
    assert slot.get_batch(batch.handle) == batch

    async def child_cannot_read() -> None:
        with pytest.raises(ValidationError):
            slot.get_batch(batch.handle)

    await asyncio.create_task(child_cannot_read())
    slot.discard(batch.handle)
    with pytest.raises(ValidationError):
        slot.get_batch(batch.handle)


async def test_page_handle_is_single_use_task_local_and_discardable() -> None:
    slot = WebPageSnapshotSlot(new_id)
    snapshot = PageSnapshot(
        "Acme opened a new factory.",
        "https://example.com/news",
        NOW,
        "a" * 64,
        ArtifactId("art_01K3H0T8NBWM3KGT9XQ06YRC5V"),
    )
    handle = slot.put(snapshot)
    assert handle.startswith("wpb_")

    async def child_cannot_take() -> None:
        with pytest.raises(ValidationError):
            slot.take(handle)

    await asyncio.create_task(child_cannot_take())
    assert slot.take(handle) == snapshot
    with pytest.raises(ValidationError):
        slot.take(handle)

    second = slot.put(snapshot)
    slot.discard_all()
    with pytest.raises(ValidationError):
        slot.take(second)
