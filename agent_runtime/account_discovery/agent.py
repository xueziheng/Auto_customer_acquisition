"""AccountDiscoveryAgent：把带证据的假设收窄为企业消歧候选。

模型只读取已批准的假设证据投影，只能输出企业公开事实；联系人姓名、邮箱、
电话、凭证、动作、概率和最终金额均不属于本边界。联系人补全在后续 workflow
经 Tool Gateway 执行，结果不会进入模型或 ChangeSet。
"""

from __future__ import annotations

import json
import re
from typing import Any, Protocol, runtime_checkable

from agent_runtime.base import AgentTask, CapabilityAgent, ChangeSet
from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
from shared.errors import ValidationError
from shared.schemas.identifiers import ChangeSetId, new_id

_MAX_OUTPUT_BYTES = 32_768
_ULID = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"
_HYPOTHESIS_RE = re.compile(rf"hyp_{_ULID}")
_SIGNAL_RE = re.compile(rf"sig_{_ULID}")
_DOMAIN_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_EMAIL = re.compile(r"(?i)(?<![\w.+-])[\w.+-]{1,64}@[a-z0-9.-]+\.[a-z]{2,63}")
_PHONE = re.compile(r"(?<!\w)(?:\+?\d[\d .()/-]{7,}\d)(?!\w)")
_DATE_LIKE = re.compile(r"\d{4}[-/]\d{2}[-/]\d{2}")
_OUTPUT_KEYS = frozenset(
    {
        "entity_name",
        "country",
        "website_domain",
        "entity_type",
        "industry",
        "size_hint",
        "source_signal_refs",
    }
)
_OPTIONAL_TEXT_KEYS = ("entity_type", "industry", "size_hint")

_SYSTEM_PROMPT = """你是 TradeOS 的企业发现能力。输入是一个带来源的需求假设。
只输出一个 JSON 对象，禁止 Markdown、解释或额外键。输出字段固定为：
entity_name, country, website_domain, entity_type, industry, size_hint,
source_signal_refs。website_domain 必须是企业公开官网域名，不含协议或路径。
source_signal_refs 只能引用输入中的 signal ID。没有足够证据时输出空对象 {}。
禁止输出联系人姓名、邮箱、电话、凭证、动作、概率/置信度或任何金额。
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


def _optional_text(value: object, *, max_len: int) -> str | None:
    if value is None:
        return None
    return _exact_text(value, max_len=max_len)


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


def _redact_contacts(value: str) -> str:
    redacted = _EMAIL.sub("[CONTACT_REDACTED]", value)
    return _PHONE.sub(
        lambda match: (
            match.group(0)
            if _DATE_LIKE.fullmatch(match.group(0)) is not None
            else "[CONTACT_REDACTED]"
        ),
        redacted,
    )


def _contact_free_source_url(value: object) -> str | None:
    source_url = _optional_text(value, max_len=2_000)
    if source_url is None:
        return None
    if _EMAIL.search(source_url) is not None or _PHONE.search(source_url) is not None:
        return None
    return source_url


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
        raw = task.inputs.get("hypothesis")
        allowed_countries = task.inputs.get("allowed_countries")
        if not isinstance(raw, dict) or not isinstance(allowed_countries, tuple):
            raise ValidationError("account discovery 任务输入无效")
        if set(raw) != {
            "hypothesis_id",
            "category",
            "reasoning",
            "evidence",
            "source_signal_refs",
        }:
            raise ValidationError("account discovery 任务输入无效")
        hypothesis_id = _exact_text(raw.get("hypothesis_id"), max_len=40)
        if _HYPOTHESIS_RE.fullmatch(hypothesis_id) is None:
            raise ValidationError("account discovery 任务输入无效")
        category = _redact_contacts(_exact_text(raw.get("category"), max_len=200))
        reasoning = _redact_contacts(
            _exact_text(raw.get("reasoning"), max_len=4_000)
        )
        countries = tuple(
            sorted({_exact_text(value, max_len=64) for value in allowed_countries})
        )
        if not countries:
            raise ValidationError("account discovery 任务输入无效")
        raw_refs = raw.get("source_signal_refs")
        if not isinstance(raw_refs, (list, tuple)) or not raw_refs:
            raise ValidationError("account discovery 任务输入无效")
        refs = tuple(dict.fromkeys(_exact_text(value, max_len=40) for value in raw_refs))
        if any(_SIGNAL_RE.fullmatch(value) is None for value in refs):
            raise ValidationError("account discovery 任务输入无效")
        raw_evidence = raw.get("evidence")
        if not isinstance(raw_evidence, (list, tuple)) or not raw_evidence:
            raise ValidationError("account discovery 任务输入无效")
        evidence: list[dict[str, str | None]] = []
        for item in raw_evidence:
            if not isinstance(item, dict) or set(item) != {
                "signal_id",
                "summary",
                "source_url",
            }:
                raise ValidationError("account discovery 证据输入无效")
            signal_id = _exact_text(item.get("signal_id"), max_len=40)
            if signal_id not in refs:
                raise ValidationError("account discovery 证据输入无效")
            evidence.append(
                {
                    "signal_id": signal_id,
                    "summary": _redact_contacts(
                        _exact_text(item.get("summary"), max_len=4_000)
                    ),
                    "source_url": _contact_free_source_url(item.get("source_url")),
                }
            )
        return {
            "hypothesis_id": hypothesis_id,
            "category": category,
            "reasoning": reasoning,
            "allowed_countries": countries,
            "source_signal_refs": refs,
            "evidence": evidence,
        }

    @classmethod
    def _validate_output(
        cls, raw: str, projection: dict[str, object]
    ) -> dict[str, object] | None:
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > _MAX_OUTPUT_BYTES:
            raise ValidationError("企业发现模型输出无效")
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            raise ValidationError("企业发现模型输出不是合法 JSON") from None
        if payload == {}:
            return None
        if not isinstance(payload, dict) or set(payload) != _OUTPUT_KEYS:
            raise ValidationError("企业发现模型输出含未授权字段")
        entity_name = _exact_text(payload.get("entity_name"), max_len=200)
        country = _exact_text(payload.get("country"), max_len=64)
        allowed_countries = projection["allowed_countries"]
        if not isinstance(allowed_countries, tuple) or country not in allowed_countries:
            raise ValidationError("企业发现模型输出国家越界")
        domain = _canonical_domain(payload.get("website_domain"))
        raw_refs = payload.get("source_signal_refs")
        if not isinstance(raw_refs, list) or not raw_refs:
            raise ValidationError("企业发现模型输出缺少证据引用")
        refs = tuple(dict.fromkeys(_exact_text(value, max_len=40) for value in raw_refs))
        source_refs = projection["source_signal_refs"]
        if not isinstance(source_refs, tuple) or any(ref not in source_refs for ref in refs):
            raise ValidationError("企业发现模型输出证据越界")
        return {
            "entity_name": entity_name,
            "country": country,
            "website_domain": domain,
            **{
                key: _optional_text(payload.get(key), max_len=200)
                for key in _OPTIONAL_TEXT_KEYS
            },
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
        return ChangeSet(
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
