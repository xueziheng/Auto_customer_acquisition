"""公开证据与适配推断分开，企业法人和职务邮箱不可默认为真。"""
import pytest

from shared.errors import ValidationError


@pytest.mark.parametrize("failure", [None, "email", "legal", "quote", "fit"])
def test_observation_requires_exact_public_evidence(failure):
    from domains.prospecting.agent_profile import AgentPreparationObservation
    observation = AgentPreparationObservation.model_validate({
        "entity_type": {"value": "distributor", "quote": "We distribute hinges"},
        "industry": {"value": "hardware", "quote": "hinges"},
        "subject_type": "legal_entity", "legal_entity_quote": "missing" if failure == "legal" else "Example Ltd",
        "public_email": "joe@example.test" if failure == "email" else "sales@example.test",
        "email_quote": "email us elsewhere" if failure == "quote" else "Sales sales@example.test",
        "fit_quote": "We distribute hinges", "fit_reasoning": "推断：产品类别与确认资料相符，不代表客户采购意愿",
        "matched": failure != "fit", "category": "hinges",
    })
    text = "Example Ltd. We distribute hinges. Sales sales@example.test."
    if failure:
        with pytest.raises(ValidationError):
            observation.require_evidence(text, "example.test", ("hinges",))
    else:
        observation.require_evidence(text, "example.test", ("hinges",))
