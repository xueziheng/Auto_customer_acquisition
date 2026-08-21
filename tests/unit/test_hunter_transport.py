"""Hunter HTTP 传输的固定主机、资源上限与脱敏合同。"""

from __future__ import annotations

import io
import logging
from email.message import Message
from typing import Self
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from connectors.hunter.transport import (
    HunterApiHttpTransport,
    HunterErrorCode,
    HunterHttpResponse,
    HunterHttpStatusError,
    HunterHttpTransport,
    HunterNetworkError,
    _NoRedirectHandler,
)
from shared.errors import ValidationError

API_KEY_CANARY = "hunter-secret-key-canary"
EMAIL_CANARY = "private-email-canary@example.com"
BODY_CANARY = "private-provider-body-canary"
AUTH_CANARY = "Authorization: Bearer private-auth-canary"


class _Response:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        headers: Message | None = None,
    ) -> None:
        self._body = body
        self.status = status
        self.headers = headers or Message()
        self.read_size: int | None = None
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        self.read_size = size
        return self._body if size < 0 else self._body[:size]

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.closed = True


class _Opener:
    def __init__(self, response: _Response | BaseException) -> None:
        self.response = response
        self.request: Request | None = None
        self.timeout: float | None = None
        self.calls = 0

    def open(self, request: Request, *, timeout: float) -> _Response:
        self.calls += 1
        self.request = request
        self.timeout = timeout
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


class _FailingBody(io.BytesIO):
    def read(self, _size: int = -1) -> bytes:
        raise OSError(f"{BODY_CANARY} {API_KEY_CANARY} {EMAIL_CANARY}")


def _transport(
    response: _Response | BaseException,
    *,
    timeout_seconds: float = 10,
) -> tuple[HunterApiHttpTransport, _Opener]:
    opener = _Opener(response)
    return (
        HunterApiHttpTransport(
            timeout_seconds=timeout_seconds,
            opener=opener,
        ),
        opener,
    )


@pytest.mark.asyncio
async def test_get_uses_fixed_host_header_auth_get_and_sorted_encoded_query() -> None:
    response = _Response(b'{"data":{"ok":true}}')
    transport, opener = _transport(response, timeout_seconds=30)

    result = await transport.get(
        "/email-verifier",
        (("z", "a b"), ("email", EMAIL_CANARY), ("a", "/+")),
        api_key=API_KEY_CANARY,
    )

    request = opener.request
    assert request is not None
    assert request.full_url == (
        "https://api.hunter.io/v2/email-verifier"
        "?a=%2F%2B&email=private-email-canary%40example.com&z=a+b"
    )
    assert request.method == "GET"
    assert request.data is None
    assert request.get_header("X-api-key") == API_KEY_CANARY
    assert API_KEY_CANARY not in request.full_url
    assert not request.has_header("Authorization")
    assert opener.timeout == 30
    assert response.read_size == 524_289
    assert result == HunterHttpResponse(200, {"data": {"ok": True}})


@pytest.mark.parametrize("path", ["/account", "/domain-search", "/email-verifier"])
@pytest.mark.asyncio
async def test_only_allowlisted_relative_paths_are_accepted(path: str) -> None:
    transport, opener = _transport(_Response(b"{}"))
    await transport.get(path, (), api_key=API_KEY_CANARY)
    assert opener.calls == 1


@pytest.mark.parametrize(
    "path",
    [
        "account",
        "/v2/account",
        "/domain-search/extra",
        "https://api.hunter.io/v2/account",
        "//attacker.example/account",
        "/account?api_key=leak",
    ],
)
@pytest.mark.asyncio
async def test_invalid_path_is_rejected_before_network(path: str) -> None:
    transport, opener = _transport(_Response(b"{}"))
    with pytest.raises(ValidationError, match="Hunter 请求参数无效"):
        await transport.get(path, (), api_key=API_KEY_CANARY)
    assert opener.calls == 0


def test_constructor_rejects_unbounded_timeout() -> None:
    opener = _Opener(_Response(b"{}"))
    for timeout in (0, -1, 30.01, True, "10"):
        with pytest.raises(ValidationError, match="Hunter timeout 无效"):
            HunterApiHttpTransport(  # type: ignore[arg-type]
                timeout_seconds=timeout,
                opener=opener,
            )


def test_redirect_handler_never_follows_redirects() -> None:
    handler = _NoRedirectHandler()
    assert (
        handler.redirect_request(
            Request("https://api.hunter.io/v2/account"),
            io.BytesIO(),
            302,
            "redirect",
            Message(),
            "https://attacker.example/steal",
        )
        is None
    )


@pytest.mark.asyncio
async def test_response_body_is_bounded_and_must_be_json_object() -> None:
    oversized = _Response(b"{" + (b"x" * 524_288))
    transport, _ = _transport(oversized)
    with pytest.raises(HunterNetworkError) as size_error:
        await transport.get("/account", (), api_key=API_KEY_CANARY)
    assert size_error.value.may_have_reached_provider is True
    assert oversized.read_size == 524_289

    deeply_nested_json = (b"[" * 1_100) + b"0" + (b"]" * 1_100)
    for body in (b"not-json", b"[]", b'"scalar"', deeply_nested_json):
        transport, _ = _transport(_Response(body))
        with pytest.raises(HunterNetworkError) as schema_error:
            await transport.get("/account", (), api_key=API_KEY_CANARY)
        assert schema_error.value.may_have_reached_provider is True


def _http_error(
    *,
    status: int,
    body: bytes,
    retry_after: str | None = None,
) -> HTTPError:
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return HTTPError(
        f"https://api.hunter.io/v2/email-verifier?email={EMAIL_CANARY}",
        status,
        BODY_CANARY,
        headers,
        io.BytesIO(body),
    )


@pytest.mark.parametrize(
    ("retry_after", "expected"),
    [("1", 1), ("3600", 3600), (None, None), ("0", None), ("3601", None), ("1.5", None)],
)
@pytest.mark.asyncio
async def test_http_error_retains_only_bounded_retry_after(
    retry_after: str | None,
    expected: int | None,
) -> None:
    provider_error = _http_error(
        status=429,
        body=b'{"errors":[{"id":"rate_limit","details":"private"}]}',
        retry_after=retry_after,
    )
    transport, _ = _transport(provider_error)

    with pytest.raises(HunterHttpStatusError) as captured:
        await transport.get(
            "/email-verifier", (("email", EMAIL_CANARY),), api_key=API_KEY_CANARY
        )

    assert captured.value.status_code == 429
    assert captured.value.error_code is HunterErrorCode.OTHER
    assert captured.value.retry_after_seconds == expected
    assert provider_error.fp is None or provider_error.fp.closed


@pytest.mark.asyncio
async def test_claimed_email_is_the_only_typed_provider_error_code() -> None:
    for provider_id, expected in (
        ("claimed_email", HunterErrorCode.CLAIMED_EMAIL),
        ("unknown_private_code", HunterErrorCode.OTHER),
    ):
        provider_error = _http_error(
            status=451,
            body=(
                '{"errors":[{"id":"'
                + provider_id
                + '","details":"'
                + BODY_CANARY
                + '"}]}'
            ).encode(),
        )
        transport, _ = _transport(provider_error)
        with pytest.raises(HunterHttpStatusError) as captured:
            await transport.get(
                "/email-verifier",
                (("email", EMAIL_CANARY),),
                api_key=API_KEY_CANARY,
            )
        assert captured.value.error_code is expected


@pytest.mark.asyncio
async def test_network_failures_are_ambiguous_and_drop_provider_exception() -> None:
    transport, _ = _transport(
        URLError(f"{BODY_CANARY} {API_KEY_CANARY} {EMAIL_CANARY}")
    )
    with pytest.raises(HunterNetworkError) as captured:
        await transport.get(
            "/email-verifier", (("email", EMAIL_CANARY),), api_key=API_KEY_CANARY
        )
    assert captured.value.may_have_reached_provider is True
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.asyncio
async def test_http_error_body_read_failure_is_safely_collapsed() -> None:
    headers = Message()
    failing_body = _FailingBody()
    provider_error = HTTPError(
        f"https://api.hunter.io/v2/email-verifier?email={EMAIL_CANARY}",
        500,
        BODY_CANARY,
        headers,
        failing_body,
    )
    transport, _ = _transport(provider_error)

    with pytest.raises(HunterNetworkError) as captured:
        await transport.get(
            "/email-verifier", (("email", EMAIL_CANARY),), api_key=API_KEY_CANARY
        )

    assert captured.value.may_have_reached_provider is True
    assert captured.value.__context__ is None
    assert failing_body.closed


@pytest.mark.asyncio
async def test_secrets_and_pii_never_enter_repr_errors_or_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    raw_body = (
        f'{{"errors":[{{"id":"claimed_email","details":"{BODY_CANARY} '
        f'{AUTH_CANARY} {API_KEY_CANARY} {EMAIL_CANARY}"}}]}}'
    ).encode()
    provider_error = _http_error(status=451, body=raw_body, retry_after="15")
    transport, opener = _transport(provider_error)

    with pytest.raises(HunterHttpStatusError) as captured:
        await transport.get(
            "/email-verifier", (("email", EMAIL_CANARY),), api_key=API_KEY_CANARY
        )

    request = opener.request
    assert request is not None
    rendered = " ".join(
        (
            repr(request),
            repr(transport),
            repr(captured.value),
            str(captured.value),
            repr(captured.value.args),
            caplog.text,
        )
    )
    for canary in (
        API_KEY_CANARY,
        EMAIL_CANARY,
        BODY_CANARY,
        AUTH_CANARY,
        request.full_url,
    ):
        assert canary not in rendered


def test_public_values_have_fixed_safe_repr_and_runtime_protocol() -> None:
    response = HunterHttpResponse(202, {"email": EMAIL_CANARY}, 9)
    status_error = HunterHttpStatusError(
        451,
        HunterErrorCode.CLAIMED_EMAIL,
        15,
    )
    network_error = HunterNetworkError(may_have_reached_provider=True)
    transport, _ = _transport(_Response(b"{}"))

    assert repr(response) == (
        "HunterHttpResponse(status_code=202, retry_after_seconds=9)"
    )
    assert str(status_error) == "Hunter HTTP 调用失败"
    assert str(network_error) == "Hunter 网络调用失败"
    assert repr(transport) == "HunterApiHttpTransport()"
    assert isinstance(transport, HunterHttpTransport)


def test_public_values_validate_their_safe_metadata() -> None:
    with pytest.raises(ValidationError, match="Hunter HTTP 响应无效"):
        HunterHttpResponse(True, {})  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="Hunter HTTP 状态无效"):
        HunterHttpStatusError(200, HunterErrorCode.OTHER)
    with pytest.raises(ValidationError, match="Hunter 网络状态无效"):
        HunterNetworkError(may_have_reached_provider=1)  # type: ignore[arg-type]
