"""OutreachAgent 的草稿职责、输入隔离与输出护栏合同。"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agent_runtime.base import AgentTask
from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
from shared.schemas.identifiers import RunId, TenantId, UserId, new_id


class _DraftPort:
    def __init__(self, response: object) -> None:
        self._response = response

    async def draft_outreach(
        self, *, system_prompt: str, request: dict[str, object]
    ) -> str:
        del system_prompt, request
        return self._response  # type: ignore[return-value]


class _MustNotRunPort:
    async def draft_outreach(
        self, *, system_prompt: str, request: dict[str, object]
    ) -> str:
        del system_prompt, request
        raise AssertionError("含凭证输入不得进入模型")


def _task(
    *,
    fact: str = "The company website lists a new outdoor furniture range.",
    source_url: str = "https://northstar.example/range",
) -> AgentTask:
    return AgentTask(
        tenant_id=TenantId(new_id("tn")),
        run_id=RunId(new_id("run")),
        acting_user=UserId(new_id("usr")),
        objective="draft_discovery_outreach",
        inputs={
            "draft_request": {
                "account_name": "Northstar Retail",
                "intent": "discovery",
                "target_language": "en-US",
                "evidence": (
                    {
                        "source_id": "page-2026-08-21",
                        "source_url": source_url,
                        "fact": fact,
                    },
                ),
                "unsubscribe_text": "If you prefer not to hear from us, please reply unsubscribe.",
            }
        },
    )


def _agent(port: Any) -> Any:
    module = __import__(
        "agent_runtime.outreach_agent.agent",
        fromlist=["OutreachAgent"],
    )
    return module.OutreachAgent(
        model="test-model-v1",
        model_client=port,
        gateway=None,
        guardrails=CredentialMarkerGuard(),
    )


@pytest.mark.asyncio
async def test_safe_discovery_output_becomes_draft_only_changeset() -> None:
    unsubscribe = "If you prefer not to hear from us, please reply unsubscribe."
    port = _DraftPort(
        json.dumps(
            {
                "subject": "A quick question about your sourcing needs",
                "body": (
                    "I noticed that your website lists a new outdoor furniture range. "
                    "Which components, packaging, or supply areas are currently hardest "
                    f"for your team to source? {unsubscribe}"
                ),
                "content_language": "en",
            }
        )
    )

    task = _task()
    result = await _agent(port).run(task, None)

    assert result.guardrail_violations == []
    assert len(result.changes) == 1
    change = result.changes[0]
    assert change["domain"] == "outreach"
    assert change["operation"] == "create_draft"
    assert change["risk_level"] == "medium"
    assert set(change["payload"]) == {
        "subject",
        "body",
        "target_language",
        "content_language",
        "source_evidence_refs",
        "generated_by",
    }
    assert change["payload"]["source_evidence_refs"] == ("page-2026-08-21",)
    assert all(item["operation"] != "send" for item in result.changes)


@pytest.mark.asyncio
async def test_model_cannot_smuggle_an_action_into_draft_output() -> None:
    result = await _agent(
        _DraftPort(
            json.dumps(
                {
                    "subject": "Hello",
                    "body": "A safe body.",
                    "content_language": "en",
                    "action": "email.send",
                }
            )
        )
    ).run(_task(), None)

    assert result.changes == []
    assert result.summary == "模型输出被护栏拦截：外联草稿模型输出含未授权字段"


@pytest.mark.asyncio
async def test_phase1_guardrails_structurally_block_customer_commitments() -> None:
    unsubscribe = "If you prefer not to hear from us, please reply unsubscribe."
    result = await _agent(
        _DraftPort(
            json.dumps(
                {
                    "subject": "Quotation update",
                    "body": (
                        "The price is USD 2.50 and delivery will be within 10 days. "
                        f"{unsubscribe}"
                    ),
                    "content_language": "en",
                }
            )
        )
    ).run(_task(), None)

    assert result.changes == []
    assert result.summary == "模型输出被 Phase 1 护栏拦截"
    assert {item["rail"] for item in result.guardrail_violations} == {
        "no_forbidden_commitment"
    }


@pytest.mark.asyncio
async def test_credential_markers_are_rejected_before_model_call() -> None:
    result = await _agent(_MustNotRunPort()).run(
        _task(fact="The forwarded page contains API token abc123."),
        None,
    )

    assert result.changes == []
    assert result.summary == "外联输入被安全边界拒绝"


@pytest.mark.asyncio
async def test_source_url_cannot_carry_query_material_into_model() -> None:
    result = await _agent(_MustNotRunPort()).run(
        _task(source_url="https://northstar.example/range?opaque=value"),
        None,
    )

    assert result.changes == []
    assert result.summary == "外联输入被安全边界拒绝"
