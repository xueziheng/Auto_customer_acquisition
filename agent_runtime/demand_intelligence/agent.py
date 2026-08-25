"""受限需求情报能力：从公开页面事实生成信号与明确标注的假设。"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from ipaddress import ip_address
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

from agent_runtime.base import AgentTask, CapabilityAgent, ChangeSet
from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
from agent_runtime.guardrails.rails import contains_numeric_probability
from shared.errors import ValidationError
from shared.schemas.evidence import EvidenceLevel
from shared.schemas.identifiers import ChangeSetId, new_id

_MAX_OUTPUT_BYTES = 65_536
_MAX_TOTAL_PAGE_BYTES = 1_000_000
_ULID = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"
_ARTIFACT = re.compile(rf"art_{_ULID}")
_HASH = re.compile(r"[0-9a-f]{64}")
_COUNTRY = re.compile(r"[A-Z]{2}")
_DOMAIN_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_EMAIL = re.compile(r"(?i)(?<![\w.+-])[\w.+-]{1,64}@[a-z0-9.-]+\.[a-z]{2,63}")
_PHONE = re.compile(r"(?<!\w)(?:\+?\d[\d .()/-]{7,}\d)(?!\w)")
_DATE_LIKE = re.compile(r"\d{4}[-/]\d{2}[-/]\d{2}")
_MONEY = re.compile(
    r"(?i)(?:[$€£¥₹]\s*\d)|(?:\d\s*(?:usd|eur|gbp|cny|rmb))|"
    r"(?:价格|报价|price|quote)\s*[:：]?\s*\d"
)
_CERTAINTY = (
    "一定需要",
    "必然需要",
    "确定需要",
    "肯定需要",
    "will buy",
    "definitely needs",
    "certainly needs",
)
_OUTPUT_KEYS = frozenset({"signals", "hypotheses"})
_SIGNAL_KEYS = frozenset(
    {
        "signal_type",
        "source_page_index",
        "source_excerpt",
        "possible_need",
        "evidence_level",
    }
)
_HYPOTHESIS_KEYS = frozenset(
    {
        "account_name_signal_index",
        "country",
        "country_signal_index",
        "category",
        "reasoning",
        "signal_indexes",
    }
)
_WEB_EVIDENCE_LEVELS = frozenset(
    {
        EvidenceLevel.AGENT_INDUSTRY_INFERENCE.value,
        EvidenceLevel.PUBLIC_COMPANY_EVENT.value,
    }
)

_SYSTEM_PROMPT = """你是 TradeOS 的需求情报能力。输入只包含已批准的公开页面快照、
目标市场/品类、排除项和本轮硬上限。只输出一个 JSON 对象，顶层键必须精确为
signals 和 hypotheses，禁止 Markdown、解释或额外键。

signals 每项字段固定为 signal_type, source_page_index,
source_excerpt, possible_need, evidence_level。source_excerpt 必须逐字摘自对应页面；
它是事实。possible_need 是推断，不能写进 source_excerpt。evidence_level 只能是
public_company_event 或 agent_industry_inference。

hypotheses 每项字段固定为 country, category, reasoning, signal_indexes,
account_name_signal_index, country_signal_index。三个索引只能引用本次 signals，
其中字段索引还必须属于 signal_indexes；country_signal_index 指向的逐字事实必须支持国家；
reasoning 必须使用“可能……值得验证”的推断措辞。国家和品类只能选输入目标集合。

企业身份与官网域名由系统从获批页面 host 确定，模型不得输出或改写。

禁止输出联系人、邮箱、电话、凭证、动作、概率/置信度数字、价格或任何最终金额。
没有足够证据时输出 {"signals": [], "hypotheses": []}。
"""


@runtime_checkable
class DemandIntelligenceModelPort(Protocol):
    async def analyze_pages(
        self,
        *,
        system_prompt: str,
        discovery: dict[str, object],
    ) -> str: ...


class DemandIntelligenceAgent(CapabilityAgent):
    """验证事实摘录与推断引用，产出两个低风险 demand 操作。"""

    name = "demand_intelligence"

    def __init__(
        self,
        model: str,
        model_client: Any,
        gateway: Any,
        guardrails: Any,
    ) -> None:
        if not _safe_label(model, maximum=128):
            raise ValidationError("需求情报模型标识无效")
        if not isinstance(model_client, DemandIntelligenceModelPort):
            raise ValidationError("需求情报模型端口无效")
        if not isinstance(guardrails, CredentialMarkerGuard):
            raise ValidationError("需求情报输入护栏无效")
        self._model = model
        self._model_port = model_client
        self._gateway = gateway
        self._input_guard = guardrails

    async def run(self, task: AgentTask, context: object) -> ChangeSet:
        del context
        if not isinstance(task, AgentTask):
            raise ValidationError("需求情报任务无效")
        try:
            projection = self._safe_projection(task)
            self._input_guard.check(
                subject=str(projection["strategy_group"]),
                body=json.dumps(projection, ensure_ascii=False, sort_keys=True),
            )
        except ValidationError:
            return self._empty(task, "探索输入被安全边界拒绝")
        try:
            raw = await self._model_port.analyze_pages(
                system_prompt=_SYSTEM_PROMPT,
                discovery=_model_projection(projection),
            )
            signals, hypotheses = self._validate_output(raw, projection)
        except ValidationError as error:
            return self._empty(task, f"模型输出被护栏拦截：{error}")
        changes: list[dict[str, object]] = []
        pages = projection["pages"]
        assert isinstance(pages, tuple)
        for signal in signals:
            page_index = signal["source_page_index"]
            assert isinstance(page_index, int)
            page = pages[page_index]
            assert isinstance(page, dict)
            changes.append(
                {
                    "domain": "demand",
                    "operation": "capture_signal",
                    "payload": {
                        "signal_type": signal["signal_type"],
                        "entity_name": signal["entity_name"],
                        "raw_observation": signal["source_excerpt"],
                        "possible_need": signal["possible_need"],
                        "source_type": "web_page",
                        "source_id": page["content_hash"],
                        "source_url": page["url"],
                        "page_hash": page["content_hash"],
                        "snapshot_artifact_ref": page["snapshot_artifact_ref"],
                        "observed_at": page["observed_at"],
                        "evidence_level": signal["evidence_level"],
                        "extracted_by": self._model,
                    },
                    "risk_level": "low",
                }
            )
        for hypothesis in hypotheses:
            indexes = hypothesis["signal_indexes"]
            assert isinstance(indexes, tuple)
            changes.append(
                {
                    "domain": "demand",
                    "operation": "create_hypothesis",
                    "payload": {
                        **hypothesis,
                        "signal_indexes": indexes,
                        "evidence_levels": tuple(
                            signals[index]["evidence_level"] for index in indexes
                        ),
                        "inferred_by": self._model,
                    },
                    "risk_level": "low",
                }
            )
        return ChangeSet(
            change_set_id=ChangeSetId(new_id("cs")),
            tenant_id=task.tenant_id,
            run_id=task.run_id,
            changes=changes,
            summary=f"已生成 {len(signals)} 条需求信号和 {len(hypotheses)} 条需求假设",
        )

    @staticmethod
    def _safe_projection(task: AgentTask) -> dict[str, object]:
        required = {
            "pages",
            "target_countries",
            "target_categories",
            "excluded_countries",
            "excluded_categories",
            "max_signals",
            "max_hypotheses",
            "strategy_group",
        }
        if set(task.inputs) != required:
            raise ValidationError("需求情报任务输入无效")
        pages = _pages(task.inputs.get("pages"))
        target_countries = _countries(
            task.inputs.get("target_countries"), required=True
        )
        target_categories = _texts(
            task.inputs.get("target_categories"), maximum=100, required=True
        )
        excluded_countries = _countries(
            task.inputs.get("excluded_countries"), required=False
        )
        excluded_categories = _texts(
            task.inputs.get("excluded_categories"), maximum=100, required=False
        )
        if set(target_countries) & set(excluded_countries) or set(
            target_categories
        ) & set(excluded_categories):
            raise ValidationError("需求情报目标命中排除项")
        max_signals = _positive_cap(task.inputs.get("max_signals"), maximum=100)
        max_hypotheses = _positive_cap(
            task.inputs.get("max_hypotheses"), maximum=100
        )
        strategy_group = _text(task.inputs.get("strategy_group"), maximum=64)
        return {
            "pages": pages,
            "target_countries": target_countries,
            "target_categories": target_categories,
            "excluded_countries": excluded_countries,
            "excluded_categories": excluded_categories,
            "max_signals": max_signals,
            "max_hypotheses": max_hypotheses,
            "strategy_group": strategy_group,
        }

    @staticmethod
    def _validate_output(
        raw: str,
        projection: dict[str, object],
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        if (
            not isinstance(raw, str)
            or len(raw.encode()) > _MAX_OUTPUT_BYTES
            or _EMAIL.search(raw) is not None
            or _contains_phone(raw)
        ):
            raise ValidationError("需求情报模型输出无效")
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            raise ValidationError("需求情报模型输出不是合法 JSON") from None
        if not isinstance(payload, dict) or set(payload) != _OUTPUT_KEYS:
            raise ValidationError("需求情报模型输出含未授权字段")
        raw_signals = payload.get("signals")
        raw_hypotheses = payload.get("hypotheses")
        max_signals = projection["max_signals"]
        max_hypotheses = projection["max_hypotheses"]
        if (
            not isinstance(raw_signals, list)
            or not isinstance(raw_hypotheses, list)
            or not isinstance(max_signals, int)
            or not isinstance(max_hypotheses, int)
            or len(raw_signals) > max_signals
            or len(raw_hypotheses) > max_hypotheses
        ):
            raise ValidationError("需求情报模型输出超过探索上限")
        pages = projection["pages"]
        assert isinstance(pages, tuple)
        signals = [_signal(item, pages) for item in raw_signals]
        hypotheses = [
            _hypothesis(item, signals, projection) for item in raw_hypotheses
        ]
        return signals, hypotheses

    @staticmethod
    def _empty(task: AgentTask, summary: str) -> ChangeSet:
        return ChangeSet(
            change_set_id=ChangeSetId(new_id("cs")),
            tenant_id=task.tenant_id,
            run_id=task.run_id,
            changes=[],
            summary=summary,
        )


def _pages(value: object) -> tuple[dict[str, str], ...]:
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= 50:
        raise ValidationError("需求情报页面输入无效")
    pages: list[dict[str, str]] = []
    total_bytes = 0
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "text",
            "url",
            "observed_at",
            "content_hash",
            "snapshot_artifact_ref",
        }:
            raise ValidationError("需求情报页面输入无效")
        text = _text(item.get("text"), maximum=200_000, multiline=True)
        text = _redact_contacts(text)
        total_bytes += len(text.encode())
        observed = item.get("observed_at")
        if not isinstance(observed, datetime) or observed.tzinfo is not UTC:
            raise ValidationError("需求情报页面观察时间无效")
        url = _text(item.get("url"), maximum=2_048)
        content_hash = _text(item.get("content_hash"), maximum=64)
        artifact_ref = _text(item.get("snapshot_artifact_ref"), maximum=40)
        if (
            _HASH.fullmatch(content_hash) is None
            or _ARTIFACT.fullmatch(artifact_ref) is None
        ):
            raise ValidationError("需求情报页面证据无效")
        pages.append(
            {
                "text": text,
                "url": url,
                "identity_domain": _page_identity_domain(url),
                "observed_at": observed.isoformat(),
                "content_hash": content_hash,
                "snapshot_artifact_ref": artifact_ref,
            }
        )
    if total_bytes > _MAX_TOTAL_PAGE_BYTES:
        raise ValidationError("需求情报页面输入超过上下文预算")
    return tuple(pages)


def _signal(item: object, pages: tuple[dict[str, str], ...]) -> dict[str, object]:
    if not isinstance(item, dict) or set(item) != _SIGNAL_KEYS:
        raise ValidationError("需求信号模型输出无效")
    page_index = item.get("source_page_index")
    if type(page_index) is not int or not 0 <= page_index < len(pages):
        raise ValidationError("需求信号页面引用越界")
    excerpt = _text(item.get("source_excerpt"), maximum=2_000, multiline=True)
    if excerpt not in pages[page_index]["text"]:
        raise ValidationError("需求信号摘录不属于来源页面")
    evidence_level = item.get("evidence_level")
    if not isinstance(evidence_level, str) or evidence_level not in _WEB_EVIDENCE_LEVELS:
        raise ValidationError("需求信号证据等级无效")
    possible_need = item.get("possible_need")
    if possible_need is not None:
        possible_need = _text(possible_need, maximum=500)
        _reject_inference_numbers(possible_need)
    return {
        "signal_type": _text(item.get("signal_type"), maximum=100),
        "entity_name": pages[page_index]["identity_domain"],
        "source_page_index": page_index,
        "source_excerpt": excerpt,
        "possible_need": possible_need,
        "evidence_level": evidence_level,
    }


def _hypothesis(
    item: object,
    signals: list[dict[str, object]],
    projection: dict[str, object],
) -> dict[str, object]:
    if not isinstance(item, dict) or set(item) != _HYPOTHESIS_KEYS:
        raise ValidationError("需求假设模型输出无效")
    raw_indexes = item.get("signal_indexes")
    if not isinstance(raw_indexes, list) or not raw_indexes:
        raise ValidationError("需求假设缺少信号引用")
    if any(type(index) is not int for index in raw_indexes):
        raise ValidationError("需求假设信号引用无效")
    indexes = tuple(dict.fromkeys(raw_indexes))
    if any(not 0 <= index < len(signals) for index in indexes):
        raise ValidationError("需求假设信号引用越界")
    account_name_index = item.get("account_name_signal_index")
    country_index = item.get("country_signal_index")
    if (
        type(account_name_index) is not int
        or type(country_index) is not int
        or account_name_index not in indexes
        or country_index not in indexes
    ):
        raise ValidationError("需求假设企业字段证据引用无效")
    cited_entities = {signals[index]["entity_name"] for index in indexes}
    if len(cited_entities) != 1:
        raise ValidationError("需求假设不得跨企业合并证据")
    entity_name = next(iter(cited_entities))
    assert isinstance(entity_name, str)
    country = _text(item.get("country"), maximum=2)
    country_quote = signals[country_index].get("source_excerpt")
    if not isinstance(country_quote, str) or re.search(
        rf"(?<![A-Za-z]){re.escape(country)}(?![A-Za-z])",
        country_quote,
        flags=re.IGNORECASE,
    ) is None:
        raise ValidationError("需求假设国家字段证据不支持该国家")
    category = _text(item.get("category"), maximum=100)
    target_countries = projection["target_countries"]
    target_categories = projection["target_categories"]
    if (
        not isinstance(target_countries, tuple)
        or country not in target_countries
        or not isinstance(target_categories, tuple)
        or category not in target_categories
    ):
        raise ValidationError("需求假设超出已确认探索范围")
    raw_reasoning = _text(item.get("reasoning"), maximum=2_000, multiline=True)
    _reject_inference_numbers(raw_reasoning)
    lowered = raw_reasoning.casefold()
    if any(marker in lowered for marker in _CERTAINTY):
        raise ValidationError("需求假设包含无证据确定性断言")
    if "可能" not in raw_reasoning or "值得验证" not in raw_reasoning:
        raise ValidationError("需求假设未明确标注为推断")
    return {
        "entity_name": entity_name,
        "country": country,
        "website_domain": entity_name,
        "category": category,
        "reasoning": raw_reasoning,
        "signal_indexes": indexes,
        "account_name_signal_index": account_name_index,
        "country_signal_index": country_index,
    }


def _domain(value: object) -> str:
    raw = _text(value, maximum=253)
    if any(marker in raw for marker in (":", "/", "?", "#", "@")):
        raise ValidationError("需求假设企业域名无效")
    try:
        canonical = raw.removesuffix(".").encode("idna").decode("ascii").lower()
    except UnicodeError as error:
        raise ValidationError("需求假设企业域名无效") from error
    labels = canonical.split(".")
    if len(labels) < 2 or any(_DOMAIN_LABEL.fullmatch(label) is None for label in labels):
        raise ValidationError("需求假设企业域名无效")
    return canonical


def _page_identity_domain(url: str) -> str:
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        if (
            parsed.scheme.casefold() not in {"http", "https"}
            or not isinstance(hostname, str)
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError
        try:
            ip_address(hostname)
        except ValueError:
            pass
        else:
            raise ValueError
        return _domain(hostname)
    except (ValueError, UnicodeError):
        raise ValidationError("需求情报页面 URL 无法绑定企业身份") from None


def _model_projection(projection: dict[str, object]) -> dict[str, object]:
    pages = projection.get("pages")
    if not isinstance(pages, tuple):
        raise ValidationError("需求情报页面输入无效")
    return {
        **{key: value for key, value in projection.items() if key != "pages"},
        "pages": tuple({"text": page["text"]} for page in pages),
    }


def _countries(value: object, *, required: bool) -> tuple[str, ...]:
    values = _texts(value, maximum=2, required=required)
    if any(_COUNTRY.fullmatch(item) is None for item in values):
        raise ValidationError("需求情报国家集合无效")
    return values


def _texts(
    value: object,
    *,
    maximum: int,
    required: bool,
) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValidationError("需求情报集合输入无效")
    values = tuple(sorted({_text(item, maximum=maximum) for item in value}))
    if (required and not values) or len(values) > 100:
        raise ValidationError("需求情报集合输入无效")
    return values


def _positive_cap(value: object, *, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValidationError("需求情报探索上限无效")
    return value


def _text(value: object, *, maximum: int, multiline: bool = False) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or value != value.strip()
        or any(
            ord(character) < 32
            and (not multiline or character not in {"\n", "\t"})
            for character in value
        )
        or "\x7f" in value
    ):
        raise ValidationError("需求情报文本字段无效")
    return value


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


def _contains_phone(value: str) -> bool:
    return any(
        _DATE_LIKE.fullmatch(match.group(0)) is None
        for match in _PHONE.finditer(value)
    )


def _reject_inference_numbers(value: str) -> None:
    if contains_numeric_probability(value) or _MONEY.search(value) is not None:
        raise ValidationError("需求推断包含概率或最终金额")


def _safe_label(value: object, *, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= maximum
        and value == value.strip()
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


__all__ = ("DemandIntelligenceAgent", "DemandIntelligenceModelPort")
