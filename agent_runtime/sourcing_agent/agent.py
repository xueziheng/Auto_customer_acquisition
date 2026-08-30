"""受限寻源分析能力：逐项比较人工录入候选，不执行采购动作。"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

from agent_runtime.base import AgentTask, CapabilityAgent, ChangeSet
from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
from agent_runtime.guardrails.rails import guard_phase1_change_set
from agent_runtime.sourcing_agent.extraction import SourcingPageCandidateDraft
from domains.sourcing.service import (
    price_rejection_reason_values,
    spec_match_level_values,
)
from shared.errors import ValidationError
from shared.schemas.identifiers import (
    ChangeSetId,
    SourcingCaseId,
    SupplierCandidateId,
    new_id,
)

_MAX_OUTPUT_BYTES = 200_000
_REVIEW_KEYS = frozenset(
    {
        "case_id",
        "candidate_id",
        "supplier_name",
        "product_title",
        "rung",
        "required_specs",
        "offered_specs",
        "price_checks",
        "evidence",
    }
)
_REQUIRED_SPEC_KEYS = frozenset({"spec_name", "required"})
_OFFERED_SPEC_KEYS = frozenset({"spec_name", "offered"})
_PRICE_CHECK_KEYS = frozenset(
    {
        "far_below_market_without_tier",
        "has_vague_range",
        "has_quantity_tier",
        "unit_clear",
        "currency_clear",
    }
)
_EVIDENCE_KEYS = frozenset(
    {"source_url", "content_hash", "snapshot_artifact_ref", "observed_at"}
)
_OUTPUT_KEYS = frozenset({"comparisons", "summary", "price_rejection_suggestions"})
_COMPARISON_KEYS = frozenset(
    {
        "spec_name",
        "offered",
        "level",
        "substitutable",
        "substitution_impact",
        "needs_customer_confirmation",
    }
)
_PRICE_SUGGESTION_KEYS = frozenset({"reason", "explanation"})
_CONTENT_HASH = re.compile(r"[0-9a-f]{64}")
_ARTIFACT_REF = re.compile(r"art_[0-9A-HJKMNP-TV-Z]{26}")
_ISO_4217_CODES = frozenset(
    [
        "AED",
        "AFN",
        "ALL",
        "AMD",
        "AOA",
        "ARS",
        "AUD",
        "AWG",
        "AZN",
        "BAM",
        "BBD",
        "BDT",
        "BGN",
        "BHD",
        "BIF",
        "BMD",
        "BND",
        "BOB",
        "BOV",
        "BRL",
        "BSD",
        "BTN",
        "BWP",
        "BYN",
        "BZD",
        "CAD",
        "CDF",
        "CHE",
        "CHF",
        "CHW",
        "CLF",
        "CLP",
        "CNY",
        "COP",
        "COU",
        "CRC",
        "CUC",
        "CUP",
        "CVE",
        "CZK",
        "DJF",
        "DKK",
        "DOP",
        "DZD",
        "EGP",
        "ERN",
        "ETB",
        "EUR",
        "FJD",
        "FKP",
        "GBP",
        "GEL",
        "GHS",
        "GIP",
        "GMD",
        "GNF",
        "GTQ",
        "GYD",
        "HKD",
        "HNL",
        "HTG",
        "HUF",
        "IDR",
        "ILS",
        "INR",
        "IQD",
        "IRR",
        "ISK",
        "JMD",
        "JOD",
        "JPY",
        "KES",
        "KGS",
        "KHR",
        "KMF",
        "KPW",
        "KRW",
        "KWD",
        "KYD",
        "KZT",
        "LAK",
        "LBP",
        "LKR",
        "LRD",
        "LSL",
        "LYD",
        "MAD",
        "MDL",
        "MGA",
        "MKD",
        "MMK",
        "MNT",
        "MOP",
        "MRU",
        "MUR",
        "MVR",
        "MWK",
        "MXN",
        "MXV",
        "MYR",
        "MZN",
        "NAD",
        "NGN",
        "NIO",
        "NOK",
        "NPR",
        "NZD",
        "OMR",
        "PAB",
        "PEN",
        "PGK",
        "PHP",
        "PKR",
        "PLN",
        "PYG",
        "QAR",
        "RON",
        "RSD",
        "RUB",
        "RWF",
        "SAR",
        "SBD",
        "SCR",
        "SDG",
        "SEK",
        "SGD",
        "SHP",
        "SLE",
        "SLL",
        "SOS",
        "SRD",
        "SSP",
        "STN",
        "SVC",
        "SYP",
        "SZL",
        "THB",
        "TJS",
        "TMT",
        "TND",
        "TOP",
        "TRY",
        "TTD",
        "TWD",
        "TZS",
        "UAH",
        "UGX",
        "USD",
        "USN",
        "UYI",
        "UYU",
        "UYW",
        "UZS",
        "VED",
        "VES",
        "VND",
        "VUV",
        "WST",
        "XAF",
        "XAG",
        "XAU",
        "XBA",
        "XBB",
        "XBC",
        "XBD",
        "XCD",
        "XDR",
        "XOF",
        "XPD",
        "XPF",
        "XPT",
        "XSU",
        "XTS",
        "XUA",
        "YER",
        "ZAR",
        "ZMW",
        "ZWG",
        "ZWL",
    ]
)
_ISO_4217_PATTERN = "|".join(sorted(_ISO_4217_CODES))
_PRICE_UNIT_PATTERN = (
    r"(?:bag|bottle|box|carton|case|drum|g|gram|kg|kilogram|l|liter|litre|m|"
    r"meter|metre|ml|pack|pair|pallet|pc|pcs|piece|roll|set|sheet|sqm|ton|"
    r"tonne|unit)s?"
)
_MODEL_MONEY = re.compile(
    rf"(?:[$€£¥₹]\s*\d|\d(?:[\d,.]*\d)?\s*[$€£¥₹])|"
    rf"(?:(?i:\b(?:{_ISO_4217_PATTERN}|RMB)\b)\s*[:=]?\s*\d)|"
    rf"(?:\d(?:[\d,.]*\d)?\s*(?i:\b(?:{_ISO_4217_PATTERN}|RMB)\b))|"
    r"(?:(?i:\b(?:unit[ -]?price|price|cost|amount)\b)[^\d\n]{0,24}\d)|"
    r"(?:\d(?:[\d,.]*\d)?[^\d\n]{0,12}(?i:\b(?:unit[ -]?price|price|cost|amount)\b))|"
    rf"(?:\d(?:[\d,.]*\d)?\s*(?i:(?:/|per)\s*{_PRICE_UNIT_PATTERN}\b))"
)

_SYSTEM_PROMPT = """你是 TradeOS 的候选供应商寻源分析能力。输入只包含人工录入的
客户必需规格、候选供应规格、确定性价格检查和证据快照。逐项比较规格，禁止生成综合
相似度、概率、置信度、价格、报价或采购动作。缺失的 offered 必须标记 unknown，绝不
能标记 exact；different 的可替代性只是建议，需客户确认。

只输出一个 JSON 对象，顶层键必须精确为 comparisons, summary,
price_rejection_suggestions。comparisons 必须逐项覆盖输入规格，每项键精确为 spec_name,
offered, level, substitutable, substitution_impact, needs_customer_confirmation。
price_rejection_suggestions 只能逐项解释输入中确定性检查触发的拒绝原因；它只是人工决策
建议。所有网页价格均为 indicative，禁止写成 quoted。
"""


@runtime_checkable
class SourcingCandidateModelPort(Protocol):
    """结构化候选分析端口；凭证、外部访问和业务写入均不属于该端口。"""

    async def review_candidate(
        self, *, system_prompt: str, candidate: dict[str, object]
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


def _optional_text(value: object, *, field: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _text(value, field=field, maximum=maximum)


def _public_url(value: object) -> str:
    result = _text(value, field="寻源证据来源", maximum=2_000)
    parsed = urlsplit(result)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValidationError("寻源证据来源无效")
    return result


def _utc_timestamp(value: object) -> str:
    raw = _text(value, field="寻源证据观察时间", maximum=64)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        raise ValidationError("寻源证据观察时间无效") from None
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValidationError("寻源证据观察时间无效")
    return raw


def _bool(value: object, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValidationError(f"{field} 无效")
    return value


class SourcingAgent(CapabilityAgent):
    """核验逐项匹配与参考价拒绝建议，再产出待人工处理的变更。"""

    name = "sourcing_agent"

    @staticmethod
    def build_page_candidate_review(
        *,
        draft: SourcingPageCandidateDraft,
        case_id: SourcingCaseId,
        candidate_id: SupplierCandidateId,
    ) -> dict[str, object]:
        """把安全页面观察值确定性映射到既有逐项解释入口。"""

        if not isinstance(draft, SourcingPageCandidateDraft):
            raise ValidationError("公开寻源候选草稿无效")
        if draft.supplier_name is None or draft.product_title is None:
            raise ValidationError("公开寻源候选缺少供应商或产品标题")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValidationError("寻源案例引用无效")
        if not isinstance(candidate_id, str) or not candidate_id.strip():
            raise ValidationError("寻源候选引用无效")
        has_tiers = bool(draft.price_tiers)
        review = {
            "case_id": str(case_id),
            "candidate_id": str(candidate_id),
            "supplier_name": draft.supplier_name.literal,
            "product_title": draft.product_title.literal,
            "rung": 6,
            "required_specs": tuple(
                {"spec_name": item.spec_name, "required": item.required}
                for item in draft.specs
            ),
            "offered_specs": tuple(
                {
                    "spec_name": item.spec_name,
                    "offered": (
                        item.observed.literal if item.observed is not None else None
                    ),
                }
                for item in draft.specs
            ),
            "price_checks": {
                "far_below_market_without_tier": False,
                "has_vague_range": any(
                    "vague_range" in tier.rejection_reasons
                    for tier in draft.price_tiers
                ),
                "has_quantity_tier": has_tiers
                and all(
                    tier.minimum_quantity is not None for tier in draft.price_tiers
                ),
                "unit_clear": has_tiers
                and all(tier.unit is not None for tier in draft.price_tiers),
                "currency_clear": has_tiers
                and all(tier.currency is not None for tier in draft.price_tiers),
            },
            "evidence": {
                "source_url": draft.evidence.source_url,
                "content_hash": draft.evidence.content_hash,
                "snapshot_artifact_ref": str(draft.evidence.snapshot_artifact_ref),
                "observed_at": draft.evidence.observed_at.isoformat(),
            },
        }
        return review

    def __init__(
        self,
        model: str,
        model_client: Any,
        gateway: Any,
        guardrails: Any,
    ) -> None:
        self._model = _text(model, field="寻源分析模型标识", maximum=128)
        if not isinstance(model_client, SourcingCandidateModelPort):
            raise ValidationError("寻源候选分析模型端口无效")
        if not isinstance(guardrails, CredentialMarkerGuard):
            raise ValidationError("寻源分析输入护栏无效")
        self._model_port = model_client
        self._gateway = gateway
        self._input_guard = guardrails

    async def run(self, task: AgentTask, context: object) -> ChangeSet:
        """分析人工候选；输入或输出失败时返回带原因的安全空变更集。"""
        del context
        if not isinstance(task, AgentTask):
            raise ValidationError("寻源分析任务无效")
        try:
            projection = self._safe_projection(task)
            self._input_guard.check(
                subject="寻源候选分析",
                body=json.dumps(projection, ensure_ascii=False, sort_keys=True),
            )
        except ValidationError:
            return self._empty(task, "寻源分析输入被安全边界拒绝")
        try:
            raw = await self._model_port.review_candidate(
                system_prompt=_SYSTEM_PROMPT,
                candidate=projection,
            )
            output = self._validate_output(raw, projection)
        except ValidationError as exc:
            return self._empty(task, f"模型输出被护栏拦截：{exc}")

        comparisons = output["comparisons"]
        suggestions = output["price_rejection_suggestions"]
        assert isinstance(comparisons, tuple) and isinstance(suggestions, tuple)
        evidence = projection["evidence"]
        assert isinstance(evidence, dict)
        provenance = {
            "source_url": evidence["source_url"],
            "content_hash": evidence["content_hash"],
            "snapshot_artifact_ref": evidence["snapshot_artifact_ref"],
            "observed_at": evidence["observed_at"],
        }
        changes: list[dict[str, object]] = [
            {
                "domain": "sourcing",
                "operation": "record_match_explanation",
                "payload": {
                    "case_id": projection["case_id"],
                    "candidate_id": projection["candidate_id"],
                    "rung": projection["rung"],
                    "comparisons": comparisons,
                    "summary": output["summary"],
                    "price_basis": "indicative",
                    "evidence": provenance,
                    "generated_by": self._model,
                },
                "risk_level": "low",
            }
        ]
        if suggestions:
            changes.append(
                {
                    "domain": "sourcing",
                    "operation": "suggest_candidate_rejection",
                    "payload": {
                        "case_id": projection["case_id"],
                        "candidate_id": projection["candidate_id"],
                        "reasons": tuple(str(item["reason"]) for item in suggestions),
                        "explanations": tuple(
                            str(item["explanation"]) for item in suggestions
                        ),
                        "is_pending_human_decision": True,
                        "price_basis": "indicative",
                        "evidence": provenance,
                        "generated_by": self._model,
                    },
                    "risk_level": "medium",
                }
            )
        candidate = ChangeSet(
            change_set_id=ChangeSetId(new_id("cs")),
            tenant_id=task.tenant_id,
            run_id=task.run_id,
            changes=changes,
            summary="已生成候选供应商逐项匹配与参考价复核建议",
        )
        return guard_phase1_change_set(candidate)

    @staticmethod
    def _safe_projection(task: AgentTask) -> dict[str, object]:
        if set(task.inputs) != {"candidate_review"}:
            raise ValidationError("寻源分析任务输入无效")
        review = task.inputs.get("candidate_review")
        if not isinstance(review, dict) or set(review) != _REVIEW_KEYS:
            raise ValidationError("寻源分析任务输入无效")

        raw_required = review.get("required_specs")
        raw_offered = review.get("offered_specs")
        if (
            not isinstance(raw_required, (list, tuple))
            or not 1 <= len(raw_required) <= 50
            or not isinstance(raw_offered, (list, tuple))
            or len(raw_offered) != len(raw_required)
        ):
            raise ValidationError("寻源规格无效")
        required_specs: list[dict[str, str]] = []
        required_names: set[str] = set()
        for item in raw_required:
            if not isinstance(item, dict) or set(item) != _REQUIRED_SPEC_KEYS:
                raise ValidationError("寻源必需规格无效")
            name = _text(item.get("spec_name"), field="寻源规格名", maximum=100)
            if name in required_names:
                raise ValidationError("寻源规格名重复")
            required_names.add(name)
            required_specs.append(
                {
                    "spec_name": name,
                    "required": _text(
                        item.get("required"), field="寻源客户规格", maximum=4_000
                    ),
                }
            )
        offered_specs: list[dict[str, str | None]] = []
        offered_names: set[str] = set()
        for item in raw_offered:
            if not isinstance(item, dict) or set(item) != _OFFERED_SPEC_KEYS:
                raise ValidationError("寻源供应规格无效")
            name = _text(item.get("spec_name"), field="寻源规格名", maximum=100)
            if name in offered_names:
                raise ValidationError("寻源规格名重复")
            offered_names.add(name)
            offered_specs.append(
                {
                    "spec_name": name,
                    "offered": _optional_text(
                        item.get("offered"), field="寻源供应规格", maximum=4_000
                    ),
                }
            )
        if offered_names != required_names:
            raise ValidationError("寻源供应规格未逐项覆盖客户规格")

        raw_checks = review.get("price_checks")
        if not isinstance(raw_checks, dict) or set(raw_checks) != _PRICE_CHECK_KEYS:
            raise ValidationError("寻源参考价检查无效")
        price_checks = {
            key: _bool(raw_checks.get(key), field="寻源参考价检查")
            for key in sorted(_PRICE_CHECK_KEYS)
        }
        raw_evidence = review.get("evidence")
        if not isinstance(raw_evidence, dict) or set(raw_evidence) != _EVIDENCE_KEYS:
            raise ValidationError("寻源候选证据无效")
        content_hash = _text(
            raw_evidence.get("content_hash"), field="寻源证据哈希", maximum=64
        )
        artifact_ref = _text(
            raw_evidence.get("snapshot_artifact_ref"),
            field="寻源证据快照引用",
            maximum=200,
        )
        if (
            _CONTENT_HASH.fullmatch(content_hash) is None
            or _ARTIFACT_REF.fullmatch(artifact_ref) is None
        ):
            raise ValidationError("寻源候选证据无效")
        rung = review.get("rung")
        if isinstance(rung, bool) or not isinstance(rung, int) or not 1 <= rung <= 7:
            raise ValidationError("寻源匹配梯级无效")
        return {
            "case_id": _text(review.get("case_id"), field="寻源案例引用", maximum=200),
            "candidate_id": _text(
                review.get("candidate_id"), field="寻源候选引用", maximum=200
            ),
            "supplier_name": _text(
                review.get("supplier_name"), field="候选供应商名称", maximum=300
            ),
            "product_title": _text(
                review.get("product_title"), field="候选产品标题", maximum=1_000
            ),
            "rung": rung,
            "required_specs": tuple(required_specs),
            "offered_specs": tuple(offered_specs),
            "price_checks": price_checks,
            "evidence": {
                "source_url": _public_url(raw_evidence.get("source_url")),
                "content_hash": content_hash,
                "snapshot_artifact_ref": artifact_ref,
                "observed_at": _utc_timestamp(raw_evidence.get("observed_at")),
            },
        }

    @staticmethod
    def _validate_output(raw: str, projection: dict[str, object]) -> dict[str, object]:
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > _MAX_OUTPUT_BYTES:
            raise ValidationError("寻源分析模型输出无效")
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            raise ValidationError("寻源分析模型输出不是合法 JSON") from None
        if not isinstance(payload, dict) or set(payload) != _OUTPUT_KEYS:
            raise ValidationError("寻源分析模型输出含未授权字段")
        raw_comparisons = payload.get("comparisons")
        required_specs = projection["required_specs"]
        offered_specs = projection["offered_specs"]
        assert isinstance(required_specs, tuple) and isinstance(offered_specs, tuple)
        if not isinstance(raw_comparisons, list) or len(raw_comparisons) != len(
            required_specs
        ):
            raise ValidationError("寻源逐项匹配未覆盖全部规格")
        required_by_name = {
            str(item["spec_name"]): str(item["required"]) for item in required_specs
        }
        offered_by_name = {
            str(item["spec_name"]): item["offered"] for item in offered_specs
        }
        levels = frozenset(spec_match_level_values())
        comparisons_by_name: dict[str, dict[str, object]] = {}
        for item in raw_comparisons:
            if not isinstance(item, dict) or set(item) != _COMPARISON_KEYS:
                raise ValidationError("寻源分析模型输出含未授权字段")
            name = _text(item.get("spec_name"), field="寻源规格名", maximum=100)
            if name not in required_by_name or name in comparisons_by_name:
                raise ValidationError("寻源逐项匹配引用越界或重复")
            offered = item.get("offered")
            if offered != offered_by_name[name]:
                raise ValidationError("模型改写了供应规格事实")
            level = _text(item.get("level"), field="寻源匹配等级", maximum=32)
            if level not in levels:
                raise ValidationError("寻源匹配等级无效")
            substitutable = item.get("substitutable")
            impact = item.get("substitution_impact")
            confirmation = item.get("needs_customer_confirmation")
            if not isinstance(confirmation, bool):
                raise ValidationError("寻源客户确认标记无效")
            if offered is None and level == "exact":
                raise ValidationError("未知供应规格不得标记为完全匹配")
            if level == "exact" and (
                substitutable is not None or impact is not None or confirmation
            ):
                raise ValidationError("完全匹配规格含矛盾的替代说明")
            if level == "unknown" and (
                substitutable is not None or impact is not None or not confirmation
            ):
                raise ValidationError("未知规格必须等待确认且不得猜测替代性")
            if level == "different":
                if not isinstance(substitutable, bool):
                    raise ValidationError("不同规格缺少可替代性判断")
                if substitutable and (
                    not isinstance(impact, str)
                    or not impact.strip()
                    or not confirmation
                ):
                    raise ValidationError("可替代规格缺少影响或客户确认")
                if impact is not None:
                    impact = _text(impact, field="寻源替代影响", maximum=4_000)
            comparisons_by_name[name] = {
                "spec_name": name,
                "required": required_by_name[name],
                "offered": offered,
                "level": level,
                "substitutable": substitutable,
                "substitution_impact": impact,
                "needs_customer_confirmation": confirmation,
            }
        if set(comparisons_by_name) != set(required_by_name):
            raise ValidationError("寻源逐项匹配未覆盖全部规格")

        expected_reasons = SourcingAgent._deterministic_price_reasons(projection)
        raw_suggestions = payload.get("price_rejection_suggestions")
        if not isinstance(raw_suggestions, list):
            raise ValidationError("寻源参考价拒绝建议无效")
        allowed_reasons = frozenset(price_rejection_reason_values())
        suggestions_by_reason: dict[str, dict[str, str]] = {}
        for item in raw_suggestions:
            if not isinstance(item, dict) or set(item) != _PRICE_SUGGESTION_KEYS:
                raise ValidationError("寻源分析模型输出含未授权字段")
            reason = _text(item.get("reason"), field="寻源参考价拒绝原因", maximum=64)
            if reason not in allowed_reasons or reason in suggestions_by_reason:
                raise ValidationError("寻源参考价拒绝建议无效")
            suggestions_by_reason[reason] = {
                "reason": reason,
                "explanation": _text(
                    item.get("explanation"),
                    field="寻源参考价拒绝说明",
                    maximum=4_000,
                ),
            }
        if set(suggestions_by_reason) != set(expected_reasons):
            raise ValidationError("价格拒绝建议与确定性检查不一致")
        ordered_suggestions = tuple(
            suggestions_by_reason[reason] for reason in expected_reasons
        )
        summary = _text(payload.get("summary"), field="寻源逐项匹配摘要", maximum=8_000)
        model_text = [
            summary,
            *(
                str(item["substitution_impact"])
                for item in comparisons_by_name.values()
                if item["substitution_impact"] is not None
            ),
            *(item["explanation"] for item in ordered_suggestions),
        ]
        if any(_MODEL_MONEY.search(value) is not None for value in model_text):
            raise ValidationError("寻源分析不得生成价格")
        return {
            "comparisons": tuple(
                comparisons_by_name[str(item["spec_name"])] for item in required_specs
            ),
            "summary": summary,
            "price_rejection_suggestions": ordered_suggestions,
        }

    @staticmethod
    def _deterministic_price_reasons(
        projection: dict[str, object],
    ) -> tuple[str, ...]:
        checks = projection["price_checks"]
        assert isinstance(checks, dict)
        rules = (
            ("far_below_market_without_tier", True, "bait_price"),
            ("has_vague_range", True, "vague_range"),
            ("has_quantity_tier", False, "quantity_tier_missing"),
            ("unit_clear", False, "unit_unclear"),
            ("currency_clear", False, "currency_unclear"),
        )
        return tuple(reason for key, trigger, reason in rules if checks[key] is trigger)

    @staticmethod
    def _empty(task: AgentTask, summary: str) -> ChangeSet:
        return ChangeSet(
            change_set_id=ChangeSetId(new_id("cs")),
            tenant_id=task.tenant_id,
            run_id=task.run_id,
            changes=[],
            summary=summary,
        )


__all__ = ("SourcingAgent", "SourcingCandidateModelPort")
