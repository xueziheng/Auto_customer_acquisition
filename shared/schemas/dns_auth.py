"""DNS 发件认证的公开、安全事实合同。"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

from shared.errors import ValidationError

_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_SELECTOR = re.compile(r"[a-z0-9](?:[a-z0-9_-]{0,61}[a-z0-9])?\Z")
_LOWER_HEX = re.compile(r"[0-9a-f]{64}\Z")
_SECRET_MARKERS = ("bearer", "token", "secret", "password", "authorization")
_CHECKS = frozenset({"spf", "dkim", "dmarc"})
_INSTRUCTIONS = frozenset({"configure_spf", "configure_dkim", "configure_dmarc"})


def _invalid_request() -> ValidationError:
    return ValidationError("DNS 认证请求无效")


def _has_control(value: str) -> bool:
    return any(unicodedata.category(character).startswith("C") for character in value)


def _valid_domain_syntax(value: object) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 253:
        return False
    if value != value.lower() or value != value.strip() or value.endswith("."):
        return False
    if _has_control(value) or any(marker in value for marker in _SECRET_MARKERS):
        return False
    if any(character in value for character in ("@", "/", "\\", "*", ":")):
        return False
    labels = value.split(".")
    if len(labels) < 2 or any(_LABEL.fullmatch(label) is None for label in labels):
        return False
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return True
    return False


def _valid_selector(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 63
        and value == value.lower()
        and value == value.strip()
        and _SELECTOR.fullmatch(value) is not None
        and not _has_control(value)
        and not any(marker in value for marker in _SECRET_MARKERS)
    )


@dataclass(frozen=True)
class DnsAuthenticationRequest:
    domain: str
    dkim_selector: str

    def __post_init__(self) -> None:
        if not _valid_domain_syntax(self.domain) or not _valid_selector(
            self.dkim_selector
        ):
            raise _invalid_request()
        qnames = (
            self.domain,
            f"{self.dkim_selector}._domainkey.{self.domain}",
            f"_dmarc.{self.domain}",
        )
        if any(len(name.encode("ascii")) > 253 for name in qnames):
            raise _invalid_request()


class DnsAuthenticationFailureCategory(str, Enum):
    MISSING = "missing"
    MALFORMED = "malformed"
    POLICY_UNSAFE = "policy_unsafe"


@dataclass(frozen=True)
class DnsAuthenticationFailure:
    check: str
    category: DnsAuthenticationFailureCategory
    instruction: str

    def __post_init__(self) -> None:
        if (
            self.check not in _CHECKS
            or not isinstance(self.category, DnsAuthenticationFailureCategory)
            or self.instruction not in _INSTRUCTIONS
            or self.instruction != f"configure_{self.check}"
        ):
            raise ValidationError("DNS 认证失败事实无效")


@dataclass(frozen=True)
class DnsAuthenticationFacts:
    checked_at: datetime
    spf_passed: bool
    dkim_passed: bool
    dmarc_passed: bool
    failures: tuple[DnsAuthenticationFailure, ...]
    check_ref: str

    def __post_init__(self) -> None:
        passed = {
            "spf": self.spf_passed,
            "dkim": self.dkim_passed,
            "dmarc": self.dmarc_passed,
        }
        if (
            not isinstance(self.checked_at, datetime)
            or self.checked_at.tzinfo is not UTC
            or any(not isinstance(value, bool) for value in passed.values())
            or not isinstance(self.failures, tuple)
            or any(
                not isinstance(item, DnsAuthenticationFailure) for item in self.failures
            )
            or _LOWER_HEX.fullmatch(self.check_ref) is None
        ):
            raise ValidationError("DNS 认证事实无效")
        failed_checks = [item.check for item in self.failures]
        if len(failed_checks) != len(set(failed_checks)) or set(failed_checks) != {
            check for check, ok in passed.items() if not ok
        }:
            raise ValidationError("DNS 认证事实不一致")

    @property
    def all_passed(self) -> bool:
        return self.spf_passed and self.dkim_passed and self.dmarc_passed
