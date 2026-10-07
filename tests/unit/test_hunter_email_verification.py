"""Hunter Email Verifier 的状态映射、轮询预算与隐私合同。"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest

from connectors.email_verification.client import (
    EmailVerificationConnector,
    EmailVerificationOutcome,
    VerificationCostNote,
)
from connectors.hunter.client import (
    MANIFEST,
    HunterAuthRequiredError,
    HunterConnector,
    HunterPermanentError,
    HunterRateLimitedError,
    HunterTransientError,
    HunterUncertainError,
)
from connectors.hunter.transport import (
    HunterErrorCode,
    HunterHttpResponse,
    HunterHttpStatusError,
    HunterNetworkError,
)
from shared.errors import ValidationError

API_KEY_CANARY = "hunter-verifier-secret-canary"
EMAIL_CANARY = "private-verifier-canary@example.com"
CHECKED_AT = datetime(2026, 8, 21, 8, 30, tzinfo=UTC)


class _Resolver:
    def resolve(self, secret_ref: str) -> str:
        assert secret_ref == "HUNTER_API_KEY_REF"
        return API_KEY_CANARY


class _Transport:
    def __init__(self, responses: list[HunterHttpResponse | BaseException]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, tuple[tuple[str, str], ...], str]] = []

    async def get(
        self,
        path: str,
        params: tuple[tuple[str, str], ...],
        *,
        api_key: str,
    ) -> HunterHttpResponse:
        self.calls.append((path, params, api_key))
        if not self.responses:
            raise AssertionError("unexpected verifier request")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class _Clock:
    def __init__(self) -> None:
        self.elapsed = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.elapsed

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.elapsed += seconds


async def _connector(
    responses: list[HunterHttpResponse | BaseException],
) -> tuple[HunterConnector, _Transport, _Clock]:
    transport = _Transport(responses)
    clock = _Clock()
    connector = HunterConnector(
        transport,
        now=lambda: CHECKED_AT,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )
    await connector.configure(_Resolver())
    return connector, transport, clock


@pytest.mark.parametrize(
    ("hunter_status", "expected"),
    [
        ("valid", EmailVerificationOutcome.VERIFIED),
        ("invalid", EmailVerificationOutcome.INVALID),
        ("accept_all", EmailVerificationOutcome.RISKY),
        ("webmail", EmailVerificationOutcome.RISKY),
        ("disposable", EmailVerificationOutcome.RISKY),
        ("unknown", EmailVerificationOutcome.UNVERIFIED),
    ],
)
@pytest.mark.asyncio
async def test_verifier_status_mapping(
    hunter_status: str,
    expected: EmailVerificationOutcome,
) -> None:
    connector, transport, _ = await _connector(
        [
            HunterHttpResponse(
                200,
                {
                    "data": {
                        "status": hunter_status,
                        "score": 99,
                        "sources": [{"private": EMAIL_CANARY}],
                    }
                },
            )
        ]
    )

    result = await connector.verify("BUYER@Example.COM")

    assert result.outcome is expected
    assert result.provider == "hunter"
    assert result.checked_at == CHECKED_AT
    assert result.cost_note is VerificationCostNote.COUNTED
    assert result.privacy_claimed is False
    assert transport.calls == [
        (
            "/email-verifier",
            (("email", "buyer@example.com"),),
            API_KEY_CANARY,
        )
    ]
    assert "score" not in vars(result)
    assert 99 not in vars(result).values()
    assert EMAIL_CANARY not in repr(result)


def test_manifest_advertises_verifier_only_after_method_exists() -> None:
    assert MANIFEST.capabilities == ("contact.enrich", "contact.verify")
    assert MANIFEST.secret_refs == ("HUNTER_API_KEY_REF",)
    assert MANIFEST.rate_limit_note == (
        "Domain Search 15/s 500/min; Email Verifier 10/s 300/min"
    )


@pytest.mark.asyncio
async def test_bad_email_is_rejected_before_transport() -> None:
    connector, transport, _ = await _connector([])
    for email in (
        "",
        "not-an-email",
        "two@@example.com",
        "buyer@localhost",
        "buyer@127.0.0.1",
        " buyer @example.com ",
    ):
        with pytest.raises(ValidationError, match="Hunter Email Verifier 输入无效"):
            await connector.verify(email)
    assert transport.calls == []


@pytest.mark.asyncio
async def test_202_polls_at_most_two_more_times_and_clamps_to_budget() -> None:
    connector, transport, clock = await _connector(
        [
            HunterHttpResponse(202, {}, 5),
            HunterHttpResponse(202, {}, 40),
            HunterHttpResponse(200, {"data": {"status": "valid"}}),
        ]
    )
    result = await connector.verify("buyer@example.com")
    assert result.outcome is EmailVerificationOutcome.UNVERIFIED
    assert result.cost_note is VerificationCostNote.UNKNOWN
    assert len(transport.calls) == 2
    assert clock.sleeps == [5, 25]


@pytest.mark.asyncio
async def test_202_exhaustion_returns_unknown_after_three_total_requests() -> None:
    connector, transport, clock = await _connector(
        [
            HunterHttpResponse(202, {}),
            HunterHttpResponse(202, {}),
            HunterHttpResponse(202, {}),
        ]
    )
    result = await connector.verify("buyer@example.com")
    assert result.outcome is EmailVerificationOutcome.UNVERIFIED
    assert result.cost_note is VerificationCostNote.UNKNOWN
    assert result.checked_at == CHECKED_AT
    assert len(transport.calls) == 3
    assert clock.sleeps == [1, 1]


@pytest.mark.asyncio
async def test_terminal_200_after_poll_uses_terminal_clock_time() -> None:
    transport = _Transport(
        [
            HunterHttpResponse(202, {}, 2),
            HunterHttpResponse(200, {"data": {"status": "invalid"}}),
        ]
    )
    clock = _Clock()

    def now() -> datetime:
        return datetime(2026, 8, 21, 8, 30 + int(clock.elapsed), tzinfo=UTC)

    connector = HunterConnector(
        transport,
        now=now,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )
    await connector.configure(_Resolver())
    result = await connector.verify("buyer@example.com")
    assert result.checked_at == datetime(2026, 8, 21, 8, 32, tzinfo=UTC)
    assert result.outcome is EmailVerificationOutcome.INVALID


@pytest.mark.asyncio
async def test_222_is_unknown_without_polling() -> None:
    connector, transport, clock = await _connector([HunterHttpResponse(222, {})])
    result = await connector.verify("buyer@example.com")
    assert result.outcome is EmailVerificationOutcome.UNVERIFIED
    assert result.cost_note is VerificationCostNote.UNKNOWN
    assert len(transport.calls) == 1
    assert clock.sleeps == []


@pytest.mark.asyncio
async def test_451_claimed_email_returns_typed_privacy_refusal() -> None:
    connector, _, _ = await _connector(
        [
            HunterHttpStatusError(
                451,
                HunterErrorCode.CLAIMED_EMAIL,
            )
        ]
    )
    result = await connector.verify(EMAIL_CANARY)
    assert result.outcome is EmailVerificationOutcome.UNVERIFIED
    assert result.cost_note is VerificationCostNote.PRIVACY_REFUSED
    assert result.privacy_claimed is True
    assert result.checked_at == CHECKED_AT


@pytest.mark.parametrize(
    ("response", "expected_type", "retry_after"),
    [
        (HunterHttpStatusError(401, HunterErrorCode.OTHER), HunterAuthRequiredError, None),
        (HunterHttpStatusError(429, HunterErrorCode.OTHER, 17), HunterRateLimitedError, 17),
        (HunterHttpStatusError(451, HunterErrorCode.OTHER), HunterPermanentError, None),
        (HunterHttpStatusError(500, HunterErrorCode.OTHER), HunterTransientError, None),
        (HunterNetworkError(False), HunterTransientError, None),
        (HunterNetworkError(True), HunterUncertainError, None),
    ],
)
@pytest.mark.asyncio
async def test_verifier_transport_errors_keep_safe_classification(
    response: BaseException,
    expected_type: type[BaseException],
    retry_after: int | None,
) -> None:
    connector, _, _ = await _connector([response])
    with pytest.raises(expected_type) as captured:
        await connector.verify(EMAIL_CANARY)
    assert getattr(captured.value, "retry_after_seconds", None) == retry_after
    assert captured.value.__context__ is None


@pytest.mark.parametrize(
    "response",
    [
        HunterHttpResponse(200, {}),
        HunterHttpResponse(200, {"data": []}),
        HunterHttpResponse(200, {"data": {}}),
        HunterHttpResponse(200, {"data": {"status": "new_unknown_status"}}),
        HunterHttpResponse(201, {"data": {"status": "valid"}}),
    ],
)
@pytest.mark.asyncio
async def test_malformed_terminal_response_is_fixed_permanent_failure(
    response: HunterHttpResponse,
) -> None:
    connector, _, _ = await _connector([response])
    with pytest.raises(HunterPermanentError, match="Hunter 永久失败"):
        await connector.verify(EMAIL_CANARY)


@pytest.mark.asyncio
async def test_logs_repr_and_errors_never_expose_email_key_or_raw_flags(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    connector, _, _ = await _connector([HunterNetworkError(True)])
    with pytest.raises(HunterUncertainError) as captured:
        await connector.verify(EMAIL_CANARY)
    rendered = " ".join(
        (repr(connector), repr(captured.value), str(captured.value), caplog.text)
    )
    assert API_KEY_CANARY not in rendered
    assert EMAIL_CANARY not in rendered
    assert isinstance(connector, EmailVerificationConnector)
