"""受限外联撰写能力：只生成可审查草稿，不执行发送。"""

from __future__ import annotations

import json
import re
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

from agent_runtime.base import AgentTask, CapabilityAgent, ChangeSet
from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
from agent_runtime.guardrails.rails import guard_phase1_change_set
from shared.errors import ValidationError
from shared.schemas.identifiers import ChangeSetId, new_id

_MAX_OUTPUT_BYTES = 100_000
_OUTPUT_KEYS = frozenset({"subject", "body", "content_language"})
_REQUEST_KEYS = frozenset(
    {
        "account_name",
        "intent",
        "target_language",
        "evidence",
        "unsubscribe_text",
    }
)
_EVIDENCE_KEYS = frozenset({"source_id", "source_url", "fact"})
_INTENTS = frozenset({"discovery", "presentation", "follow_up"})
_LANGUAGE_TAG = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*")

_SYSTEM_PROMPT = """你是 TradeOS 的外联草稿能力。输入只包含企业名称、序列意图、
目标语言、公开事实证据和必须逐字包含的退订说明。只输出一个 JSON 对象，键必须精确
为 subject, body, content_language，禁止 Markdown、解释、动作或额外键。

第一封 discovery 邮件的目标是请客户表达真实采购困难，不是推销已有产品。只能引用
输入 evidence 中的公开事实，不能把推断写成事实。客户内容必须使用 target_language，
且 body 必须逐字包含 unsubscribe_text。禁止价格、折扣、交期、库存、认证、付款条件、
合同、独家代理、质量保证等商业承诺。你只写草稿，绝不建议、声明或调用发送动作。
"""


@runtime_checkable
class OutreachDraftModelPort(Protocol):
    """结构化草稿模型端口；凭证、发送与业务写入均不属于该端口。"""

    async def draft_outreach(
        self, *, system_prompt: str, request: dict[str, object]
    ) -> str: ...


def _text(value: object, *, field: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValidationError(f"{field} 无效")
    return value


def _language(value: object, *, field: str) -> str:
    result = _text(value, field=field, maximum=35)
    if _LANGUAGE_TAG.fullmatch(result) is None:
        raise ValidationError(f"{field} 无效")
    return result


def _public_url(value: object) -> str:
    result = _text(value, field="外联证据来源", maximum=2_000)
    parsed = urlsplit(result)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValidationError("外联证据来源无效")
    return result


class OutreachAgent(CapabilityAgent):
    """验证公开证据与模型草稿，并交给 Phase 1 默认输出护栏。"""

    name = "outreach_agent"

    def __init__(
        self,
        model: str,
        model_client: Any,
        gateway: Any,
        guardrails: Any,
    ) -> None:
        self._model = _text(model, field="外联模型标识", maximum=128)
        if not isinstance(model_client, OutreachDraftModelPort):
            raise ValidationError("外联草稿模型端口无效")
        if not isinstance(guardrails, CredentialMarkerGuard):
            raise ValidationError("外联输入护栏无效")
        self._model_port = model_client
        self._gateway = gateway
        self._input_guard = guardrails

    async def run(self, task: AgentTask, context: object) -> ChangeSet:
        """生成一条 ``create_draft`` 变更；任何失败都返回安全空变更集。"""
        del context
        if not isinstance(task, AgentTask):
            raise ValidationError("外联任务无效")
        try:
            projection = self._safe_projection(task)
            self._input_guard.check(
                subject=str(projection["account_name"]),
                body=json.dumps(projection, ensure_ascii=False, sort_keys=True),
            )
        except ValidationError:
            return self._empty(task, "外联输入被安全边界拒绝")
        try:
            raw = await self._model_port.draft_outreach(
                system_prompt=_SYSTEM_PROMPT,
                request=projection,
            )
            draft = self._validate_output(raw, projection)
        except ValidationError as exc:
            return self._empty(task, f"模型输出被护栏拦截：{exc}")
        evidence = projection["evidence"]
        assert isinstance(evidence, tuple)
        candidate = ChangeSet(
            change_set_id=ChangeSetId(new_id("cs")),
            tenant_id=task.tenant_id,
            run_id=task.run_id,
            changes=[
                {
                    "domain": "outreach",
                    "operation": "create_draft",
                    "payload": {
                        **draft,
                        "target_language": projection["target_language"],
                        "source_evidence_refs": tuple(
                            str(item["source_id"]) for item in evidence
                        ),
                        "generated_by": self._model,
                    },
                    "risk_level": "medium",
                }
            ],
            summary="已生成 1 封待审查外联草稿",
        )
        return guard_phase1_change_set(candidate)

    @staticmethod
    def _safe_projection(task: AgentTask) -> dict[str, object]:
        if set(task.inputs) != {"draft_request"}:
            raise ValidationError("外联任务输入无效")
        request = task.inputs.get("draft_request")
        if not isinstance(request, dict) or set(request) != _REQUEST_KEYS:
            raise ValidationError("外联任务输入无效")
        account_name = _text(
            request.get("account_name"), field="外联企业名称", maximum=200
        )
        intent = _text(request.get("intent"), field="外联意图", maximum=32)
        if intent not in _INTENTS:
            raise ValidationError("外联意图无效")
        target_language = _language(
            request.get("target_language"), field="外联目标语言"
        )
        unsubscribe_text = _text(
            request.get("unsubscribe_text"), field="外联退订说明", maximum=1_000
        )
        raw_evidence = request.get("evidence")
        if not isinstance(raw_evidence, (list, tuple)) or not 1 <= len(raw_evidence) <= 20:
            raise ValidationError("外联公开证据无效")
        evidence: list[dict[str, str]] = []
        for item in raw_evidence:
            if not isinstance(item, dict) or set(item) != _EVIDENCE_KEYS:
                raise ValidationError("外联公开证据无效")
            evidence.append(
                {
                    "source_id": _text(
                        item.get("source_id"), field="外联证据引用", maximum=200
                    ),
                    "source_url": _public_url(item.get("source_url")),
                    "fact": _text(
                        item.get("fact"), field="外联公开事实", maximum=4_000
                    ),
                }
            )
        return {
            "account_name": account_name,
            "intent": intent,
            "target_language": target_language,
            "evidence": tuple(evidence),
            "unsubscribe_text": unsubscribe_text,
        }

    @staticmethod
    def _validate_output(
        raw: str, projection: dict[str, object]
    ) -> dict[str, str]:
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > _MAX_OUTPUT_BYTES:
            raise ValidationError("外联草稿模型输出无效")
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            raise ValidationError("外联草稿模型输出不是合法 JSON") from None
        if not isinstance(payload, dict) or set(payload) != _OUTPUT_KEYS:
            raise ValidationError("外联草稿模型输出含未授权字段")
        subject = _text(payload.get("subject"), field="外联草稿主题", maximum=998)
        body = _text(payload.get("body"), field="外联草稿正文", maximum=100_000)
        content_language = _language(
            payload.get("content_language"), field="外联草稿语言"
        )
        unsubscribe_text = projection["unsubscribe_text"]
        if not isinstance(unsubscribe_text, str) or unsubscribe_text not in body:
            raise ValidationError("外联草稿缺少退订说明")
        return {
            "subject": subject,
            "body": body,
            "content_language": content_language,
        }

    @staticmethod
    def _empty(task: AgentTask, summary: str) -> ChangeSet:
        return ChangeSet(
            change_set_id=ChangeSetId(new_id("cs")),
            tenant_id=task.tenant_id,
            run_id=task.run_id,
            changes=[],
            summary=summary,
        )


__all__ = ("OutreachAgent", "OutreachDraftModelPort")
