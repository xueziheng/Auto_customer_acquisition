"""Hunter Domain Search 的确定性 Provider 适配器。"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Protocol, TypeGuard, runtime_checkable
from urllib.parse import urlsplit

from connectors.base import ConnectorManifest
from connectors.contact_enrichment.client import (
    ContactCandidate,
    ContactEmailKind,
    ContactEnrichmentConnector,
    ContactEnrichmentResult,
    ContactSource,
    EnrichmentCostNote,
)
from connectors.email_verification.client import (
    EmailVerificationConnector,
    EmailVerificationOutcome,
    EmailVerificationResult,
    VerificationCostNote,
)
from connectors.hunter.transport import (
    HunterHttpStatusError,
    HunterHttpTransport,
    HunterNetworkError,
)
from shared.errors import ValidationError

MANIFEST = ConnectorManifest(
    connector_id="hunter",
    capabilities=("contact.enrich", "contact.verify"),
    secret_refs=("HUNTER_API_KEY_REF",),
    rate_limit_note="Domain Search 15/s 500/min; Email Verifier 10/s 300/min",
    compliance_note="PII 仅 typed 临时交接；Provider score 不进入业务数据",
)

_DOMAIN_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_LOCAL_PART = re.compile(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}")
_ROLE_TOKEN = re.compile(r"\w+", flags=re.UNICODE)


@runtime_checkable
class HunterSecretResolver(Protocol):
    def resolve(self, secret_ref: str) -> str: ...


class HunterConnectorError(Exception):
    """不携带 Provider 请求、响应或 PII 的连接器错误。"""


class HunterAuthRequiredError(HunterConnectorError):
    def __init__(self) -> None:
        super().__init__("Hunter 凭证需要恢复")


@dataclass(frozen=True)
class HunterRateLimitedError(HunterConnectorError):
    retry_after_seconds: int | None = None

    def __post_init__(self) -> None:
        if self.retry_after_seconds is not None and (
            not isinstance(self.retry_after_seconds, int)
            or isinstance(self.retry_after_seconds, bool)
            or not 1 <= self.retry_after_seconds <= 3_600
        ):
            raise ValidationError("Hunter retry-after 无效")
        HunterConnectorError.__init__(self, "Hunter 调用受限")


class HunterPermanentError(HunterConnectorError):
    def __init__(self) -> None:
        super().__init__("Hunter 永久失败")


class HunterTransientError(HunterConnectorError):
    def __init__(self) -> None:
        super().__init__("Hunter 临时失败")


class HunterUncertainError(HunterConnectorError):
    def __init__(self) -> None:
        super().__init__("Hunter 调用结果不确定")


@dataclass(frozen=True, repr=False)
class _SecretValue:
    value: str = field(repr=False)


class HunterConnector(ContactEnrichmentConnector, EmailVerificationConnector):
    """把 Hunter Domain Search 转成 Provider-neutral typed 联系人结果。"""

    manifest = MANIFEST

    def __init__(
        self,
        transport: HunterHttpTransport,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not isinstance(transport, HunterHttpTransport):
            raise ValidationError("Hunter transport 无效")
        if not all(callable(value) for value in (now, monotonic, sleeper)):
            raise ValidationError("Hunter runtime 依赖无效")
        self._transport = transport
        self._api_key: _SecretValue | None = None
        self._now = now
        self._monotonic = monotonic
        self._sleeper = sleeper

    def __repr__(self) -> str:
        return "HunterConnector()"

    async def configure(self, secret_resolver: HunterSecretResolver) -> None:
        if not isinstance(secret_resolver, HunterSecretResolver):
            raise ValidationError("Hunter secret resolver 无效")
        resolution_failed = False
        api_key: object = None
        try:
            api_key = secret_resolver.resolve("HUNTER_API_KEY_REF")
        # resolver 是凭证边界；任何实现异常都必须在这里脱敏后终止。
        except Exception:  # noqa: BLE001
            resolution_failed = True
        if resolution_failed or not _valid_api_key(api_key):
            raise HunterAuthRequiredError()
        self._api_key = _SecretValue(api_key)

    async def health_check(self) -> bool:
        if self._api_key is None:
            return False
        try:
            response = await self._transport.get(
                "/account",
                (),
                api_key=self._api_key.value,
            )
        except (HunterHttpStatusError, HunterNetworkError):
            return False
        return response.status_code == 200

    async def find_contacts(
        self,
        company_domain: str,
        role_hints: tuple[str, ...],
    ) -> ContactEnrichmentResult:
        domain, canonical_hints = _validate_search_input(company_domain, role_hints)
        if self._api_key is None:
            raise HunterAuthRequiredError()

        safe_error: HunterConnectorError | None = None
        response = None
        try:
            response = await self._transport.get(
                "/domain-search",
                (("domain", domain), ("limit", "10"), ("offset", "0")),
                api_key=self._api_key.value,
            )
        except (HunterHttpStatusError, HunterNetworkError) as error:
            safe_error = _classify_transport_error(error)
        if safe_error is not None:
            raise safe_error
        if response is None or response.status_code != 200:
            raise HunterPermanentError()
        return _parse_domain_search(response.payload, domain, canonical_hints)

    async def verify(self, email: str) -> EmailVerificationResult:
        try:
            canonical_email = _canonical_email(email)
        except (TypeError, ValueError, UnicodeError):
            raise ValidationError("Hunter Email Verifier 输入无效") from None
        if self._api_key is None:
            raise HunterAuthRequiredError()

        started_at = self._monotonic()
        attempts = 0
        while attempts < 3:
            if attempts > 0 and self._monotonic() - started_at >= 30:
                return self._unknown_verification()
            safe_error: HunterConnectorError | None = None
            response = None
            try:
                response = await self._transport.get(
                    "/email-verifier",
                    (("email", canonical_email),),
                    api_key=self._api_key.value,
                )
            except HunterHttpStatusError as error:
                if (
                    error.status_code == 451
                    and error.error_code.value == "claimed_email"
                ):
                    return EmailVerificationResult(
                        EmailVerificationOutcome.UNVERIFIED,
                        "hunter",
                        self._now(),
                        VerificationCostNote.PRIVACY_REFUSED,
                        privacy_claimed=True,
                    )
                safe_error = _classify_transport_error(error)
            except HunterNetworkError as error:
                safe_error = _classify_transport_error(error)
            if safe_error is not None:
                raise safe_error
            if response is None:
                raise HunterPermanentError()

            attempts += 1
            if response.status_code == 222:
                return self._unknown_verification()
            if response.status_code == 202:
                if attempts >= 3:
                    return self._unknown_verification()
                remaining = 30 - (self._monotonic() - started_at)
                if remaining <= 0:
                    return self._unknown_verification()
                delay = min(float(response.retry_after_seconds or 1), remaining)
                await self._sleeper(delay)
                if self._monotonic() - started_at >= 30:
                    return self._unknown_verification()
                continue
            if response.status_code != 200:
                raise HunterPermanentError()
            return self._parse_verification(response.payload)
        return self._unknown_verification()

    def _parse_verification(self, payload: object) -> EmailVerificationResult:
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            raise HunterPermanentError()
        status = payload["data"].get("status")
        outcomes = {
            "valid": EmailVerificationOutcome.VERIFIED,
            "invalid": EmailVerificationOutcome.INVALID,
            "accept_all": EmailVerificationOutcome.RISKY,
            "webmail": EmailVerificationOutcome.RISKY,
            "disposable": EmailVerificationOutcome.RISKY,
            "unknown": EmailVerificationOutcome.UNVERIFIED,
        }
        outcome = outcomes.get(status) if isinstance(status, str) else None
        if outcome is None:
            raise HunterPermanentError()
        return EmailVerificationResult(
            outcome,
            "hunter",
            self._now(),
            VerificationCostNote.COUNTED,
        )

    def _unknown_verification(self) -> EmailVerificationResult:
        return EmailVerificationResult(
            EmailVerificationOutcome.UNVERIFIED,
            "hunter",
            self._now(),
            VerificationCostNote.UNKNOWN,
        )


def _validate_search_input(
    company_domain: object,
    role_hints: object,
) -> tuple[str, tuple[str, ...]]:
    try:
        domain = _canonical_domain(company_domain)
        hints = _canonical_role_hints(role_hints)
    except (TypeError, ValueError, UnicodeError):
        raise ValidationError("Hunter Domain Search 输入无效") from None
    if type(role_hints) is not tuple or len(role_hints) > 10:
        raise ValidationError("Hunter Domain Search 输入无效")
    return domain, hints


def _canonical_role_hints(role_hints: object) -> tuple[str, ...]:
    if type(role_hints) is not tuple or len(role_hints) > 10:
        raise ValidationError("Hunter Domain Search 输入无效")
    canonical: set[str] = set()
    for hint in role_hints:
        if not isinstance(hint, str):
            raise ValidationError("Hunter Domain Search 输入无效")
        normalized = " ".join(hint.split()).casefold()
        if not normalized or len(normalized.encode("utf-8")) > 200:
            raise ValidationError("Hunter Domain Search 输入无效")
        canonical.add(normalized)
    return tuple(sorted(canonical))


def _canonical_domain(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError
    candidate = value.strip().rstrip(".")
    if (
        not candidate
        or len(candidate.encode("utf-8")) > 253
        or any(character.isspace() for character in candidate)
        or any(ord(character) < 32 or ord(character) == 127 for character in candidate)
    ):
        raise ValueError
    canonical = candidate.encode("idna").decode("ascii").casefold()
    labels = canonical.split(".")
    if len(labels) < 2 or any(_DOMAIN_LABEL.fullmatch(label) is None for label in labels):
        raise ValueError
    try:
        ipaddress.ip_address(canonical)
    except ValueError:
        return canonical
    raise ValueError


def _parse_domain_search(
    payload: object,
    company_domain: str,
    role_hints: tuple[str, ...],
) -> ContactEnrichmentResult:
    if not isinstance(payload, dict):
        raise HunterPermanentError()
    data = payload.get("data")
    meta = payload.get("meta")
    if not isinstance(data, dict) or not isinstance(meta, dict):
        raise HunterPermanentError()
    emails = data.get("emails")
    linked_domains = data.get("linked_domains", [])
    results = meta.get("results")
    if (
        not isinstance(emails, list)
        or not isinstance(linked_domains, list)
        or not isinstance(results, int)
        or isinstance(results, bool)
        or results < 0
    ):
        raise HunterPermanentError()

    allowed_domains = {company_domain}
    for linked_domain in linked_domains:
        try:
            allowed_domains.add(_canonical_domain(linked_domain))
        except (TypeError, ValueError, UnicodeError):
            continue

    candidates = [
        candidate
        for item in emails
        if (candidate := _parse_candidate(item, allowed_domains, role_hints))
        is not None
    ]
    candidates.sort(
        key=lambda candidate: (
            0 if candidate.email_kind is ContactEmailKind.PERSONAL else 1,
            candidate.email,
        )
    )
    unique_candidates: list[ContactCandidate] = []
    seen_emails: set[str] = set()
    for candidate in candidates:
        if candidate.email in seen_emails:
            continue
        seen_emails.add(candidate.email)
        unique_candidates.append(candidate)
        if len(unique_candidates) == 5:
            break

    cost_note = (
        EnrichmentCostNote.COUNTED
        if results > 0
        else EnrichmentCostNote.NO_RESULT
    )
    return ContactEnrichmentResult(tuple(unique_candidates), "hunter", cost_note)


def _parse_candidate(
    item: object,
    allowed_domains: set[str],
    role_hints: tuple[str, ...],
) -> ContactCandidate | None:
    if not isinstance(item, dict):
        return None
    try:
        email = _canonical_email(item.get("value"))
    except (TypeError, ValueError, UnicodeError):
        return None
    email_domain = email.rsplit("@", 1)[1]
    if email_domain not in allowed_domains:
        return None
    kind_value = item.get("type")
    if kind_value == "personal":
        email_kind = ContactEmailKind.PERSONAL
    elif kind_value == "generic":
        email_kind = ContactEmailKind.GENERIC
    else:
        return None

    position = _optional_text(item.get("position"))
    department = _optional_text(item.get("department"))
    seniority = _optional_text(item.get("seniority"))
    if role_hints and not _role_matches(
        role_hints,
        (position, department, seniority),
    ):
        return None

    raw_sources = item.get("sources")
    if not isinstance(raw_sources, list):
        return None
    sources = sorted(
        {
            source
            for raw_source in raw_sources
            if (source := _parse_source(raw_source)) is not None
        },
        key=lambda source: (
            source.uri,
            source.first_seen_on,
            source.last_seen_on,
            source.still_on_page,
        ),
    )[:20]
    if not sources:
        return None

    name_parts = tuple(
        part
        for raw_part in (item.get("first_name"), item.get("last_name"))
        if (part := _optional_text(raw_part)) is not None
    )
    full_name = " ".join(name_parts) or None
    return ContactCandidate(
        email,
        full_name,
        position,
        email_kind,
        tuple(sources),
    )


def _parse_source(value: object) -> ContactSource | None:
    if not isinstance(value, dict):
        return None
    uri = value.get("uri")
    first_seen = value.get("extracted_on")
    last_seen = value.get("last_seen_on")
    still_on_page = value.get("still_on_page")
    if (
        not isinstance(uri, str)
        or len(uri.encode("utf-8")) > 2_048
        or not isinstance(first_seen, str)
        or not isinstance(last_seen, str)
        or not isinstance(still_on_page, bool)
    ):
        return None
    try:
        parsed_uri = urlsplit(uri)
        first_seen_on = date.fromisoformat(first_seen)
        last_seen_on = date.fromisoformat(last_seen)
    except (UnicodeError, ValueError):
        return None
    if (
        parsed_uri.scheme not in {"http", "https"}
        or not parsed_uri.hostname
        or parsed_uri.username is not None
        or parsed_uri.password is not None
        or first_seen_on > last_seen_on
    ):
        return None
    try:
        return ContactSource(uri, first_seen_on, last_seen_on, still_on_page)
    except ValidationError:
        return None


def _canonical_email(value: object) -> str:
    if not isinstance(value, str) or value.count("@") != 1:
        raise ValueError
    local_part, raw_domain = value.strip().rsplit("@", 1)
    local_part = local_part.casefold()
    if (
        _LOCAL_PART.fullmatch(local_part) is None
        or local_part.startswith(".")
        or local_part.endswith(".")
        or ".." in local_part
    ):
        raise ValueError
    domain = _canonical_domain(raw_domain)
    email = f"{local_part}@{domain}"
    if len(email.encode("utf-8")) > 254:
        raise ValueError
    return email


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    if not normalized or len(normalized.encode("utf-8")) > 500:
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        return None
    return normalized


def _role_matches(
    hints: tuple[str, ...],
    role_fields: tuple[str | None, ...],
) -> bool:
    role_tokens = {
        token
        for field_value in role_fields
        if field_value is not None
        for token in _ROLE_TOKEN.findall(field_value.casefold())
    }
    return any(
        set(_ROLE_TOKEN.findall(hint)).issubset(role_tokens)
        for hint in hints
    )


def _classify_transport_error(
    error: HunterHttpStatusError | HunterNetworkError,
) -> HunterConnectorError:
    if isinstance(error, HunterNetworkError):
        return (
            HunterUncertainError()
            if error.may_have_reached_provider
            else HunterTransientError()
        )
    if error.status_code == 401:
        return HunterAuthRequiredError()
    if error.status_code in {403, 429}:
        return HunterRateLimitedError(error.retry_after_seconds)
    if error.status_code >= 500:
        return HunterTransientError()
    return HunterPermanentError()


def _valid_api_key(value: object) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and not any(ord(character) < 33 or ord(character) == 127 for character in value)
    )


__all__ = (
    "MANIFEST",
    "HunterAuthRequiredError",
    "HunterConnector",
    "HunterConnectorError",
    "HunterPermanentError",
    "HunterRateLimitedError",
    "HunterSecretResolver",
    "HunterTransientError",
    "HunterUncertainError",
)
