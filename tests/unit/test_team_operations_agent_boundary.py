"""TeamOperationsAgent 的证据链、时间与人工确认边界。"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agent_runtime.base import AgentTask
from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
from shared.schemas.identifiers import RunId, TenantId, UserId, new_id


class _ExtractionPort:
    def __init__(self, response: object) -> None:
        self._response = response

    async def extract_work(
        self, *, system_prompt: str, upload: dict[str, object]
    ) -> str:
        del system_prompt, upload
        return self._response  # type: ignore[return-value]


class _MustNotRunPort:
    async def extract_work(
        self, *, system_prompt: str, upload: dict[str, object]
    ) -> str:
        del system_prompt, upload
        raise AssertionError("含凭证输入不得进入模型")


def _task(*, content: str | None = None) -> AgentTask:
    return AgentTask(
        tenant_id=TenantId(new_id("tn")),
        run_id=RunId(new_id("run")),
        acting_user=UserId(new_id("usr")),
        objective="extract_employee_work_upload",
        inputs={
            "work_upload": {
                "upload_id": "upload-one",
                "artifact_ref": new_id("art"),
                "source_message_id": "message-one",
                "employee_id": "employee-one",
                "account_id": "account-one",
                "opportunity_id": "opportunity-one",
                "need_id": "need-one",
                "source_kind": "chat_transcript",
                "source_content": content
                or (
                    "Customer: We need 5000 stainless hinges. "
                    "We will confirm the final quantity tomorrow."
                ),
                "occurred_at": "2026-08-21T15:00:00-07:00",
                "customer_timezone": "America/Los_Angeles",
            }
        },
    )


def _agent(port: Any) -> Any:
    module = __import__(
        "agent_runtime.team_operations.agent",
        fromlist=["TeamOperationsAgent"],
    )
    return module.TeamOperationsAgent(
        model="test-model-v1",
        model_client=port,
        gateway=None,
        guardrails=CredentialMarkerGuard(),
    )


def _safe_response() -> dict[str, object]:
    return {
        "facts": [
            {
                "fact_type": "customer_statement",
                "value": "The customer needs stainless hinges.",
                "evidence_quote": "We need 5000 stainless hinges.",
            }
        ],
        "need_field_updates": [
            {
                "field_name": "quantity",
                "value": "5000",
                "evidence_quote": "We need 5000 stainless hinges.",
            }
        ],
        "commitments": [
            {
                "commitment_type": "customer",
                "action": "confirm the final quantity",
                "due_at": "2026-08-22T23:59:00-07:00",
                "due_at_uncertain": False,
                "verbatim": "We will confirm the final quantity tomorrow.",
            }
        ],
        "progress_note": {
            "summary": "The customer stated a need and a next-step commitment.",
            "evidence_quotes": [
                "We need 5000 stainless hinges.",
                "We will confirm the final quantity tomorrow.",
            ],
        },
    }


@pytest.mark.asyncio
async def test_extraction_is_evidence_bound_and_pending_confirmation() -> None:
    result = await _agent(_ExtractionPort(json.dumps(_safe_response()))).run(
        _task(), None
    )

    assert result.guardrail_violations == []
    assert [change["operation"] for change in result.changes] == [
        "extract_facts",
        "update_need_fields",
        "create_commitment",
        "progress_note",
    ]
    for change in result.changes:
        assert change["payload"]["confirmation_status"] == "pending"
        assert change["payload"]["confirmed_by"] is None
        assert change["payload"]["artifact_ref"].startswith("art_")
        assert change["payload"]["source_message_id"] == "message-one"
    fact = result.changes[0]["payload"]["facts"][0]
    assert fact["provenance"] == {
        "source_type": "upload",
        "source_id": result.changes[0]["payload"]["artifact_ref"],
        "source_message_id": "message-one",
        "evidence_quote": "We need 5000 stainless hinges.",
        "extracted_by": "test-model-v1",
        "confirmed_by": None,
        "confirmed_at": None,
    }
    need_change = result.changes[1]
    assert need_change["payload"]["fields"] == (
        {
            "name": "quantity",
            "value": "5000",
            "quote": "We need 5000 stainless hinges.",
        },
    )
    commitment = result.changes[2]["payload"]
    assert commitment["due_at"] == "2026-08-22T23:59:00-07:00"
    assert commitment["due_at_uncertain"] is False
    assert "source_content" not in json.dumps(result.changes)


@pytest.mark.asyncio
async def test_evidence_quotes_must_be_verbatim_from_original_upload() -> None:
    response = _safe_response()
    response["facts"] = [
        {
            "fact_type": "customer_statement",
            "value": "Invented fact",
            "evidence_quote": "This sentence is not in the upload.",
        }
    ]

    result = await _agent(_ExtractionPort(json.dumps(response))).run(_task(), None)

    assert result.changes == []
    assert result.summary == "模型输出被护栏拦截：员工工作提取证据不是原文"


@pytest.mark.asyncio
async def test_commitment_due_time_must_be_absolute_and_timezone_aware() -> None:
    response = _safe_response()
    response["commitments"][0]["due_at"] = "tomorrow"  # type: ignore[index]

    result = await _agent(_ExtractionPort(json.dumps(response))).run(_task(), None)

    assert result.changes == []
    assert result.summary == "模型输出被护栏拦截：承诺到期时间必须是绝对时间"


@pytest.mark.asyncio
async def test_need_fields_require_an_existing_need_reference() -> None:
    task = _task()
    task.inputs["work_upload"]["need_id"] = None

    result = await _agent(_ExtractionPort(json.dumps(_safe_response()))).run(
        task, None
    )

    assert result.changes == []
    assert result.summary == "模型输出被护栏拦截：需求字段提取缺少需求引用"


@pytest.mark.asyncio
async def test_unknown_output_fields_are_rejected() -> None:
    response = _safe_response()
    response["confidence"] = 0.93

    result = await _agent(_ExtractionPort(json.dumps(response))).run(_task(), None)

    assert result.changes == []
    assert result.summary == "模型输出被护栏拦截：员工工作模型输出含未授权字段"


@pytest.mark.asyncio
async def test_credentials_are_rejected_before_model_call() -> None:
    result = await _agent(_MustNotRunPort()).run(
        _task(content="Forwarded authorization: bearer secret-value"), None
    )

    assert result.changes == []
    assert result.summary == "员工工作输入被安全边界拒绝"
