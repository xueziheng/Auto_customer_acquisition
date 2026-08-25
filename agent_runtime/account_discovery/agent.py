"""AccountDiscoveryAgent：以结构化组织事实解析官网域名。

模型只读取 tenant-bound 企业名/国家、typed category 与 opaque evidence refs；
推断、观察摘要和 URL 不进入模型。模型只返回官网域名与 evidence refs，ChangeSet
中的企业身份完全来自可信投影，因此无需猜测自由文本里哪个词是人名。
"""

from __future__ import annotations

import json
import re
from typing import Any, Protocol, runtime_checkable

from agent_runtime.base import AgentTask, CapabilityAgent, ChangeSet
from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
from agent_runtime.guardrails.rails import guard_phase1_change_set
from shared.errors import ValidationError
from shared.schemas.identifiers import ChangeSetId, new_id

_MAX_OUTPUT_BYTES = 32_768
_ULID = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"
_HYPOTHESIS_RE = re.compile(rf"hyp_{_ULID}")
_ACCOUNT_RE = re.compile(rf"acc_{_ULID}")
_SIGNAL_RE = re.compile(rf"sig_{_ULID}")
_DOMAIN_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_OUTPUT_KEYS = frozenset({"website_domain", "source_signal_refs"})

_SYSTEM_PROMPT = """你是 TradeOS 的企业发现能力。输入只含可信组织事实、需求类别和证据 ID。
只输出一个 JSON 对象，禁止 Markdown、解释或额外键。输出字段固定为：
website_domain, source_signal_refs。website_domain 必须是企业公开官网域名，不含协议或路径。
source_signal_refs 只能引用输入中的 signal ID。没有足够证据时输出空对象 {}。
禁止输出企业名、联系人信息、自由文本、凭证、动作、概率或金额。
"""


@runtime_checkable
class AccountDiscoveryModelPort(Protocol):
    """结构化模型调用端口；provider 凭证与计量只存在于实现侧。"""

    async def discover_account(
        self, *, system_prompt: str, hypothesis: dict[str, object]
    ) -> str: ...


def _exact_text(value: object, *, max_len: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > max_len
    ):
        raise ValidationError("企业发现字段无效")
    return value


def _canonical_domain(value: object) -> str:
    raw = _exact_text(value, max_len=253)
    if any(marker in raw for marker in (":", "/", "?", "#", "@")):
        raise ValidationError("企业发现官网域名无效")
    try:
        canonical = raw.removesuffix(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValidationError("企业发现官网域名无效") from exc
    labels = canonical.split(".")
    if len(labels) < 2 or any(_DOMAIN_LABEL.fullmatch(label) is None for label in labels):
        raise ValidationError("企业发现官网域名无效")
    return canonical


class AccountDiscoveryAgent(CapabilityAgent):
    """验证企业公开事实输出并生成一个低风险 Prospecting ChangeSet。"""

    name = "account_discovery"

    def __init__(
        self,
        model: str,
        model_client: Any,
        gateway: Any,
        guardrails: Any,
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ValidationError("account discovery 模型标识无效")
        if not isinstance(model_client, AccountDiscoveryModelPort):
            raise ValidationError("account discovery 模型端口无效")
        if not isinstance(guardrails, CredentialMarkerGuard):
            raise ValidationError("account discovery 输入护栏无效")
        self._model = model
        self._model_port = model_client
        self._gateway = gateway
        self._input_guard = guardrails

    @staticmethod
    def _safe_projection(task: AgentTask) -> dict[str, object]:
        if set(task.inputs) != {
            "hypothesis_id",
            "organization",
            "category",
            "source_signal_refs",
            "allowed_countries",
        }:
            raise ValidationError("account discovery 任务输入无效")
        organization = task.inputs.get("organization")
        allowed_countries = task.inputs.get("allowed_countries")
        if (
            not isinstance(organization, dict)
            or set(organization) != {"account_id", "entity_name", "country"}
            or not isinstance(allowed_countries, tuple)
        ):
            raise ValidationError("account discovery 任务输入无效")
        hypothesis_id = _exact_text(task.inputs.get("hypothesis_id"), max_len=40)
        if _HYPOTHESIS_RE.fullmatch(hypothesis_id) is None:
            raise ValidationError("account discovery 任务输入无效")
        account_id = _exact_text(organization.get("account_id"), max_len=40)
        if _ACCOUNT_RE.fullmatch(account_id) is None:
            raise ValidationError("account discovery 任务输入无效")
        entity_name = _exact_text(organization.get("entity_name"), max_len=200)
        country = _exact_text(organization.get("country"), max_len=64)
        category = _exact_text(task.inputs.get("category"), max_len=200)
        countries = tuple(
            sorted({_exact_text(value, max_len=64) for value in allowed_countries})
        )
        if not countries or country not in countries:
            raise ValidationError("account discovery 任务输入无效")
        raw_refs = task.inputs.get("source_signal_refs")
        if not isinstance(raw_refs, (list, tuple)) or not raw_refs:
            raise ValidationError("account discovery 任务输入无效")
        refs = tuple(dict.fromkeys(_exact_text(value, max_len=40) for value in raw_refs))
        if any(_SIGNAL_RE.fullmatch(value) is None for value in refs):
            raise ValidationError("account discovery 任务输入无效")
        return {
            "hypothesis_id": hypothesis_id,
            "organization": {
                "entity_name": entity_name,
                "country": country,
            },
            "category": category,
            "source_signal_refs": refs,
        }

    def _validate_output(
        self, raw: str, projection: dict[str, object]
    ) -> dict[str, object] | None:
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > _MAX_OUTPUT_BYTES:
            raise ValidationError("企业发现模型输出无效")
        self._input_guard.check(subject=None, body=raw)
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            raise ValidationError("企业发现模型输出不是合法 JSON") from None
        if payload == {}:
            return None
        if not isinstance(payload, dict) or set(payload) != _OUTPUT_KEYS:
            raise ValidationError("企业发现模型输出含未授权字段")
        domain = _canonical_domain(payload.get("website_domain"))
        raw_refs = payload.get("source_signal_refs")
        if not isinstance(raw_refs, list) or not raw_refs:
            raise ValidationError("企业发现模型输出缺少证据引用")
        refs = tuple(dict.fromkeys(_exact_text(value, max_len=40) for value in raw_refs))
        source_refs = projection["source_signal_refs"]
        if not isinstance(source_refs, tuple) or any(ref not in source_refs for ref in refs):
            raise ValidationError("企业发现模型输出证据越界")
        organization = projection.get("organization")
        if not isinstance(organization, dict):
            raise ValidationError("企业发现安全组织投影无效")
        return {
            "entity_name": organization["entity_name"],
            "country": organization["country"],
            "website_domain": domain,
            "entity_type": None,
            "industry": None,
            "size_hint": None,
            "source_signal_refs": refs,
        }

    async def run(self, task: AgentTask, context: object) -> ChangeSet:
        """读取安全投影、调用模型、验证输出并返回无 PII 的 ChangeSet。"""
        del context
        if not isinstance(task, AgentTask):
            raise ValidationError("account discovery 任务无效")
        try:
            projection = self._safe_projection(task)
            self._input_guard.check(
                subject=str(projection["category"]),
                body=json.dumps(projection, ensure_ascii=False, sort_keys=True),
            )
        except ValidationError:
            return self._empty(task, "任务输入被安全边界拒绝")
        try:
            raw = await self._model_port.discover_account(
                system_prompt=_SYSTEM_PROMPT,
                hypothesis=projection,
            )
            candidate = self._validate_output(raw, projection)
        except ValidationError as exc:
            return self._empty(task, f"模型输出被护栏拦截：{exc}")
        if candidate is None:
            return self._empty(task, "现有证据不足以解析企业官网")
        candidate_change_set = ChangeSet(
            change_set_id=ChangeSetId(new_id("cs")),
            tenant_id=task.tenant_id,
            run_id=task.run_id,
            changes=[
                {
                    "domain": "prospecting",
                    "operation": "resolve_account",
                    "payload": candidate,
                    "risk_level": "low",
                }
            ],
            summary="已生成 1 条企业消歧候选",
        )
        return guard_phase1_change_set(candidate_change_set)

    @staticmethod
    def _empty(task: AgentTask, summary: str) -> ChangeSet:
        return ChangeSet(
            change_set_id=ChangeSetId(new_id("cs")),
            tenant_id=task.tenant_id,
            run_id=task.run_id,
            changes=[],
            summary=summary,
        )


__all__ = ("AccountDiscoveryAgent", "AccountDiscoveryModelPort")
