"""qualification_agent 最小边界验收（TDD RED：agent 尚为 NotImplementedError 骨架）。

边界契约（最小闭环）：
- 结构化模型调用抽象为可注入 port（ReplyModelPort）；agent 只验证受限输出并
  生成 typed ChangeSet；
- 模型输出 schema 只允许 category + candidate_fields + 退订专用 suppress_scope；
  动作/置信度/其他键一律护栏拦截（动作由域 REPLY_ACTIONS 决定）；
- 提取候选必须用 demand 词表，quote 必须逐字出现在原消息（provenance）；
- agent 不落库、不执行动作、不 import infra/domains 内部实现（AST 锁定）；
- 凭证不进模型：prompt 只含消息正文。
"""

from __future__ import annotations

import ast
import importlib
import json
import pathlib
import typing
from typing import Any

import pytest

from agent_runtime.base import AgentTask, ChangeSet
from domains.conversations.schemas import ReplyFieldEvidence
from shared.errors import ValidationError
from shared.schemas.identifiers import RunId, TenantId, UserId, new_id

AGENT_MODULE = (
    pathlib.Path(__file__).resolve().parents[2]
    / "agent_runtime"
    / "qualification_agent"
    / "agent.py"
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


class _RawPort:
    """返回任意对象：模拟不遵守 ``ReplyModelPort`` 返回 str 契约的实现。"""

    def __init__(self, value: object) -> None:
        self._value = value

    async def classify_reply(
        self, *, system_prompt: str, message: dict[str, str]
    ) -> str:
        del system_prompt, message
        return self._value  # type: ignore[return-value]


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
    port = _FakePort(
        [
            json.dumps(
                {
                    "category": "unsubscribe",
                    "candidate_fields": [],
                    "suppress_scope": "contact",
                }
            )
        ]
    )
    task = _task(message)
    changeset = await _agent(port).run(task, None)
    entry = _classification_entry(changeset)
    assert entry is not None
    payload = entry["payload"]
    assert payload["category"] == "unsubscribe"
    assert payload["suppress_scope"] == "contact"
    assert payload["message_id"] == "msg_boundary_1"
    assert payload["model_version"] == "test-model-v1"
    assert set(payload) == {
        "category",
        "message_id",
        "model_version",
        "suppress_scope",
    }
    assert entry["risk_level"] == "low"
    assert changeset.tenant_id == task.tenant_id
    assert changeset.run_id == task.run_id
    # 任何动作都不由 agent 产出/执行（apply_actions 是 workflow 后续步骤）
    assert all(
        change.get("operation") != "apply_actions" for change in changeset.changes
    )


async def test_typed_classify_result_is_the_boundary() -> None:
    """classify() 返回 typed ReplyClassificationResult（枚举类别 + 候选元组）。"""
    message = {
        "message_id": "msg_boundary_8",
        "subject": "Interested",
        "body": "Yes, we are interested in your products.",
    }
    port = _FakePort(
        [json.dumps({"category": "clear_interest", "candidate_fields": []})]
    )
    agent = _agent(port)
    result = await agent.classify(message=message)
    assert result.category.value == "clear_interest"
    assert result.candidate_fields == ()


@pytest.mark.parametrize(
    ("subject", "body"),
    (
        (
            "Company-wide opt out",
            "Please remove our entire company from your database.",
        ),
        (
            "Not interested, remove us",
            "Please remove us from your contact list.",
        ),
        (
            "Remove our company",
            "Please remove our company from your distribution list.",
        ),
        (
            "Both addresses",
            "Please remove me and my assistant from your list.",
        ),
    ),
)
async def test_explicit_account_unsubscribe_never_downgrades_to_contact(
    subject: str,
    body: str,
) -> None:
    """通用安全判定必须覆盖所有受控 account-scope 表达，不能信任模型降级。"""
    message = {
        "message_id": "msg_account_scope",
        "subject": subject,
        "body": body,
    }
    port = _FakePort(
        [
            json.dumps(
                {
                    "category": "unsubscribe",
                    "candidate_fields": [],
                    "suppress_scope": "contact",
                }
            )
        ]
    )

    result = await _agent(port).classify(message=message)

    assert result.suppress_scope.value == "account"


async def test_unsubscribe_without_broader_evidence_defaults_to_contact_scope() -> None:
    message = {
        "message_id": "msg_contact_scope",
        "subject": "Unsubscribe",
        "body": "Please unsubscribe me from your emails.",
    }
    port = _FakePort([json.dumps({"category": "unsubscribe", "candidate_fields": []})])

    result = await _agent(port).classify(message=message)

    assert result.suppress_scope.value == "contact"


@pytest.mark.parametrize(
    "category,body",
    [
        ("complaint", "Stop sending me spam. I am reporting your unsolicited mail."),
        (
            "auto_reply",
            "This is an automatic reply. If you would like to unsubscribe, click the link below.",
        ),
        ("auto_reply", "This is an automatic reply.\nUnsubscribe"),
        ("clear_interest", "Please do not unsubscribe me. We need hinges."),
    ],
)
async def test_unsubscribe_words_do_not_replace_current_reply_meaning(category, body):
    agent = _agent(_FakePort([json.dumps({"category": category})]))
    result = await agent.classify(
        message={
            "message_id": "msg_meaning",
            "subject": "(current reply)",
            "body": body,
        }
    )
    assert result.category.value == category
    assert result.suppress_scope is None


@pytest.mark.parametrize(
    "body",
    [
        "I am leaving our company. Please unsubscribe only me.",
        "Please unsubscribe me. Do not remove us from your mailing list.",
    ],
)
async def test_company_mention_does_not_expand_personal_unsubscribe(body):
    agent = _agent(
        _FakePort(
            [json.dumps({"category": "unsubscribe", "suppress_scope": "contact"})]
        )
    )
    result = await agent.classify(
        message={
            "message_id": "msg_personal",
            "subject": "(current reply)",
            "body": body,
        }
    )
    assert result.category.value == "unsubscribe"
    assert result.suppress_scope.value == "contact"


@pytest.mark.parametrize(
    "category,body,scope",
    [
        ("clear_interest", "Please unsubscribe me from your emails.", "contact"),
        ("clear_interest", "unsubscribe", "contact"),
        (
            "auto_reply",
            "Please remove our entire organization from your mailing list. Thank you.",
            "account",
        ),
    ],
)
async def test_unambiguous_direct_unsubscribe_still_overrides_nonprotective_category(
    category, body, scope
):
    agent = _agent(_FakePort([json.dumps({"category": category})]))
    result = await agent.classify(
        message={
            "message_id": "msg_explicit_request",
            "subject": "(current reply)",
            "body": body,
        }
    )
    assert result.category.value == "unsubscribe"
    assert result.suppress_scope.value == scope


async def test_non_unsubscribe_cannot_smuggle_suppression_scope() -> None:
    message = {
        "message_id": "msg_wrong_scope",
        "subject": "Not interested",
        "body": "We are not interested.",
    }
    port = _FakePort(
        [
            json.dumps(
                {
                    "category": "rejection",
                    "candidate_fields": [],
                    "suppress_scope": "account",
                }
            )
        ]
    )

    with pytest.raises(ValidationError, match="非退订分类不得携带抑制范围"):
        await _agent(port).classify(message=message)


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
        [
            json.dumps(
                {"category": "rejection", "confidence": 0.9, "candidate_fields": []}
            )
        ]
    )
    agent = _agent(port)
    with pytest.raises(ValidationError):
        await agent.classify(message=message)
    changeset = await agent.run(_task(message), None)
    assert changeset.changes == []
    assert changeset.summary == "模型输出被护栏拦截：模型输出含未授权键：['confidence']"


async def test_invalid_message_input_has_distinct_summary() -> None:
    """P3-4：消息输入校验失败与模型输出护栏失败用不同 summary。"""
    agent = _agent(_FakePort())
    for bad_message in (
        None,
        {},
        {"subject": "x"},
        {"message_id": "m", "subject": "", "body": "b"},
    ):
        changeset = await agent.run(_task(bad_message), None)
        assert changeset.changes == []
        assert changeset.summary == "任务输入无效"


async def test_model_cannot_smuggle_actions_or_unknown_keys() -> None:
    """模型输出任何动作/未授权键 → 拦截（动作只能来自域 REPLY_ACTIONS，不由模型给）。"""
    message = {
        "message_id": "msg_boundary_4",
        "subject": "Hello",
        "body": "We may need hinges.",
    }
    expectations = (
        ({"category": "maybe_interesting"}, "模型输出了未知类别"),
        (
            {"category": "rejection", "actions": ["stop_sequence"]},
            "模型输出含未授权键：['actions']",
        ),
        (
            {"category": "rejection", "side_effect": "email.send"},
            "模型输出含未授权键：['side_effect']",
        ),
    )
    for payload, expected_summary in expectations:
        port = _FakePort([json.dumps(payload)])
        agent = _agent(port)
        with pytest.raises(ValidationError):
            await agent.classify(message=message)
        changeset = await agent.run(_task(message), None)
        assert changeset.changes == []
        assert changeset.summary == f"模型输出被护栏拦截：{expected_summary}"


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
        change
        for change in changeset.changes
        if change.get("operation") == "update_need_fields"
    )
    fields = fields_entry["payload"]["fields"]
    assert {(item["field"], item["value"]) for item in fields} == {
        ("quantity", "50000"),
        ("size_spec", "M8 x 20mm"),
    }


async def test_overlong_exact_candidate_quote_rejects_entire_model_output() -> None:
    """逐字命中仍不够：超过 500 code point 的整正文不得进入任何 ChangeSet。"""
    body = (
        "email-marker@example.test RAW-CRED-MARKER " + "界" * 1_150 + " RAW-TAIL-MARKER"
    )
    port = _FakePort(
        [
            json.dumps(
                {
                    "category": "provides_specification",
                    "candidate_fields": [
                        {
                            "field": "product_category",
                            "value": "hinges",
                            "quote": body,
                        }
                    ],
                }
            )
        ]
    )
    agent = _agent(port)
    message = {
        "message_id": "msg_overlong_candidate_quote",
        "subject": "Specification",
        "body": body,
    }

    with pytest.raises(ValidationError, match="候选逐字证据超长"):
        await agent.classify(message=message)

    changeset = await agent.run(_task(message), None)
    assert changeset.changes == []
    assert changeset.summary == "模型输出被护栏拦截：模型输出候选逐字证据超长"


def test_durable_reply_field_contract_rejects_overlong_quote() -> None:
    """绕过 agent 的调用方也不能把超长模型候选交给 Conversation 持久化。"""
    with pytest.raises(ValidationError, match="回复字段逐字证据超长"):
        ReplyFieldEvidence(
            field="product_category",
            value="hinges",
            quote="界" * 501,
        )


async def test_non_string_port_output_is_rejected_without_leaking() -> None:
    """P2：port 返回非 str（None/bytes/int/list）→ 在任何 encode/len/json.loads
    之前类型校验失败；classify 抛 ValidationError，run 不泄漏异常并返回精确
    摘要的空 ChangeSet。"""
    message = {
        "message_id": "msg_boundary_type",
        "subject": "Hello",
        "body": "We may need hinges.",
    }
    for bad_value in (None, b"raw bytes", 123, ["a", "b"]):
        port = _RawPort(bad_value)
        agent = _agent(port)
        with pytest.raises(ValidationError) as caught:
            await agent.classify(message=message)
        assert "模型输出类型无效" in str(caught.value)
        changeset = await agent.run(_task(message), None)
        assert changeset.changes == []
        assert changeset.summary == "模型输出被护栏拦截：模型输出类型无效"


async def test_oversized_model_output_is_rejected_before_parsing() -> None:
    """P2：固定大小上限（64 KiB）在 json.loads 前拦截；超限有效/无效 JSON 均
    不解析，run 返回可审计空 ChangeSet。"""
    message = {
        "message_id": "msg_boundary_size",
        "subject": "Hello",
        "body": "We may need hinges.",
    }
    oversized_valid = (
        '{"category": "rejection", "candidate_fields": [], "padding": "'
        + "x" * 70_000
        + '"}'
    )
    oversized_invalid = "x" * 70_000
    for raw in (oversized_valid, oversized_invalid):
        port = _FakePort([raw])
        agent = _agent(port)
        with pytest.raises(ValidationError) as caught:
            await agent.classify(message=message)
        assert "大小上限" in str(caught.value)  # 若先解析，原因会是 JSON 类错误
        changeset = await agent.run(_task(message), None)
        assert changeset.changes == []
        assert changeset.summary == "模型输出被护栏拦截：模型输出超过大小上限"


async def test_identical_duplicate_candidates_are_deduplicated() -> None:
    """P3-1：完全相同候选确定性去重（不重复落 ChangeSet）。"""
    message = {
        "message_id": "msg_boundary_dup",
        "subject": "Bolt specification",
        "body": "We need 50,000 pieces of M8 x 20mm hex bolts.",
    }
    model_output = {
        "category": "provides_specification",
        "candidate_fields": [
            {"field": "quantity", "value": "50000", "quote": "50,000 pieces"},
            {"field": "quantity", "value": "50000", "quote": "50,000 pieces"},
            {"field": "size_spec", "value": "M8 x 20mm", "quote": "M8 x 20mm"},
        ],
    }
    port = _FakePort([json.dumps(model_output)])
    result = await _agent(port).classify(message=message)
    assert len(result.candidate_fields) == 2


async def test_conflicting_candidates_same_field_reject_whole_output() -> None:
    """P3-1：同 field 但 value 或 quote 不同 → 整份输出 ValidationError。"""
    message = {
        "message_id": "msg_boundary_conflict",
        "subject": "Bolt specification",
        "body": "We need 50,000 pieces of M8 x 20mm hex bolts.",
    }
    for payload in (
        {
            "category": "provides_specification",
            "candidate_fields": [
                {"field": "quantity", "value": "50000", "quote": "50,000 pieces"},
                {"field": "quantity", "value": "90000", "quote": "50,000 pieces"},
            ],
        },
        {
            "category": "provides_specification",
            "candidate_fields": [
                {"field": "quantity", "value": "50000", "quote": "50,000 pieces"},
                {"field": "quantity", "value": "50000", "quote": "M8 x 20mm"},
            ],
        },
    ):
        port = _FakePort([json.dumps(payload)])
        agent = _agent(port)
        with pytest.raises(ValidationError) as caught:
            await agent.classify(message=message)
        assert "候选字段冲突" in str(caught.value)
        changeset = await agent.run(_task(message), None)
        assert changeset.changes == []
        assert changeset.summary == "模型输出被护栏拦截：模型输出候选字段冲突：quantity"


async def test_malformed_model_output_is_intercepted() -> None:
    message = {
        "message_id": "msg_boundary_6",
        "subject": "Hello",
        "body": "We may need hinges.",
    }
    expectations = (
        ("not json at all", "模型输出不是合法 JSON"),
        ('["a", "b"]', "模型输出必须是 JSON 对象"),
        (
            '{"category": "rejection", "candidate_fields": "nope"}',
            "模型输出 candidate_fields 必须是数组",
        ),
    )
    for raw, expected_reason in expectations:
        port = _FakePort([raw])
        agent = _agent(port)
        with pytest.raises(ValidationError):
            await agent.classify(message=message)
        changeset = await agent.run(_task(message), None)
        assert changeset.changes == []
        assert changeset.summary == f"模型输出被护栏拦截：{expected_reason}"


async def test_port_receives_only_subject_and_body_without_identifiers() -> None:
    """P3-2：port 只能收到 subject/body，绝不能收到 message_id/其他 identifier；
    run 保留 message_id 用于 ChangeSet。"""
    message = {
        "message_id": "msg_boundary_7",
        "subject": "Interested",
        "body": "Yes, we are interested in your products.",
    }
    port = _FakePort()
    task = _task(message)
    changeset = await _agent(port).run(task, None)
    _system, received = port.calls[0]
    assert received == {
        "subject": "Interested",
        "body": "Yes, we are interested in your products.",
    }
    assert set(received) == {"subject", "body"}
    for marker in (
        "secret",
        "password",
        "token",
        "dsn",
        "postgresql",
        "message_id",
        "run_id",
        "tenant_id",
    ):
        assert marker not in json.dumps(received).lower()
        assert marker not in _system.lower()
    # message_id 只在 ChangeSet 分类条目里
    payload = _classification_entry(changeset)["payload"]
    assert payload["message_id"] == "msg_boundary_7"


def test_need_field_vocabulary_matches_demand_factual_contract() -> None:
    """P3-3：agent 的 NEED_FIELD_NAMES 必须与 domains/demand ValidatedNeed 的
    模型可提取 FactualField 字段集一致；typed unit 与布尔复购事实除外。"""
    demand_models = importlib.import_module("domains.demand.models")
    hints = typing.get_type_hints(demand_models.ValidatedNeed)
    factual: set[str] = set()
    for name, hint in hints.items():
        candidates = (
            typing.get_args(hint)
            if typing.get_origin(hint) is typing.Union
            else (hint,)
        )
        if any(
            typing.get_origin(arg) is demand_models.FactualField for arg in candidates
        ):
            factual.add(name)
    from agent_runtime.qualification_agent.agent import NEED_FIELD_NAMES

    non_model_extractable = {"unit", "recurring_requirement"}
    assert non_model_extractable <= factual
    assert NEED_FIELD_NAMES.isdisjoint(non_model_extractable)
    assert NEED_FIELD_NAMES == frozenset(factual - non_model_extractable)


async def test_model_unit_candidate_cannot_enter_need_changeset() -> None:
    """人工报价单位是事实字段，但即使逐字出现也不授权模型确认单位。"""
    message = {
        "message_id": "msg_unit_boundary",
        "subject": "Specification",
        "body": "We need 100 pieces.",
    }
    agent = _agent(
        _FakePort(
            [
                json.dumps(
                    {
                        "category": "provides_specification",
                        "candidate_fields": [
                            {"field": "unit", "value": "pieces", "quote": "pieces"}
                        ],
                    }
                )
            ]
        )
    )
    result = await agent.classify(message=message)
    assert result.candidate_fields == ()
    changeset = await agent.run(_task(message), None)
    assert all(
        change.get("operation") != "update_need_fields" for change in changeset.changes
    )


async def test_model_recurring_requirement_candidate_cannot_enter_need_changeset() -> (
    None
):
    """逐字复购候选被单独丢弃，同批合法字段仍必须保留。"""
    recurring_quote = "We order these hinges every month."
    quantity_quote = "500 pieces"
    message = {
        "message_id": "msg_recurring_requirement_boundary",
        "subject": "Recurring order",
        "body": f"{recurring_quote} We need {quantity_quote}.",
    }
    agent = _agent(
        _FakePort(
            [
                json.dumps(
                    {
                        "category": "provides_specification",
                        "candidate_fields": [
                            {
                                "field": "recurring_requirement",
                                "value": "true",
                                "quote": recurring_quote,
                            },
                            {
                                "field": "quantity",
                                "value": "500",
                                "quote": quantity_quote,
                            },
                        ],
                    }
                )
            ]
        )
    )

    result = await agent.classify(message=message)
    assert [
        (candidate.field, candidate.value, candidate.quote)
        for candidate in result.candidate_fields
    ] == [("quantity", "500", quantity_quote)]

    changeset = await agent.run(_task(message), None)
    fields_changes = [
        change
        for change in changeset.changes
        if change.get("operation") == "update_need_fields"
    ]
    assert len(fields_changes) == 1
    assert fields_changes[0]["payload"]["fields"] == [
        {
            "field": "quantity",
            "value": "500",
            "quote": quantity_quote,
        }
    ]
    assert all(
        item["field"] != "recurring_requirement"
        for change in fields_changes
        for item in change["payload"]["fields"]
    )


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
