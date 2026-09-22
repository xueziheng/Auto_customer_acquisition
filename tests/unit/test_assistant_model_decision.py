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
