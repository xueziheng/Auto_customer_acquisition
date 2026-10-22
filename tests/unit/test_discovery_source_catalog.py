"""来源目录对管理界面公开的选择、认证字段与实现状态契约。"""

from shared.schemas.discovery_sources import DISCOVERY_SOURCES, get_discovery_source


def test_selected_sources_remain_discoverable_separately_from_backups_and_examples() -> None:
    selected = {item.source_id for item in DISCOVERY_SOURCES if item.selection == "selected"}
    assert {
        "hunter", "apollo", "pdl", "snov", "ocp", "ted", "find_a_tender",
        "contracts_finder", "sam_gov", "canadabuys", "pncp", "compras_gov_br",
        "world_bank", "ungm", "importyeti", "volza", "panjiva", "importgenius",
        "company_websites", "industry_directories", "trade_associations", "chambers",
        "trade_fairs", "dealer_networks", "company_procurement", "company_news",
        "rss", "linkedin_public", "trade_record_indexes", "codex_cli", "brave", "tavily",
    } <= selected
    assert get_discovery_source("zerobounce").selection == "backup"
    assert get_discovery_source("blum").selection == "example"
    assert not any("chile" in item.source_id or "mercado" in item.source_id
                   for item in DISCOVERY_SOURCES)


def test_credential_forms_distinguish_single_key_oauth_and_open_sources() -> None:
    assert [field.name for field in get_discovery_source("hunter").credential_fields] == ["api_key"]
    assert [field.name for field in get_discovery_source("snov").credential_fields] == [
        "client_id", "client_secret",
    ]
    for source_id in ("ted", "ungm", "ocp", "canadabuys", "pncp", "world_bank"):
        assert get_discovery_source(source_id).credential_fields == ()
    assert get_discovery_source("volza").credential_fields == ()
    assert [field.name for field in get_discovery_source("panjiva").credential_fields] == [
        "client_id", "client_secret",
    ]
    assert [field.name for field in get_discovery_source("importgenius").credential_fields] == [
        "access_token",
    ]


def test_missing_credentials_do_not_mislabel_commercial_sources_as_public() -> None:
    assert get_discovery_source("volza").credential_mode == "commercial_pending"
    assert get_discovery_source("codex_cli").credential_mode == "managed_session"
    assert get_discovery_source("insee").credential_mode == "authentication_pending"
    assert get_discovery_source("panjiva").credential_mode == "oauth_client"
    assert get_discovery_source("importgenius").credential_mode == "access_token"
    for item in DISCOVERY_SOURCES:
        if item.credential_fields:
            assert item.credential_mode in {"api_key", "oauth_client", "access_token"}


def test_lookup_cannot_turn_unknown_identifier_into_a_configurable_source() -> None:
    assert get_discovery_source("unknown-provider") is None
    assert get_discovery_source("../hunter") is None
    assert len({item.source_id for item in DISCOVERY_SOURCES}) == len(DISCOVERY_SOURCES)


def test_planned_sources_never_claim_to_have_working_collectors() -> None:
    for source_id in ("apollo", "snov", "pdl", "ted", "ungm", "importyeti"):
        item = get_discovery_source(source_id)
        assert item.integration_status == "planned"
        assert item.integration_note
    assert get_discovery_source("hunter").integration_status == "partial"
    assert get_discovery_source("codex_cli").integration_status == "partial"


def test_directions_have_no_fabricated_website_or_registration_destination() -> None:
    for source_id in ("company_websites", "company_procurement", "trade_record_indexes"):
        item = get_discovery_source(source_id)
        assert item.website_url is None
        assert item.registration_url is None
    for item in DISCOVERY_SOURCES:
        for value in (item.website_url, item.registration_url, item.docs_url):
            assert value is None or value.startswith("https://")
        assert len({field.name for field in item.credential_fields}) == len(item.credential_fields)
