from __future__ import annotations

import importlib
from datetime import UTC, datetime
from decimal import Decimal

from shared.schemas.identifiers import TenantId
from shared.schemas.money import CurrencyCode, Money

_models = importlib.import_module("domains.organization.models")
CompanyPlaybook = _models.CompanyPlaybook


def _playbook() -> CompanyPlaybook:
    return CompanyPlaybook(
        tenant_id=TenantId("tenant-one"),
        company_type="trading_company",
        minimum_deal_value=Money(Decimal(10000), CurrencyCode("USD")),
        updated_at=datetime(2026, 8, 21, 12, tzinfo=UTC),
        excluded_categories=["Adult Products", "firearms / ammunition"],
        excluded_countries=["North Korea", "IR"],
    )


def test_category_gate_is_normalized_and_conservative_for_phrase_matches() -> None:
    playbook = _playbook()

    assert playbook.is_category_allowed("premium ADULT-products catalog") is False
    assert playbook.is_category_allowed("Firearms ammunition accessories") is False
    assert playbook.is_category_allowed("stainless steel hinges") is True
    assert playbook.is_category_allowed("   ") is False


def test_country_gate_requires_normalized_exact_match_and_fails_closed() -> None:
    playbook = _playbook()

    assert playbook.is_country_allowed("  north   KOREA ") is False
    assert playbook.is_country_allowed("ir") is False
    assert playbook.is_country_allowed("Ireland") is True
    assert playbook.is_country_allowed("") is False
