"""模型意图白名单及研究字段的可核验来源。"""

import pytest
from pydantic import ValidationError

from agent_runtime.assistant.context import AssistantContext
from agent_runtime.assistant.decision import parse_decision, validate_decision
from agent_runtime.assistant.proposal import ResearchProposalBuilder
from domains.assistant.schemas import Clarification, ResearchDraft, SourcedField
from tests.unit.test_assistant_context import ACTOR, turn


def test_freeform_confirmation_is_not_a_model_action():
    with pytest.raises(ValidationError):
        parse_decision('{"kind":"confirm","proposal_id":"other"}')
    assert (
        parse_decision(
            '{"kind":"clarify","questions":["研究哪些国家？"],"missing_fields":["target_countries"]}'
        ).kind
        == "clarify"
    )


def context(*turns, role="boss"):
    return AssistantContext(
        ACTOR,
        role,
        frozenset({"product_help", "research_proposal"}),
        (),
        turns,
        0,
        "v1",
    )


async def test_invented_budget_and_wrong_source_cannot_form_plan():
    ctx = context(turn("one").model_copy(update={"input_text": "只研究美国铰链"}))
    result = await ResearchProposalBuilder().build(
        ctx,
        ResearchDraft(
            fields=(
                SourcedField(
                    name="max_search_queries", value="3", source_turn_id="one"
                ),
            )
        ),
    )
    assert isinstance(result, Clarification)
    assert "max_search_queries" in result.missing_fields


async def test_later_explicit_correction_overrides_old_budget():
    fields = {
        "objective": "研究铰链需求",
        "target_countries": "US",
        "target_categories": "hinges",
        "excluded_countries": "无",
        "excluded_categories": "无",
        "max_search_queries": "6",
        "max_pages_read": "2",
        "max_signals": "2",
        "max_hypotheses": "1",
        "minimum_confidence_tier": "low_mid",
        "strategy_group": "demand_first",
        "query_limit": "3",
    }
    first = turn("one").model_copy(
        update={"input_text": "；".join(f"{k}={v}" for k, v in fields.items())}
    )
    second = turn("two").model_copy(update={"input_text": "max_search_queries=3"})
    draft = ResearchDraft(
        fields=tuple(
            SourcedField(name=k, value=v, source_turn_id="one")
            for k, v in fields.items()
        )
    )
    result = await ResearchProposalBuilder().build(context(first, second), draft)
    assert (
        isinstance(result, Clarification)
        and "max_search_queries" in result.missing_fields
    )
    corrected = ResearchDraft(
        fields=tuple(
            SourcedField(
                name=k,
                value="3" if k == "max_search_queries" else v,
                source_turn_id="two" if k == "max_search_queries" else "one",
            )
            for k, v in fields.items()
        )
    )
    plan = await ResearchProposalBuilder().build(context(first, second), corrected)
    assert plan.max_search_queries == 3 and plan.execution_mode == "research_only"
    assert {q.discovery_lane for q in plan.queries} == {
        "importer",
        "distributor",
        "ecommerce",
    }
    assert not plan.campaign_id


async def test_sales_cannot_prepare_confirmable_proposal():
    from shared.errors import PermissionDenied

    with pytest.raises(PermissionDenied):
        await ResearchProposalBuilder().build(
            context(turn("one"), role="sales"),
            ResearchDraft(
                fields=(
                    SourcedField(name="objective", value="test", source_turn_id="one"),
                )
            ),
        )


def test_fabricated_fact_link_and_probability_are_rejected():
    from shared.errors import ValidationError as Invalid

    ctx = context(turn("one"))
    for text in (
        '{"kind":"explain","fragments":[{"text":"明确会购买","dependencies":[{"kind":"need","object_id":"made_up"}]}]}',
        '{"kind":"clarify","questions":["成交概率0.85，可以报价吗？"],"missing_fields":[]}',
    ):
        with pytest.raises(Invalid):
            validate_decision(parse_decision(text), ctx)
