"""事实字段必须来自受信页面，模型建议不直接晋升为事实。"""

import importlib
from datetime import UTC, datetime

import pytest


def project():
    try:
        return importlib.import_module(
            "domains.prospecting.public_evidence"
        ).project_candidate
    except ModuleNotFoundError:
        pytest.fail("RED: 受信公开事实投影尚未实现")


def test_public_projection_rejects_invented_email_country_and_domain():
    fn = project()
    base = {
        "source_id": "company_websites",
        "source_url": "https://acme.example/contact",
        "text": "Acme Ltd\nAddress: Kenya\nTricycles\nsales@acme.example",
        "content_hash": "a" * 64,
        "artifact_id": "art_test",
        "observed_at": datetime.now(UTC),
        "name": "Acme Ltd",
        "country": "KE",
        "product_tags": ("tricycles",),
        "public_emails": ("sales@acme.example",),
    }
    candidate = fn(**base)
    assert candidate.country == "KE"
    assert candidate.provenance.source_id == "art_test"
    assert candidate.public_emails == ("sales@acme.example",)
    for change in (
        {"public_emails": ("ceo@acme.example",)},
        {"country": "ZA"},
        {"name": "Made Up"},
    ):
        with pytest.raises(ValueError):
            fn(**(base | change))


def test_public_projection_does_not_promote_inference_to_fact():
    fn = project()
    with pytest.raises(ValueError):
        fn(
            source_id="company_websites",
            source_url="https://acme.example",
            text="Acme may sell in Kenya",
            content_hash="a" * 64,
            artifact_id="art_test",
            observed_at=datetime.now(UTC),
            name="Acme",
            country="KE",
            product_tags=("tricycles",),
            public_emails=(),
        )


def test_directory_host_cannot_impersonate_listed_company():
    with pytest.raises(ValueError):
        project()(
            source_id="company_websites",
            source_url="https://directory.example/acme",
            text="Acme Ltd\nAddress: Kenya\ntricycles sales@acme.example",
            content_hash="a" * 64,
            artifact_id="art_test",
            observed_at=datetime.now(UTC),
            name="Acme Ltd",
            country="KE",
            product_tags=("tricycles",),
            public_emails=("sales@acme.example",),
        )


@pytest.mark.parametrize(
    ("address", "wrong", "correct"),
    [
        ("South Sudan", "SD", "SS"),
        ("Nigeria", "NE", "NG"),
        ("Democratic Republic of the Congo", "CG", "CD"),
        ("Equatorial Guinea", "GN", "GQ"),
        ("Guinea-Bissau", "GN", "GW"),
    ],
)
def test_country_entities_use_longest_complete_name(address, wrong, correct):
    values = {
        "source_id": "company_websites",
        "source_url": "https://acme.example",
        "text": f"Acme\nAddress: {address}\ntricycles",
        "content_hash": "a" * 64,
        "artifact_id": "art_test",
        "observed_at": datetime.now(UTC),
        "name": "Acme",
        "product_tags": ("tricycles",),
        "public_emails": (),
    }
    with pytest.raises(ValueError):
        project()(**values, country=wrong)
    assert project()(**values, country=correct).country == correct


@pytest.mark.parametrize(
    "address",
    ["Nigeria and Niger", "South Sudan / Sudan", "Nigerian office", "Kenyan office"],
)
def test_country_conflicts_or_adjectives_are_not_address_evidence(address):
    with pytest.raises(ValueError):
        project()(
            source_id="company_websites",
            source_url="https://acme.example",
            text=f"Acme\nAddress: {address}\ntricycles",
            content_hash="a" * 64,
            artifact_id="art_test",
            observed_at=datetime.now(UTC),
            name="Acme",
            country="SD"
            if "Sudan" in address
            else "KE"
            if "Kenyan" in address
            else "NE",
            product_tags=("tricycles",),
            public_emails=(),
        )
