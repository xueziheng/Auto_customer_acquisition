"""qualification_agent 最小边界验收（TDD RED：agent 尚为 NotImplementedError 骨架）。

边界契约（最小闭环）：
- 结构化模型调用抽象为可注入 port（ReplyModelPort）；agent 只验证受限输出并
  生成 typed ChangeSet；
- 模型输出 schema 只允许 category + candidate_fields——动作/置信度/任何其他键
  一律护栏拦截（确定性动作由域 REPLY_ACTIONS 决定，本任务不派生、不执行）；
- 提取候选必须用 demand 词表，quote 必须逐字出现在原消息（provenance）；
- agent 不落库、不执行动作、不 import infra/domains 内部实现（AST 锁定）；
- 凭证不进模型：prompt 只含消息正文。
"""

from __future__ import annotations

import ast
import json
import pathlib
from typing import Any

import pytest

from agent_runtime.base import AgentTask, ChangeSet
from shared.errors import ValidationError
from shared.schemas.identifiers import RunId, TenantId, UserId, new_id

AGENT_MODULE = (
    pathlib.Path(__file__).resolve().parents[2]
    / "agent_runtime" / "qualification_agent" / "agent.py"
)


class _FakePort:
    """显式 fake port：队列耗尽后重放末次输出（一致行为的模型）。"""

    def __init__(self, responses: list[str] | None = None) -> None:
        self._responses = list(responses or [])
        self._last: str | None = None
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def classify_reply(
        self, *, system_prompt: str, message: dict[str, str]
    ) -> str:
        self.calls.append((system_prompt, message))
        if self._responses:
            self._last = self._responses.pop(0)
        if self._last is not None:
            return self._last
        return json.dumps({"category": "auto_reply", "candidate_fields": []})


def _agent(port: _FakePort) -> Any:
    module = __import__(
        "agent_runtime.qualification_agent.agent", fromlist=["QualificationAgent"]
    )
    return module.QualificationAgent(
        model="test-model-v1", model_client=port, gateway=None, guardrails=None
    )


def _task(message: dict[str, str]) -> AgentTask:
    return AgentTask(
        tenant_id=TenantId(new_id("tn")),
        run_id=RunId(new_id("run")),
        acting_user=UserId("usr_evals"),
        objective="classify_reply",
        inputs={"message": message},
    )


def _classification_entry(changeset: ChangeSet) -> dict[str, Any] | None:
    for change in changeset.changes:
        if change.get("operation") == "record_classification":
            return change
    return None


async def test_classification_produces_typed_changeset_without_actions() -> None:
    """类别来自模型；payload 只含分类契约（无动作键——动作由域 REPLY_ACTIONS 决定）。"""
    message = {
        "message_id": "msg_boundary_1",
        "subject": "Unsubscribe",
        "body": "Please unsubscribe me from your emails.",
    }
    port = _FakePort([json.dumps({"category": "unsubscribe", "candidate_fields": []})])
    task = _task(message)
    changeset = await _agent(port).run(task, None)
    entry = _classification_entry(changeset)
    assert entry is not None
    payload = entry["payload"]
    assert payload["category"] == "unsubscribe"
    assert payload["message_id"] == "msg_boundary_1"
    assert payload["model_version"] == "test-model-v1"
    assert set(payload) == {"category", "message_id", "model_version"}
    assert entry["risk_level"] == "low"
    assert changeset.tenant_id == task.tenant_id
    assert changeset.run_id == task.run_id
    # 任何动作都不由 agent 产出/执行（apply_actions 是 workflow 后续步骤）
    assert all(change.get("operation") != "apply_actions" for change in changeset.changes)


async def test_typed_classify_result_is_the_boundary() -> None:
    """classify() 返回 typed ReplyClassificationResult（枚举类别 + 候选元组）。"""
    message = {
        "message_id": "msg_boundary_8",
        "subject": "Interested",
        "body": "Yes, we are interested in your products.",
    }
    port = _FakePort([json.dumps({"category": "clear_interest", "candidate_fields": []})])
    agent = _agent(port)
    result = await agent.classify(message=message)
    assert result.category.value == "clear_interest"
    assert result.candidate_fields == ()


async def test_auto_reply_classifies_without_stopping_semantics() -> None:
    message = {
        "message_id": "msg_boundary_2",
        "subject": "Out of office",
        "body": "Thank you for your email. I am out of the office until Friday.",
    }
    port = _FakePort([json.dumps({"category": "auto_reply", "candidate_fields": []})])
    result = await _agent(port).classify(message=message)
    assert result.category.value == "auto_reply"


async def test_model_numeric_confidence_is_intercepted() -> None:
    """硬边界 3：模型输出数值置信度 → 护栏拦截（classify 抛错、run 返回空 ChangeSet）。"""
    message = {
        "message_id": "msg_boundary_3",
        "subject": "Not interested",
        "body": "Thanks, but we are not interested in your products.",
    }
    port = _FakePort(
        [json.dumps({"category": "rejection", "confidence": 0.9, "candidate_fields": []})]
    )
    agent = _agent(port)
    with pytest.raises(ValidationError):
        await agent.classify(message=message)
    changeset = await agent.run(_task(message), None)
    assert changeset.changes == []
    assert "拦截" in changeset.summary


async def test_model_cannot_smuggle_actions_or_unknown_keys() -> None:
    """模型输出任何动作/未授权键 → 拦截（动作只能来自域 REPLY_ACTIONS，不由模型给）。"""
    message = {
        "message_id": "msg_boundary_4",
        "subject": "Hello",
        "body": "We may need hinges.",
    }
    for payload in (
        {"category": "maybe_interesting"},
        {"category": "rejection", "actions": ["stop_sequence"]},
        {"category": "rejection", "side_effect": "email.send"},
    ):
        port = _FakePort([json.dumps(payload)])
        agent = _agent(port)
        with pytest.raises(ValidationError):
            await agent.classify(message=message)
        changeset = await agent.run(_task(message), None)
        assert changeset.changes == []
        assert "拦截" in changeset.summary


async def test_extraction_candidates_require_vocabulary_and_verbatim_quote() -> None:
    """候选字段：词表内 + quote 逐字在消息中；否则该候选被拦（不落 ChangeSet）。"""
    message = {
        "message_id": "msg_boundary_5",
        "subject": "Bolt specification",
        "body": "We need 50,000 pieces of M8 x 20mm hex bolts, stainless steel 304.",
    }
    model_output = {
        "category": "provides_specification",
        "candidate_fields": [
            {"field": "quantity", "value": "50000", "quote": "50,000 pieces"},
            {"field": "size_spec", "value": "M8 x 20mm", "quote": "M8 x 20mm"},
            {"field": "quantity", "value": "999", "quote": "quote not in message"},
            {"field": "price_in_cny", "value": "100", "quote": "M8 x 20mm"},
        ],
    }
    port = _FakePort([json.dumps(model_output)])
    result = await _agent(port).classify(message=message)
    assert {(item.field, item.value) for item in result.candidate_fields} == {
        ("quantity", "50000"),
        ("size_spec", "M8 x 20mm"),
    }
    changeset = await _agent(port).run(_task(message), None)
    fields_entry = next(
        change for change in changeset.changes if change.get("operation") == "update_need_fields"
    )
    fields = fields_entry["payload"]["fields"]
    assert {(item["field"], item["value"]) for item in fields} == {
        ("quantity", "50000"),
        ("size_spec", "M8 x 20mm"),
    }


async def test_malformed_model_output_is_intercepted() -> None:
    message = {
        "message_id": "msg_boundary_6",
        "subject": "Hello",
        "body": "We may need hinges.",
    }
    for raw in ("not json at all", '["a", "b"]', '{"category": "rejection", "candidate_fields": "nope"}'):
        port = _FakePort([raw])
        agent = _agent(port)
        with pytest.raises(ValidationError):
            await agent.classify(message=message)
        changeset = await agent.run(_task(message), None)
        assert changeset.changes == []
        assert "拦截" in changeset.summary


async def test_model_never_receives_secrets_or_identifiers() -> None:
    """凭证不进模型：port 只收到系统提示与消息正文。"""
    message = {
        "message_id": "msg_boundary_7",
        "subject": "Interested",
        "body": "Yes, we are interested in your products.",
    }
    port = _FakePort()
    await _agent(port).run(_task(message), None)
    _system, received = port.calls[0]
    assert received["body"] == "Yes, we are interested in your products."
    for marker in ("secret", "password", "token", "dsn", "postgresql"):
        assert marker not in json.dumps(received).lower()
        assert marker not in _system.lower()


def test_agent_module_imports_only_allowed_layers() -> None:
    """agent 不 import infra/domains 内部实现/外部 SDK（依赖白名单 + 无 DB 路径）。"""
    tree = ast.parse(AGENT_MODULE.read_text(encoding="utf-8"))
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    forbidden = (
        "infra.",
        "domains.conversations.models",
        "domains.conversations.repository",
        "sqlalchemy",
        "httpx",
        "openai",
        "asyncpg",
        "tool_gateway.handlers",
    )
    for module in imports:
        assert not module.startswith(forbidden), f"agent 越权 import：{module}"
