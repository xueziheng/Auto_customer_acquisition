"""邮箱可达性验证的 Provider-neutral typed 契约。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Protocol, runtime_checkable

from shared.errors import ValidationError

__all__ = (
    "EmailVerificationConnector",
    "EmailVerificationOutcome",
    "EmailVerificationResult",
    "VerificationCostNote",
)


class EmailVerificationOutcome(str, Enum):
    VERIFIED = "verified"
    INVALID = "invalid"
    RISKY = "risky"
    UNVERIFIED = "unverified"


class VerificationCostNote(str, Enum):
    COUNTED = "hunter.email_verifier.counted"
    PRIVACY_REFUSED = "hunter.email_verifier.privacy_refused"
    UNKNOWN = "hunter.email_verifier.unknown"
    CACHE_HIT = "hunter.email_verifier.cache_hit"


@dataclass(frozen=True, repr=False)
class EmailVerificationResult:
    outcome: EmailVerificationOutcome
    provider: str
    checked_at: datetime
    cost_note: VerificationCostNote
    privacy_claimed: bool = False

    def __post_init__(self) -> None:
        checked_at_valid = (
            isinstance(self.checked_at, datetime)
            and self.checked_at.tzinfo is not None
            and self.checked_at.utcoffset() == timedelta(0)
        )
        provider_valid = (
            isinstance(self.provider, str)
            and bool(self.provider)
            and self.provider == self.provider.strip()
        )
        if (
            not isinstance(self.outcome, EmailVerificationOutcome)
            or not provider_valid
            or not checked_at_valid
            or not isinstance(self.cost_note, VerificationCostNote)
            or not isinstance(self.privacy_claimed, bool)
        ):
            raise ValidationError("邮箱验证结果无效")


@runtime_checkable
class EmailVerificationConnector(Protocol):
    async def verify(self, email: str) -> EmailVerificationResult:
        """验证单个邮箱并返回确定性四值结果。"""
        raise NotImplementedError
