"""匿名 one-click unsubscribe 路由与租户中间件窄旁路合同。"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from apps.api.main import create_app
from apps.api.middleware import ApiSettings
from shared.errors import TransientError

TOKEN = "current-v1." + "a" * 43 + "." + "b" * 43


class _Service:
    def __init__(self, outcome: bool | BaseException = False) -> None:
        self.outcome = outcome
        self.calls: list[str] = []

    async def consume(self, token: str) -> bool:
        self.calls.append(token)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    async def issue(self, tenant_id: object, preflight: object) -> object:
        del tenant_id, preflight
        raise AssertionError("router 不得签发 token")


async def _request(
    app: object,
    method: str,
    path: str,
    **kwargs: object,
) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://testserver",
    ) as client:
        return await client.request(method, path, **kwargs)


def _app(service: object | None = None, *, retry_after_seconds: int = 30) -> object:
    return create_app(
        settings=ApiSettings(
            tenant_id="tenant-a",
            dev_mode=False,
            retry_after_seconds=retry_after_seconds,
        ),
        unsubscribe_service=service,
    )


@pytest.mark.asyncio
async def test_get_is_fixed_safe_page_and_never_consumes_token() -> None:
    service = _Service(True)
    response = await _request(_app(service), "GET", f"/unsubscribe/{TOKEN}")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["content-security-policy"] == (
        "default-src 'none'; form-action 'self'; base-uri 'none'; "
        "frame-ancestors 'none'"
    )
    assert "set-cookie" not in response.headers
    assert "确认退订" in response.text
    assert TOKEN not in response.text
    assert "<script" not in response.text.lower()
    assert "http://" not in response.text and "https://" not in response.text
    assert service.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ("GET", "POST"))
async def test_only_exact_one_segment_path_bypasses_tenant_header(method: str) -> None:
    kwargs = (
        {
            "headers": {"Content-Type": "application/x-www-form-urlencoded"},
            "content": b"List-Unsubscribe=One-Click",
        }
        if method == "POST"
        else {}
    )
    exact = await _request(_app(), method, f"/unsubscribe/{TOKEN}", **kwargs)
    assert exact.status_code in {200, 204}

    for path in (
        "/unsubscribe",
        f"/unsubscribe/{TOKEN}/extra",
        f"/nested/unsubscribe/{TOKEN}",
    ):
        response = await _request(_app(), method, path, **kwargs)
        assert response.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "body"),
    [
        ({}, b"List-Unsubscribe=One-Click"),
        ({"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"}, b"List-Unsubscribe=One-Click"),
        ({"Content-Type": "application/x-www-form-urlencoded"}, b"list-unsubscribe=one-click"),
        ({"Content-Type": "application/x-www-form-urlencoded"}, b"x" * 257),
    ],
)
async def test_bad_post_shape_is_constant_204_without_repository_call(
    headers: dict[str, str], body: bytes
) -> None:
    service = _Service(True)
    response = await _request(
        _app(service),
        "POST",
        f"/unsubscribe/{TOKEN}",
        headers=headers,
        content=body,
    )

    assert response.status_code == 204
    assert response.content == b""
    assert service.calls == []


@pytest.mark.asyncio
async def test_duplicate_content_type_is_not_an_exact_one_click_request() -> None:
    service = _Service(True)
    response = await _request(
        _app(service),
        "POST",
        f"/unsubscribe/{TOKEN}",
        headers=[
            ("Content-Type", "application/x-www-form-urlencoded"),
            ("Content-Type", "application/x-www-form-urlencoded"),
        ],
        content=b"List-Unsubscribe=One-Click",
    )

    assert response.status_code == 204
    assert response.content == b""
    assert service.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", (True, False))
async def test_valid_duplicate_unknown_and_expired_are_same_empty_204(
    outcome: bool,
) -> None:
    service = _Service(outcome)
    response = await _request(
        _app(service),
        "POST",
        f"/unsubscribe/{TOKEN}",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        content=b"List-Unsubscribe=One-Click",
    )

    assert response.status_code == 204
    assert response.content == b""
    assert service.calls == [TOKEN]


@pytest.mark.asyncio
async def test_internal_failure_is_fixed_503_without_token_or_exception() -> None:
    service = _Service(TransientError("postgres://user:secret@db/private"))
    response = await _request(
        _app(service, retry_after_seconds=17),
        "POST",
        f"/unsubscribe/{TOKEN}",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        content=b"List-Unsubscribe=One-Click",
    )

    assert response.status_code == 503
    assert response.json() == {
        "code": "service_unavailable",
        "message": "退订服务暂不可用",
    }
    assert response.headers["retry-after"] == "17"
    assert TOKEN not in response.text
    assert "secret" not in response.text


@pytest.mark.asyncio
async def test_cancellation_propagates() -> None:
    service = _Service(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await _request(
            _app(service),
            "POST",
            f"/unsubscribe/{TOKEN}",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            content=b"List-Unsubscribe=One-Click",
        )


def test_one_click_routes_are_not_advertised_in_openapi() -> None:
    schema = _app(_Service()).openapi()

    assert not any(path.startswith("/unsubscribe") for path in schema["paths"])


@pytest.mark.asyncio
async def test_oversized_single_segment_token_still_uses_constant_204_contract() -> None:
    token = "x" * 513
    service = _Service(False)

    response = await _request(
        _app(service),
        "POST",
        f"/unsubscribe/{token}",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        content=b"List-Unsubscribe=One-Click",
    )

    assert response.status_code == 204
    assert response.content == b""
    assert service.calls == [token]


def test_oversized_stream_chunk_is_rejected_before_copy() -> None:
    module = __import__(
        "apps.api.routers.unsubscribe", fromlist=["_append_bounded"]
    )

    class TrackingBuffer(bytearray):
        extended = False

        def extend(self, value: object) -> None:
            self.extended = True
            super().extend(value)

    buffer = TrackingBuffer()

    assert module._append_bounded(buffer, b"x" * 257) is False
    assert buffer.extended is False
    assert buffer == b""
