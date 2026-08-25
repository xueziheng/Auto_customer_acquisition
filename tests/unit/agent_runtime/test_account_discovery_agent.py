"""AccountDiscoveryAgent 的模型隔离与企业事实边界验收。"""

from __future__ import annotations

import json

import pytest

from agent_runtime.account_discovery.agent import AccountDiscoveryAgent
from agent_runtime.base import AgentTask
from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
from shared.schemas.identifiers import RunId, TenantId, UserId, new_id


class _CapturingModel:
    def __init__(self, response: dict[str, object] | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self._response = response or {}

    async def discover_account(
        self, *, system_prompt: str, hypothesis: dict[str, object]
    ) -> str:
        self.calls.append(
            {"system_prompt": system_prompt, "hypothesis": hypothesis}
        )
        return json.dumps(self._response)


def _task(*, summary: str, reasoning: str = "企业扩建，可能需要五金") -> AgentTask:
    signal_id = new_id("sig")
    return AgentTask(
        tenant_id=TenantId(new_id("tn")),
        run_id=RunId(new_id("run")),
        acting_user=UserId("employee:manager"),
        objective="寻找企业公开官网",
        inputs={
            "hypothesis": {
                "hypothesis_id": new_id("hyp"),
                "category": "industrial hinges",
                "reasoning": reasoning,
                "evidence": [
                    {
                        "signal_id": signal_id,
                        "summary": summary,
                        "source_url": "https://example.com/news",
                    }
                ],
                "source_signal_refs": [signal_id],
            },
            "allowed_countries": ("US",),
        },
    )


@pytest.mark.parametrize(
    "summary",
    [
        "Contact Alice at alice@example.com or +1 (212) 555-0199.",
        "Alice Buyer confirmed Acme Manufacturing expanded.",
        "Alice Smith confirmed Acme Manufacturing expanded.",
        "Alice confirmed Acme Manufacturing expanded.",
        "张三表示 Acme Manufacturing 已经扩建。",
        "张三",
    ],
)
async def test_contact_pii_context_is_rejected_before_account_model_input(
    summary: str,
) -> None:
    model = _CapturingModel()
    agent = AccountDiscoveryAgent(
        "model-v1", model, object(), CredentialMarkerGuard()
    )

    result = await agent.run(
        _task(summary=summary),
        None,
    )

    assert result.changes == []
    assert model.calls == []


async def test_credential_marker_stops_before_account_model() -> None:
    model = _CapturingModel()
    agent = AccountDiscoveryAgent(
        "model-v1", model, object(), CredentialMarkerGuard()
    )

    result = await agent.run(
        _task(summary="Public notice", reasoning="api_key: synthetic-secret-marker"),
        None,
    )

    assert result.changes == []
    assert model.calls == []
    assert "安全边界拒绝" in result.summary


async def test_account_output_is_canonical_public_fact_without_contact_fields() -> None:
    signal_id = new_id("sig")
    model = _CapturingModel(
        {
            "entity_name": "Acme Manufacturing",
            "country": "US",
            "website_domain": "EXAMPLE.COM.",
            "entity_type": "manufacturer",
            "industry": "hardware",
            "size_hint": None,
            "source_signal_refs": [signal_id],
        }
    )
    task = _task(summary="Acme Manufacturing opened a new factory.")
    task.inputs["hypothesis"]["source_signal_refs"] = [signal_id]
    task.inputs["hypothesis"]["evidence"][0]["signal_id"] = signal_id
    agent = AccountDiscoveryAgent(
        "model-v1", model, object(), CredentialMarkerGuard()
    )

    result = await agent.run(task, None)

    assert len(result.changes) == 1
    payload = result.changes[0]["payload"]
    assert payload["website_domain"] == "example.com"
    assert not ({"email", "phone", "full_name", "confidence"} & set(payload))


@pytest.mark.parametrize(
    ("field", "unsafe_value"),
    [
        ("entity_name", "Alice Buyer"),
        ("industry", "Alice Smith"),
        ("industry", "张三经理"),
        ("industry", "alice@example.com"),
        ("industry", "+1 (212) 555-0199"),
        ("size_hint", "api_key: synthetic-secret-marker"),
        ("size_hint", "概率为82%"),
        ("industry", "send email now"),
    ],
)
async def test_every_unsafe_model_output_string_is_rejected_before_changeset(
    field: str,
    unsafe_value: str,
) -> None:
    signal_id = new_id("sig")
    response: dict[str, object] = {
        "entity_name": "Acme Manufacturing",
        "country": "US",
        "website_domain": "example.com",
        "entity_type": "manufacturer",
        "industry": "hardware",
        "size_hint": None,
        "source_signal_refs": [signal_id],
    }
    response[field] = unsafe_value
    model = _CapturingModel(response)
    task = _task(summary="Acme Manufacturing opened a new factory.")
    task.inputs["hypothesis"]["source_signal_refs"] = [signal_id]
    task.inputs["hypothesis"]["evidence"][0]["signal_id"] = signal_id
    agent = AccountDiscoveryAgent(
        "model-v1", model, object(), CredentialMarkerGuard()
    )

    result = await agent.run(task, None)

    assert result.changes == []
    assert "护栏拦截" in result.summary
