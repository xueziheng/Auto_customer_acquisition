"""本机研究确认只激活固定的公开研究预算，不带触达权限。"""

from scripts.pilot_research_directive import CHANGES, SUMMARY, plan


def test_kenya_research_plan_has_exact_small_scope() -> None:
    draft = plan()
    assert draft.execution_mode == "research_only"
    assert draft.target_countries == ("KE",)
    assert draft.target_categories == ("solar electric three-wheeler",)
    assert draft.max_search_queries == 3
    assert draft.max_pages_read == 3
    assert draft.max_signals == 3
    assert draft.max_hypotheses == 3
    assert {query.discovery_lane for query in draft.queries} == {
        "importer", "distributor", "ecommerce"
    }
    assert all(query.country == "KE" and query.limit == 1 for query in draft.queries)
    assert draft.campaign_id == ""
    assert draft.role_hints == ()
    assert "不发邮件" in SUMMARY
    assert any("不启用联系人发现" in change for change in CHANGES)
