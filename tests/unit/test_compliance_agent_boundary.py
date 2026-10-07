"""ComplianceAgent 是带证据的事后巡检员，不是业务写入闸门。"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agent_runtime.base import AgentTask
from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
from shared.schemas.identifiers import RunId, TenantId, UserId, new_id


class _AuditPort:
    def __init__(self, response: object) -> None:
        self._response = response

    async def inspect_samples(
        self, *, system_prompt: str, audit_scope: dict[str, object]
    ) -> str:
        del system_prompt, audit_scope
        return self._response  # type: ignore[return-value]


class _MustNotRunPort:
    async def inspect_samples(
        self, *, system_prompt: str, audit_scope: dict[str, object]
    ) -> str:
        del system_prompt, audit_scope
        raise AssertionError("含凭证样本不得进入模型")


def _task(*, content: str = "The price is USD 2.50.") -> AgentTask:
    return AgentTask(
        tenant_id=TenantId(new_id("tn")),
        run_id=RunId(new_id("run")),
        acting_user=UserId(new_id("usr")),
        objective="inspect_compliance_samples",
        inputs={
            "audit_scope": {
                "policy_version": "phase1-us-2026-08",
                "owner_ref": "employee-review-owner",
                "checks": ("unapproved_commitment", "provenance_required"),
                "samples": (
                    {
                        "sample_id": "sent-message-one",
                        "kind": "sent_message",
                        "content": content,
                        "occurred_at": "2026-08-21T12:00:00+00:00",
                        "source_artifact_ref": "artifact-message-one",
                    },
                ),
            }
        },
    )


def _agent(port: Any) -> Any:
    module = __import__(
        "agent_runtime.compliance_agent.agent",
        fromlist=["ComplianceAgent"],
    )
    return module.ComplianceAgent(
        model="test-model-v1",
        model_client=port,
        gateway=None,
        guardrails=CredentialMarkerGuard(),
    )


def _finding(**overrides: object) -> str:
    finding = {
        "sample_id": "sent-message-one",
        "check_id": "unapproved_commitment",
        "severity": "critical",
        "summary": "The sent message contains a concrete unapproved price.",
        "evidence_quote": "The price is USD 2.50.",
        "recommended_action": "Notify the owner and review the approval trail.",
    }
    finding.update(overrides)
    return json.dumps({"findings": [finding]})


@pytest.mark.asyncio
async def test_verbatim_finding_becomes_notification_only_changeset() -> None:
    task = _task()
    result = await _agent(_AuditPort(_finding())).run(task, None)

    assert result.guardrail_violations == []
    assert len(result.changes) == 1
    change = result.changes[0]
    assert change["domain"] == "compliance"
    assert change["operation"] == "compliance_finding"
    assert change["risk_level"] == "low"
    assert change["payload"] == {
        "sample_id": "sent-message-one",
        "sample_kind": "sent_message",
        "check_id": "unapproved_commitment",
        "severity": "critical",
        "summary": "The sent message contains a concrete unapproved price.",
        "evidence_quote": "The price is USD 2.50.",
        "source_artifact_ref": "artifact-message-one",
        "occurred_at": "2026-08-21T12:00:00+00:00",
        "policy_version": "phase1-us-2026-08",
        "owner_ref": "employee-review-owner",
        "recommended_action": "Notify the owner and review the approval trail.",
        "generated_by": "test-model-v1",
    }


@pytest.mark.asyncio
async def test_finding_must_reference_allowed_check_and_verbatim_evidence() -> None:
    wrong_check = await _agent(
        _AuditPort(_finding(check_id="delete_everything"))
    ).run(_task(), None)
    invented_quote = await _agent(
        _AuditPort(_finding(evidence_quote="A quote not present in the sample."))
    ).run(_task(), None)

    assert wrong_check.changes == []
    assert wrong_check.summary == "模型输出被护栏拦截：合规发现引用越界"
    assert invented_quote.changes == []
    assert invented_quote.summary == "模型输出被护栏拦截：合规发现证据不是原文"


@pytest.mark.asyncio
async def test_model_cannot_smuggle_business_action_into_finding() -> None:
    raw = json.loads(_finding())
    raw["findings"][0]["action"] = "email.send"

    result = await _agent(_AuditPort(json.dumps(raw))).run(_task(), None)

    assert result.changes == []
    assert result.summary == "模型输出被护栏拦截：合规巡检模型输出含未授权字段"


@pytest.mark.asyncio
async def test_default_guardrail_blocks_numeric_confidence_in_finding_text() -> None:
    result = await _agent(
        _AuditPort(_finding(summary="The confidence is 0.91."))
    ).run(_task(), None)

    assert result.changes == []
    assert result.summary == "模型输出被 Phase 1 护栏拦截"
    assert {item["rail"] for item in result.guardrail_violations} == {
        "no_probability_output"
    }


@pytest.mark.asyncio
async def test_compliance_credentials_are_rejected_before_model_call() -> None:
    result = await _agent(_MustNotRunPort()).run(
        _task(content="Authorization: Bearer secret-value"),
        None,
    )

    assert result.changes == []
    assert result.summary == "合规巡检输入被安全边界拒绝"
