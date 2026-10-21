"""固定 /account 的免费额度读取；只交付白名单计数，丢弃账户身份字段。"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from connectors.hunter.client import HUNTER_API_KEY_REF, HunterSecretResolver
from connectors.hunter.transport import (
    HunterHttpStatusError,
    HunterHttpTransport,
    HunterNetworkError,
)
from shared.schemas.provider_usage import ProviderUsageCounter, ProviderUsageObservation


def _number(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and 0 <= result <= Decimal(1000000000000) else None
    except InvalidOperation:
        return None


def parse_hunter_usage(payload: Mapping[str, object], checked_at: datetime) -> ProviderUsageObservation:
    """只接纳 documented requests 字段；available 是总额度而非余额。"""
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return ProviderUsageObservation(outcome="unknown", checked_at=checked_at)
    requests = data.get("requests")
    counters: list[ProviderUsageCounter] = []
    kinds: tuple[Literal["credits", "searches", "verifications"], ...] = ("credits", "searches", "verifications")
    if isinstance(requests, Mapping):
        for kind in kinds:
            item = requests.get(kind)
            if isinstance(item, Mapping):
                counters.append(ProviderUsageCounter(
                    kind=kind, used=_number(item.get("used")),
                    limit=_number(item.get("available")), remaining=_number(item.get("remaining")),
                ))
    reset = None
    raw_date = data.get("reset_date")
    if isinstance(raw_date, str):
        try:
            reset = date.fromisoformat(raw_date)
        except ValueError:
            pass
    return ProviderUsageObservation(
        outcome="success", checked_at=checked_at, counters=tuple(counters), reset_date=reset,
    )


class HunterUsageConnector:
    """只被 Gateway 执行器调用；构造不读取凭证、不访问外部。"""

    def __init__(self, transport: HunterHttpTransport, resolver: HunterSecretResolver) -> None:
        self._transport, self._resolver = transport, resolver

    async def read(self) -> ProviderUsageObservation:
        """一次免费 GET，无重试；固定错误分类，不暴露 Provider 错误文本。"""
        now = datetime.now(UTC)
        try:
            response = await self._transport.get(
                "/account", (), api_key=self._resolver.resolve(HUNTER_API_KEY_REF),
            )
        except HunterHttpStatusError as error:
            outcome = {401: "credential_invalid", 403: "credential_invalid", 402: "payment_required", 429: "rate_limited"}.get(error.status_code, "unavailable")
            return ProviderUsageObservation(outcome=outcome, checked_at=now, retry_after_seconds=error.retry_after_seconds)  # type: ignore[arg-type]
        except HunterNetworkError:
            return ProviderUsageObservation(outcome="unavailable", checked_at=now)
        if response.status_code != 200:
            return ProviderUsageObservation(outcome="unknown", checked_at=now)
        return parse_hunter_usage(response.payload, datetime.now(UTC))
