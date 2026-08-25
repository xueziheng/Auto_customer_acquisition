"""触达域发布契约与领域说明保持显式一致。"""

from pathlib import Path

from domains.outreach.events import PUBLISHES
from shared.events.catalog import CampaignStateChanged


def test_campaign_state_changed_is_declared_by_outreach_domain() -> None:
    assert CampaignStateChanged in PUBLISHES


def test_all_outreach_published_events_are_named_in_agents_contract() -> None:
    document = (
        Path(__file__).parents[2] / "domains" / "outreach" / "AGENTS.md"
    ).read_text(encoding="utf-8")
    assert [event.__name__ for event in PUBLISHES if event.__name__ not in document] == []
