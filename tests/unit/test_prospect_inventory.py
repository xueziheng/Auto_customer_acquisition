"""公共资料准入和来源调度的行为契约。"""

import importlib
from datetime import UTC, datetime, timedelta

import pytest

from shared.schemas.provenance import Provenance, SourceType

NOW = datetime(2026, 10, 13, tzinfo=UTC)


def api():
    try:
        return importlib.import_module("domains.prospecting.inventory")
    except ModuleNotFoundError:
        pytest.fail("RED：公共候选库存领域接口尚未实现")


def candidate(**overrides):
    values = {
        "id": "pci_test",
        "canonical_domain": "WWW.Acme.com.",
        "name": "Acme",
        "country": "US",
        "product_tags": ("hinges",),
        "public_emails": ("Sales@Acme.com",),
        "source_id": "company_news",
        "source_url": "https://acme.com/news",
        "evidence_text": "Acme makes hinges",
        "observed_at": NOW,
        "expires_at": NOW + timedelta(days=30),
        "provenance": Provenance(
            SourceType.WEB_PAGE,
            "page_test",
            "human",
            NOW,
            source_url="https://acme.com/news",
            page_hash="a" * 64,
        ),
    }
    values.update(overrides)
    return api().PublicCandidate(**values)


def test_public_candidate_normalizes_identity():
    value = candidate()
    assert value.canonical_domain == "acme.com"
    assert value.public_emails == ("sales@acme.com",)


@pytest.mark.parametrize(
    "override",
    [
        {"private_notes": "secret"},
        {"source_id": "unknown"},
        {"provenance": Provenance(SourceType.UPLOAD, "upload_x", "human", NOW)},
        {"provenance": None},
        {"source_url": "http://localhost/private"},
        {"expires_at": NOW},
        {"public_emails": ("broken",)},
    ],
)
def test_private_or_untraceable_records_rejected(override):
    with pytest.raises(ValueError):
        candidate(**override)


def test_planner_excludes_unavailable_and_rotates_exploration():
    m = api()
    states = [
        m.SourceState(
            source_id="company_websites",
            availability="available",
            cost_class="free",
            relevant=True,
            valid_new_count=10,
            attempts=1,
            last_used_at=NOW,
        ),
        m.SourceState(
            source_id="company_news",
            availability="available",
            cost_class="free",
            relevant=True,
        ),
        m.SourceState(
            source_id="apollo",
            availability="not_configured",
            cost_class="unknown",
            relevant=True,
        ),
    ]
    assert set(m.select_sources(states, request_budget=2)) == {"company_websites", "company_news"}
    assert m.select_sources(states, request_budget=1) == ("company_news",)
    assert m.select_sources(states, request_budget=0) == ()


def test_query_count_and_plan_validation():
    m = api()
    with pytest.raises(ValueError):
        m.InventoryQuery(product_tags=())
    with pytest.raises(ValueError):
        m.validate_request_count(101)
    assert m.validate_request_count(100) == 100


def test_africa_query_filters_geography_and_public_api_admission():
    m = api()
    q = m.InventoryQuery(region="africa", product_tags=("hinges",))
    assert m.matches_query(candidate(country="KE"), q)
    assert not m.matches_query(candidate(country="US"), q)
    assert candidate(
        source_id="pncp",
        provenance=Provenance(
            SourceType.EXTERNAL_API,
            "record_test",
            "collector-v1",
            NOW,
            source_url="https://acme.com/news",
            page_hash="a" * 64,
        ),
    )


def test_public_api_with_key_is_not_misclassified_as_private():
    url = "https://api.sam.gov/opportunities/v2/opportunities"
    assert candidate(
        source_id="sam_gov",
        source_url=url,
        provenance=Provenance(
            SourceType.EXTERNAL_API,
            "sam_record",
            "collector-v1",
            NOW,
            source_url=url,
            page_hash="a" * 64,
        ),
    )


def test_replenishment_target_is_separate_from_delivery_size():
    m=api()
    assert hasattr(m,'replenishment_target'), 'RED: 补库存目标尚未与交付数量分离'
    assert m.replenishment_target(1)==50
    assert m.replenishment_target(50)==50
    assert m.replenishment_target(51)==100
    assert m.replenishment_target(100)==100
