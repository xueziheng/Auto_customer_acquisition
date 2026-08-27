"""新研究提案无需触达授权，旧缺省仍是触达准备。"""

import json
from dataclasses import asdict

import pytest

from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
from agent_runtime.trade_manager.agent import TradeManagerAgent
from shared.errors import ValidationError
from tests.unit.workflows.test_research_discovery import research_plan


class Model:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    async def parse_discovery_directive(self, **kwargs):
        self.calls += 1
        return json.dumps(self.payload)


def payload():
    plan = asdict(research_plan())
    for key in ("campaign_id", "role_hints", "assessment_ref"):
        plan.pop(key)
    return {
        "plan": plan,
        "interpretation_summary": "仅研究三线路",
        "no_auto_send": True,
        "expected_behavior_changes": ["只保存公开证据，不补全联系人、不发送、不报价"],
    }


async def test_research_proposal_decodes_without_campaign_or_contact_roles():
    agent = TradeManagerAgent(
        "model-v2", Model(payload()), None, CredentialMarkerGuard()
    )
    draft = await agent.propose_discovery(
        "仅研究美国铰链，三条查询，读取两页，最多1条信号与1条假设，门槛low_mid"
    )
    assert draft.plan.execution_mode == "research_only"
    assert draft.plan.campaign_id == ""
    assert {q.discovery_lane for q in draft.plan.queries} == {
        "importer",
        "distributor",
        "ecommerce",
    }


async def test_missing_execution_mode_does_not_silently_become_research():
    data = payload()
    data["plan"].pop("execution_mode")
    agent = TradeManagerAgent("model-v2", Model(data), None, CredentialMarkerGuard())
    with pytest.raises(ValidationError):
        await agent.propose_discovery("解析旧需求探索指令")


@pytest.mark.parametrize("mode", ["research_only", "outreach_preparation"])
@pytest.mark.parametrize("missing_lane", [None, "omit"])
async def test_new_model_query_requires_explicit_lane_in_every_mode(mode, missing_lane):
    data = payload()
    data["plan"].update(
        execution_mode=mode,
        campaign_id="cmp_01K3H0T8NBWM3KGT9XQ06YRC5V",
        role_hints=["purchasing"],
        assessment_ref="assessment:approved",
    )
    if missing_lane == "omit":
        data["plan"]["queries"][0].pop("discovery_lane")
    else:
        data["plan"]["queries"][0]["discovery_lane"] = None
    agent = TradeManagerAgent("model-v2", Model(data), None, CredentialMarkerGuard())
    with pytest.raises(ValidationError):
        await agent.propose_discovery("生成新需求探索提案")


async def test_tavily_shaped_input_is_rejected_before_trade_manager_model():
    synthetic = "tvly-" + "0" * 32
    model = Model(payload())
    agent = TradeManagerAgent("model-v2", model, None, CredentialMarkerGuard())
    with pytest.raises(ValidationError) as error:
        await agent.propose_discovery("只研究。" + synthetic)
    assert model.calls == 0
    assert synthetic not in str(error.value)


@pytest.mark.parametrize("in_subject", [False, True])
def test_tavily_shape_is_rejected_by_shared_guard_without_echo(in_subject):
    synthetic = "tvly-" + "0" * 32
    with pytest.raises(ValidationError) as error:
        CredentialMarkerGuard().check(
            subject=synthetic if in_subject else None,
            body="公开企业描述" if in_subject else synthetic,
        )
    assert synthetic not in str(error.value)


async def test_tavily_shaped_web_text_never_reaches_demand_model():
    from agent_runtime.demand_intelligence.agent import DemandIntelligenceAgent
    from tests.unit.agent_runtime.test_demand_intelligence_agent import (
        _CapturingModel,
        _response,
        _task,
    )

    model = _CapturingModel(_response())
    synthetic = "tvly-" + "0" * 32
    result = await DemandIntelligenceAgent(
        "model-v2", model, None, CredentialMarkerGuard()
    ).run(_task(text="公开网页误贴：" + synthetic), None)
    assert model.calls == []
    assert result.changes == []
    assert synthetic not in result.summary
