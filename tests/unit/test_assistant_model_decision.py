"""模型只选择授权来源；正文与来源引用由代码生成。"""

from dataclasses import replace

import pytest

from agent_runtime.assistant.decision import validate_decision
from agent_runtime.assistant.model_decision import (
    AssistantOutputError,
    model_decision_schema,
    parse_model_decision,
)
from agent_runtime.assistant.reads import PRODUCT_HELP, PRODUCT_REF
from domains.assistant.schemas import AuthorizedFragment, Explanation, ObjectRef
from tests.unit.test_assistant_decision import context, turn

RESEARCH_INPUT = "研究目标=测试铰链需求；国家=US；品类=hinges；排除国家=无；排除品类=无；搜索次数=9；页面数=2；信号数=2；假设数=1；证据门槛=low_mid；策略组=demand_first；每次搜索结果数=3"


async def test_research_selection_uses_canonical_fields_from_chinese_user_labels():
    from agent_runtime.assistant.proposal import ResearchProposalBuilder

    ctx = context(turn("one").model_copy(update={"input_text": RESEARCH_INPUT}))
    draft = parse_model_decision('{"kind":"research"}', ctx)
    assert len(draft.fields) == 12
    assert all(f.source_turn_id == "one" for f in draft.fields)
    plan = await ResearchProposalBuilder().build(ctx, draft)
    assert plan.max_search_queries == 9
    assert plan.target_countries == ("US",)
    assert plan.execution_mode == "research_only"


async def test_two_budget_corrections_use_latest_user_value_without_reasking():
    from agent_runtime.assistant.model_decision import model_context
    from agent_runtime.assistant.proposal import ResearchProposalBuilder

    first = turn("one").model_copy(update={"input_text": RESEARCH_INPUT})
    history = [first]
    for source, value in (("two", 6), ("three", 3)):
        history.append(turn(source).model_copy(update={"input_text": f"搜索次数={value}"}))
        ctx = context(*history)
        draft = parse_model_decision('{"kind":"research"}', ctx)
        plan = await ResearchProposalBuilder().build(ctx, draft)
        assert plan.max_search_queries == value
        field = next(f for f in draft.fields if f.name == "max_search_queries")
        assert field.source_turn_id == source
        parsed = model_context(ctx)["research_input"]
        assert parsed["missing_fields"] == []
        assert next(f for f in parsed["fields"] if f["name"] == "max_search_queries")["value"] == str(value)


async def test_research_selection_keeps_missing_conflicting_and_role_checks():
    from agent_runtime.assistant.proposal import ResearchProposalBuilder
    from shared.errors import PermissionDenied

    empty = context(turn("one").model_copy(update={"input_text": "找客户"}))
    result = parse_model_decision('{"kind":"research"}', empty)
    assert result.kind == "clarify"
    assert "max_search_queries" in result.missing_fields
    conflict = context(turn("one").model_copy(update={
        "input_text": RESEARCH_INPUT.replace("排除国家=无", "排除国家=US")}))
    result = await ResearchProposalBuilder().build(conflict, parse_model_decision('{"kind":"research"}', conflict))
    assert result.kind == "clarify" and "excluded_countries" in result.missing_fields
    sales = replace(conflict, role="sales")
    with pytest.raises(PermissionDenied):
        await ResearchProposalBuilder().build(sales, parse_model_decision('{"kind":"research"}', sales))


def test_model_cannot_supply_research_values_or_forged_origins():
    ctx = context(turn("one").model_copy(update={"input_text": RESEARCH_INPUT}))
    with pytest.raises(AssistantOutputError):
        parse_model_decision('{"kind":"research","fields":[{"name":"max_search_queries","value":"99","source_turn_id":"one"}]}', ctx)
    assert set(model_decision_schema()["$defs"]["ResearchSelection"]["properties"]) == {"kind"}


def test_oversized_research_field_does_not_break_ordinary_chat_context():
    from agent_runtime.assistant.model_decision import model_context

    invalid = turn("one").model_copy(update={"input_text": "目标=" + "甲" * 4001})
    ctx = replace(sourced_context(), turns=(invalid, turn("two")))
    assert "objective" in model_context(ctx)["research_input"]["missing_fields"]
    result = parse_model_decision('{"kind":"explain","source_indexes":[0]}', ctx)
    assert result.fragments[0].text == PRODUCT_HELP


async def test_invalid_latest_value_clears_old_field_and_can_be_corrected():
    from agent_runtime.assistant.model_decision import model_context
    from agent_runtime.assistant.proposal import ResearchProposalBuilder

    initial = turn("one").model_copy(update={"input_text": RESEARCH_INPUT})
    invalid = turn("two").model_copy(update={"input_text": "搜索次数=" + "9" * 4001})
    ctx = context(initial, invalid)
    assert "max_search_queries" in model_context(ctx)["research_input"]["missing_fields"]
    draft = parse_model_decision('{"kind":"research"}', ctx)
    result = await ResearchProposalBuilder().build(ctx, draft)
    assert result.kind == "clarify" and "max_search_queries" in result.missing_fields
    corrected = turn("three").model_copy(update={"input_text": "搜索次数=3"})
    ctx = context(initial, invalid, corrected)
    draft = parse_model_decision('{"kind":"research"}', ctx)
    plan = await ResearchProposalBuilder().build(ctx, draft)
    assert plan.max_search_queries == 3
    assert next(f for f in draft.fields if f.name == "max_search_queries").source_turn_id == "three"


def sourced_context():
    return replace(
        context(turn("one"), turn("two")),
        fragments=(AuthorizedFragment(text=PRODUCT_HELP, dependencies=(PRODUCT_REF,)),),
    )


def test_model_selects_source_code_preserves_exact_text_and_all_current_turns():
    ctx = sourced_context()
    result = validate_decision(
        parse_model_decision('{"kind":"explain","source_indexes":[0]}', ctx), ctx
    )
    assert isinstance(result, Explanation)
    assert result.fragments[0].text == PRODUCT_HELP
    assert result.fragments[0].dependencies == (PRODUCT_REF,)
    assert result.fragments[0].source_turn_ids == ("one", "two")


def test_selection_keeps_full_dependency_closure_and_requested_source_text():
    other = ObjectRef(kind="need", object_id="need_visible", version="v2")
    ctx = replace(sourced_context(), fragments=(
        *sourced_context().fragments,
        AuthorizedFragment(text="客户明确要求铰链。", dependencies=(other,)),
    ))
    result = validate_decision(
        parse_model_decision('{"kind":"explain","source_indexes":[1]}', ctx), ctx
    )
    assert result.fragments[0].text == "客户明确要求铰链。"
    assert set(result.fragments[0].dependencies) == {other, PRODUCT_REF}


@pytest.mark.parametrize("indexes", ["[-1]", "[1]", "[true]", '["0"]', "[0,0]", "[]"])
def test_invented_duplicate_or_invalid_source_selection_is_rejected(indexes):
    with pytest.raises(AssistantOutputError):
        parse_model_decision('{"kind":"explain","source_indexes":'+indexes+'}', sourced_context())


def test_freeform_generated_explanation_is_not_a_valid_new_model_output():
    with pytest.raises(AssistantOutputError) as error:
        parse_model_decision(
            '{"kind":"explain","source_indexes":[0],"text":"客户已确认采购"}',
            sourced_context(),
        )
    assert error.value.reason == "output_schema"
    assert "客户已确认采购" not in str(error.value)


def test_schema_and_clarification_keep_model_away_from_explanation_text_and_ids():
    schema = model_decision_schema()
    branch = schema["$defs"]["SourceSelection"]["properties"]
    assert set(branch) == {"kind", "source_indexes"}
    result = parse_model_decision(
        '{"kind":"clarify","questions":["研究哪个国家？"],"missing_fields":["target_countries"]}',
        sourced_context(),
    )
    assert result.kind == "clarify"


def test_legacy_decision_validator_exposes_fixed_source_reason_without_text():
    ctx = sourced_context()
    result = Explanation(fragments=(AuthorizedFragment(
        text="客户已确认采购", dependencies=(PRODUCT_REF,)),))
    with pytest.raises(AssistantOutputError) as error:
        validate_decision(result, ctx)
    assert error.value.reason == "output_excerpt"
    assert "客户已确认采购" not in str(error.value)
