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


def _task(
    *,
    organization_name: str = "Apple",
    category: str = "五金",
    website_domain: str = "apple.com",
    account_id: str | None = None,
    signal_id: str | None = None,
) -> AgentTask:
    source_ref = signal_id or new_id("sig")
    return AgentTask(
        tenant_id=TenantId(new_id("tn")),
        run_id=RunId(new_id("run")),
        acting_user=UserId("employee:manager"),
        objective="寻找企业公开官网",
        inputs={
            "hypothesis_id": new_id("hyp"),
            "organization": {
                "account_id": account_id or new_id("acc"),
                "entity_name": organization_name,
                "country": "US",
                "website_domain": website_domain,
            },
            "category": category,
            "source_signal_refs": [source_ref],
            "allowed_countries": ("US",),
        },
    )


@pytest.mark.parametrize(
    ("organization_name", "category", "website_domain"),
    [
        ("Apple", "五金", "apple.com"),
        ("Google", "铰链", "google.com"),
        ("General Electric", "industrial hinges", "ge.com"),
        ("华为", "五金", "huawei.com"),
    ],
)
async def test_structured_organization_projection_avoids_name_false_positives(
    organization_name: str,
    category: str,
    website_domain: str,
) -> None:
    signal_id = new_id("sig")
    model = _CapturingModel(
        {
            "evidence_sufficient": True,
            "source_signal_refs": [signal_id],
        }
    )
    agent = AccountDiscoveryAgent(
        "model-v1", model, object(), CredentialMarkerGuard()
    )

    task = _task(
        organization_name=organization_name,
        category=category,
        website_domain=website_domain,
        signal_id=signal_id,
    )
    result = await agent.run(task, None)

    assert len(model.calls) == 1
    assert model.calls[0]["hypothesis"] == {
        "hypothesis_id": task.inputs["hypothesis_id"],
        "organization": {
            "account_id": task.inputs["organization"]["account_id"],
            "entity_name": organization_name,
            "country": "US",
            "website_domain": website_domain,
        },
        "category": category,
        "source_signal_refs": (signal_id,),
    }
    payload = result.changes[0]["payload"]
    assert payload == {
        "account_id": task.inputs["organization"]["account_id"],
        "entity_name": organization_name,
        "country": "US",
        "website_domain": website_domain,
        "source_signal_refs": (signal_id,),
    }


async def test_credential_marker_stops_before_account_model() -> None:
    model = _CapturingModel()
    agent = AccountDiscoveryAgent(
        "model-v1", model, object(), CredentialMarkerGuard()
    )

    result = await agent.run(
        _task(category="api_key: synthetic-secret-marker"),
        None,
    )

    assert result.changes == []
    assert model.calls == []
    assert "安全边界拒绝" in result.summary


async def test_account_output_is_canonical_public_fact_without_model_free_text() -> None:
    signal_id = new_id("sig")
    model = _CapturingModel(
        {
            "evidence_sufficient": True,
            "source_signal_refs": [signal_id],
        }
    )
    task = _task(
        organization_name="General Electric",
        category="铰链",
        website_domain="example.com",
        signal_id=signal_id,
    )
    agent = AccountDiscoveryAgent(
        "model-v1", model, object(), CredentialMarkerGuard()
    )

    result = await agent.run(task, None)

    assert len(result.changes) == 1
    payload = result.changes[0]["payload"]
    assert payload["entity_name"] == "General Electric"
    assert payload["website_domain"] == "example.com"
    assert not ({"email", "phone", "full_name", "confidence"} & set(payload))


@pytest.mark.parametrize(
    "unsafe_extra",
    [
        {"entity_name": "alice"},
        {"entity_name": "ALICE SMITH"},
        {"source_url": "https://example.com/people/Alice-SMITH"},
        {"industry": "alice@example.com"},
        {"size_hint": "api_key: synthetic-secret-marker"},
        {"analysis": "82% chance"},
        {"next_action": "send email now"},
    ],
)
async def test_every_unsafe_model_output_string_is_rejected_before_changeset(
    unsafe_extra: dict[str, str],
) -> None:
    signal_id = new_id("sig")
    response: dict[str, object] = {
        "evidence_sufficient": True,
        "source_signal_refs": [signal_id],
    }
    response.update(unsafe_extra)
    model = _CapturingModel(response)
    task = _task(signal_id=signal_id)
    agent = AccountDiscoveryAgent(
        "model-v1", model, object(), CredentialMarkerGuard()
    )

    result = await agent.run(task, None)

    assert len(model.calls) == 1
    assert result.changes == []
    assert "护栏拦截" in result.summary


async def test_model_cannot_redirect_or_merge_trusted_account_identity() -> None:
    signal_id = new_id("sig")
    account_id = new_id("acc")
    model = _CapturingModel(
        {
            "evidence_sufficient": True,
            "website_domain": "google.com",
            "source_signal_refs": [signal_id],
        }
    )
    agent = AccountDiscoveryAgent(
        "model-v1", model, object(), CredentialMarkerGuard()
    )

    result = await agent.run(
        _task(
            organization_name="Apple",
            website_domain="apple.com",
            account_id=account_id,
            signal_id=signal_id,
        ),
        None,
    )

    assert model.calls[0]["hypothesis"]["organization"] == {
        "account_id": account_id,
        "entity_name": "Apple",
        "country": "US",
        "website_domain": "apple.com",
    }
    assert result.changes == []
    assert "未授权字段" in result.summary
