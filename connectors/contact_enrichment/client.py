"""联系人补全的 Provider-neutral typed 契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Protocol, runtime_checkable

from shared.errors import ValidationError

__all__ = (
    "ContactCandidate",
    "ContactEmailKind",
    "ContactEnrichmentConnector",
    "ContactEnrichmentResult",
    "ContactSource",
    "EnrichmentCostNote",
)


class ContactEmailKind(str, Enum):
    PERSONAL = "personal"
    GENERIC = "generic"


class EnrichmentCostNote(str, Enum):
    COUNTED = "hunter.domain_search.counted"
    NO_RESULT = "hunter.domain_search.no_result"


def _exact_text(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()


@dataclass(frozen=True, repr=False)
class ContactSource:
    uri: str = field(repr=False)
    first_seen_on: date
    last_seen_on: date
    still_on_page: bool

    def __post_init__(self) -> None:
        valid_dates = (
            type(self.first_seen_on) is date
            and type(self.last_seen_on) is date
            and self.first_seen_on <= self.last_seen_on
        )
        if (
            not _exact_text(self.uri)
            or not valid_dates
            or not isinstance(self.still_on_page, bool)
        ):
            raise ValidationError("联系人来源无效")


@dataclass(frozen=True, repr=False)
class ContactCandidate:
    email: str = field(repr=False)
    full_name: str | None = field(default=None, repr=False)
    role_title: str | None = field(default=None, repr=False)
    email_kind: ContactEmailKind = ContactEmailKind.PERSONAL
    sources: tuple[ContactSource, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        try:
            sources = tuple(self.sources)
        except TypeError:
            raise ValidationError("联系人候选无效") from None
        object.__setattr__(self, "sources", sources)
        email_valid = (
            _exact_text(self.email)
            and self.email.count("@") == 1
            and not any(char.isspace() for char in self.email)
        )
        optional_text_valid = all(
            value is None or _exact_text(value)
            for value in (self.full_name, self.role_title)
        )
        if (
            not email_valid
            or not optional_text_valid
            or not isinstance(self.email_kind, ContactEmailKind)
            or not sources
            or any(not isinstance(source, ContactSource) for source in sources)
        ):
            raise ValidationError("联系人候选无效")


@dataclass(frozen=True, repr=False)
class ContactEnrichmentResult:
    candidates: tuple[ContactCandidate, ...] = field(repr=False)
    provider: str
    cost_note: EnrichmentCostNote

    def __post_init__(self) -> None:
        try:
            candidates = tuple(self.candidates)
        except TypeError:
            raise ValidationError("联系人补全结果无效") from None
        object.__setattr__(self, "candidates", candidates)
        if (
            not _exact_text(self.provider)
            or not isinstance(self.cost_note, EnrichmentCostNote)
            or any(
                not isinstance(candidate, ContactCandidate)
                for candidate in candidates
            )
        ):
            raise ValidationError("联系人补全结果无效")


@runtime_checkable
class ContactEnrichmentConnector(Protocol):
    async def find_contacts(
        self, company_domain: str, role_hints: tuple[str, ...]
    ) -> ContactEnrichmentResult:
        """按公司域名返回带来源的 typed 联系人候选。"""
        raise NotImplementedError
