"""DemandIntelligenceAgent 的证据、推断与模型输入验收。"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from agent_runtime.base import AgentTask
from agent_runtime.demand_intelligence.agent import DemandIntelligenceAgent
from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
from shared.schemas.identifiers import RunId, TenantId, UserId, new_id

NOW = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)
HASH = "a" * 64
ARTIFACT = "art_01K3H0T8NBWM3KGT9XQ06YRC5V"
FACT = "Acme opened a new outdoor furniture production line."


class _CapturingModel:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    async def analyze_pages(
        self, *, system_prompt: str, discovery: dict[str, object]
    ) -> str:
        self.calls.append({"system_prompt": system_prompt, "discovery": discovery})
        return json.dumps(self.response)


def _task(*, text: str = FACT) -> AgentTask:
    return AgentTask(
        TenantId(new_id("tn")),
        RunId(new_id("run")),
        UserId("employee:manager"),
        "探索公开需求信号",
        inputs={
            "pages": (
                {
                    "text": text,
                    "url": "https://example.com/news",
                    "observed_at": NOW,
                    "content_hash": HASH,
                    "snapshot_artifact_ref": ARTIFACT,
                },
            ),
            "target_countries": ("US",),
            "target_categories": ("industrial hinges",),
            "excluded_countries": (),
            "excluded_categories": (),
            "max_signals": 2,
            "max_hypotheses": 2,
            "strategy_group": "company_change",
        },
    )


def _response(*, reasoning: str = "企业扩产，可能需要工业铰链，值得验证") -> dict[str, object]:
    return {
        "signals": [
            {
                "signal_type": "product_line_expansion",
                "entity_name": "Acme",
                "source_page_index": 0,
                "source_excerpt": FACT,
                "possible_need": "industrial hinges",
                "evidence_level": "public_company_event",
            }
        ],
        "hypotheses": [
            {
                "entity_name": "Acme",
                "country": "US",
                "website_domain": "example.com",
                "category": "industrial hinges",
                "reasoning": reasoning,
                "signal_indexes": [0],
            }
        ],
    }


async def test_signal_carries_complete_snapshot_tuple_and_separates_inference() -> None:
    model = _CapturingModel(_response())
    agent = DemandIntelligenceAgent(
        "model-v1", model, object(), CredentialMarkerGuard()
    )

    result = await agent.run(_task(), None)

    signal = result.changes[0]["payload"]
    hypothesis = result.changes[1]["payload"]
    assert (
        signal["source_url"],
        signal["observed_at"],
        signal["page_hash"],
        signal["snapshot_artifact_ref"],
    ) == ("https://example.com/news", NOW.isoformat(), HASH, ARTIFACT)
    assert signal["raw_observation"] == FACT
    assert signal["possible_need"] == "industrial hinges"
    assert hypothesis["signal_indexes"] == (0,)
    assert "reasoning" not in signal
    assert "raw_observation" not in hypothesis


async def test_hypothesis_without_visible_signal_evidence_is_rejected() -> None:
    response = _response()
    response["hypotheses"][0]["signal_indexes"] = []
    model = _CapturingModel(response)
    agent = DemandIntelligenceAgent(
        "model-v1", model, object(), CredentialMarkerGuard()
    )

    result = await agent.run(_task(), None)

    assert result.changes == []
    assert "模型输出被护栏拦截" in result.summary


@pytest.mark.parametrize(
    "reasoning",
    [
        "有 82% 概率需要工业铰链，可能采购，值得验证",
        "概率为82%，可能需要工业铰链，值得验证",
        "可能性为 0.82，可能需要工业铰链，值得验证",
        "概率为８２％，可能需要工业铰链，值得验证",
        "There is an 82% chance of buying industrial hinges，可能采购，值得验证",
        "The chance 82% suggests demand for industrial hinges，可能采购，值得验证",
        "The buyer is 82% likely to need industrial hinges，可能采购，值得验证",
        "Likely 82% to need industrial hinges，可能采购，值得验证",
    ],
)
async def test_numeric_confidence_variants_in_inference_are_rejected(
    reasoning: str,
) -> None:
    model = _CapturingModel(_response(reasoning=reasoning))
    agent = DemandIntelligenceAgent(
        "model-v1", model, object(), CredentialMarkerGuard()
    )

    result = await agent.run(_task(), None)

    assert result.changes == []
    assert "概率" in result.summary


async def test_page_contact_pii_is_redacted_before_model_input() -> None:
    model = _CapturingModel({"signals": [], "hypotheses": []})
    agent = DemandIntelligenceAgent(
        "model-v1", model, object(), CredentialMarkerGuard()
    )

    await agent.run(
        _task(text=f"{FACT} Contact alice@example.com or +1 212 555 0199."),
        None,
    )

    model_blob = json.dumps(model.calls[0], ensure_ascii=False)
    assert "alice@example.com" not in model_blob
    assert "212 555" not in model_blob
    assert model_blob.count("[CONTACT_REDACTED]") == 2
