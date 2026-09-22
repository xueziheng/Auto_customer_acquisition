"""已交付提问/提案不占执行槽，浏览器不能自填权限。"""

import pytest
from pydantic import ValidationError


def test_browser_cannot_supply_authority():
    from domains.assistant.schemas import TurnInput

    with pytest.raises(ValidationError):
        TurnInput.model_validate(
            {
                "text": "查我的机会",
                "object_refs": [],
                "idempotency_key": "one",
                "role": "boss",
            }
        )


def test_research_fields_require_exactly_one_source():
    from domains.assistant.schemas import SourcedField

    for values in ({}, {"source_turn_id": "atr_test", "policy_version": "v1"}):
        with pytest.raises(ValidationError):
            SourcedField(name="target_countries", value="US", **values)
