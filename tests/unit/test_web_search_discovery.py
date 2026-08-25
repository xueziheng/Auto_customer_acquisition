"""公开网页 SSRF/redirect 与 task-local opaque handle 验收。"""

from __future__ import annotations

import asyncio
import socket
from datetime import UTC, datetime

import pytest

from connectors.web_search.client import PageSnapshot, WebSearchResult
from connectors.web_search.transport import (
    PublicPageRejectedError,
    SafePublicPageHttpTransport,
)
from shared.errors import ValidationError
from shared.schemas.identifiers import ArtifactId, TenantId, new_id
from tool_gateway.handlers.web_slots import WebPageSnapshotSlot, WebSearchResultSlot

NOW = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)


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
