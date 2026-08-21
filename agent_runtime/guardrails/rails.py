"""Guardrails —— 模型输出落库前的最后一道闸。

任何一条不过就拒绝并**结构化退回**（让 Agent 知道怎么改），
不是抛裸异常（会被当临时故障盲目重试）。

检查清单见 ``docs/architecture/06-agent-runtime.md`` 第三节。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from agent_runtime.base import ChangeSet
from domains.quotations.service import contains_forbidden_commitment
from shared.errors import ValidationError
from shared.schemas.money import Money


@dataclass(frozen=True)
class RailViolation:
    """一条护栏违规。

    字段：
        rail:        哪条护栏
        location:    违规位置（变更条目索引 / 字段路径）
        detail:      具体问题
        how_to_fix:  怎么改（给 Agent 的重试指引）
    """

    rail: str
    location: str
    detail: str
    how_to_fix: str


@dataclass(frozen=True)
class RailResult:
    passed: bool
    violations: list[RailViolation] = field(default_factory=list)


@runtime_checkable
class Rail(Protocol):
    """单条护栏接口。新护栏 = 新实现 + 注册，不改检查器。"""

    name: str

    def check(self, change_set: ChangeSet) -> list[RailViolation]: ...


class EvidenceRequiredRail:
    """检查 Phase 1 模型变更携带可回溯证据。"""

    name = "evidence_required"

    def check(self, change_set: ChangeSet) -> list[RailViolation]:
        violations: list[RailViolation] = []
        for index, change in enumerate(change_set.changes):
            if not isinstance(change, Mapping):
                continue
            domain = change.get("domain")
            operation = change.get("operation")
            payload = change.get("payload")
            base = f"changes[{index}].payload"
            if not isinstance(payload, Mapping):
                continue
            if domain == "demand" and operation == "capture_signal":
                violations.extend(self._check_signal(payload, base))
            elif domain == "demand" and operation == "create_hypothesis":
                violations.extend(self._check_hypothesis(payload, base))
            elif domain == "demand" and operation == "update_need_fields":
                violations.extend(self._check_need_fields(payload, base))
            elif (
                domain == "prospecting"
                and operation == "resolve_account"
                and not self._nonempty_sequence(payload.get("source_signal_refs"))
            ):
                violations.append(self._missing(f"{base}.source_signal_refs"))
        return violations

    def _check_signal(
        self, payload: Mapping[object, object], base: str
    ) -> list[RailViolation]:
        required = ("source_type", "source_id", "observed_at", "extracted_by")
        if payload.get("source_type") == "web_page":
            required += ("source_url", "page_hash", "snapshot_artifact_ref")
        return [
            self._missing(f"{base}.{field_name}")
            for field_name in required
            if not self._nonblank(payload.get(field_name))
        ]

    def _check_hypothesis(
        self, payload: Mapping[object, object], base: str
    ) -> list[RailViolation]:
        violations: list[RailViolation] = []
        signal_indexes = payload.get("signal_indexes")
        evidence_levels = payload.get("evidence_levels")
        has_indexes = self._nonempty_sequence(signal_indexes)
        has_levels = self._nonempty_sequence(evidence_levels)
        if not has_indexes:
            violations.append(self._missing(f"{base}.signal_indexes"))
        if not has_levels or (
            has_indexes and len(evidence_levels) != len(signal_indexes)
        ):
            violations.append(self._missing(f"{base}.evidence_levels"))
        if not self._nonblank(payload.get("inferred_by")):
            violations.append(self._missing(f"{base}.inferred_by"))
        return violations

    def _check_need_fields(
        self, payload: Mapping[object, object], base: str
    ) -> list[RailViolation]:
        violations: list[RailViolation] = []
        if not self._nonblank(payload.get("message_id")):
            violations.append(self._missing(f"{base}.message_id"))
        fields = payload.get("fields")
        if not self._nonempty_sequence(fields):
            violations.append(self._missing(f"{base}.fields"))
            return violations
        for index, item in enumerate(fields):
            quote = item.get("quote") if isinstance(item, Mapping) else None
            if not self._nonblank(quote):
                violations.append(self._missing(f"{base}.fields[{index}].quote"))
        return violations

    @staticmethod
    def _nonblank(value: object) -> bool:
        return isinstance(value, str) and bool(value.strip())

    @staticmethod
    def _nonempty_sequence(value: object) -> bool:
        return isinstance(value, (list, tuple)) and bool(value)

    def _missing(self, location: str) -> RailViolation:
        return RailViolation(
            rail=self.name,
            location=location,
            detail="模型变更缺少可回溯证据",
            how_to_fix="补充原始来源引用或客户原话后重新生成变更",
        )


class FactInferenceSeparationRail:
    """防止事实与推断共用同一个持久化字段形状。"""

    name = "fact_inference_separation"

    def check(self, change_set: ChangeSet) -> list[RailViolation]:
        violations: list[RailViolation] = []
        for index, change in enumerate(change_set.changes):
            violations.extend(self._walk(change, f"changes[{index}]"))
        return violations

    def _walk(self, value: object, location: str) -> list[RailViolation]:
        if isinstance(value, Mapping):
            has_value = "value" in value
            has_provenance = "provenance" in value
            has_basis = "based_on" in value
            if has_value and has_provenance and has_basis:
                return [
                    self._violation(
                        location,
                        "同一字段同时声明事实来源和推断依据",
                        "把直接观察值和推断结论拆成两个字段",
                    )
                ]
            if has_value and has_provenance:
                provenance = value.get("provenance")
                source_type = (
                    provenance.get("source_type")
                    if isinstance(provenance, Mapping)
                    else None
                )
                source_value = getattr(source_type, "value", source_type)
                if source_value == "agent_inference":
                    return [
                        self._violation(
                            f"{location}.provenance.source_type",
                            "事实字段使用了 Agent 推断来源",
                            "改为 InferredField，并通过 based_on 引用证据",
                        )
                    ]
                return []
            violations: list[RailViolation] = []
            for key, child in value.items():
                violations.extend(self._walk(child, f"{location}.{key}"))
            return violations
        if isinstance(value, (list, tuple)):
            violations = []
            for index, child in enumerate(value):
                violations.extend(self._walk(child, f"{location}[{index}]"))
            return violations
        return []

    def _violation(
        self, location: str, detail: str, how_to_fix: str
    ) -> RailViolation:
        return RailViolation(self.name, location, detail, how_to_fix)


class NoForbiddenCommitmentRail:
    """拒绝未经审批的客户可见商业承诺。"""

    name = "no_forbidden_commitment"

    def check(self, change_set: ChangeSet) -> list[RailViolation]:
        violations: list[RailViolation] = []
        for index, change in enumerate(change_set.changes):
            if (
                not isinstance(change, Mapping)
                or change.get("domain") != "outreach"
                or change.get("operation") != "create_draft"
            ):
                continue
            payload = change.get("payload")
            if not isinstance(payload, Mapping):
                violations.append(self._invalid(f"changes[{index}].payload"))
                continue
            approval_ref = change.get("approval_ref", payload.get("approval_ref"))
            if isinstance(approval_ref, str) and approval_ref.strip():
                continue
            seen: set[str] = set()
            for field_name in ("subject", "body"):
                location = f"changes[{index}].payload.{field_name}"
                text = payload.get(field_name)
                if not isinstance(text, str):
                    violations.append(self._invalid(location))
                    continue
                try:
                    commitments = contains_forbidden_commitment(text)
                except ValidationError:
                    violations.append(self._invalid(location))
                    continue
                for commitment in commitments:
                    if commitment.value in seen:
                        continue
                    seen.add(commitment.value)
                    violations.append(
                        RailViolation(
                            rail=self.name,
                            location=location,
                            detail=(
                                "客户可见草稿含未审批承诺："
                                f"{commitment.value}"
                            ),
                            how_to_fix="删除承诺内容，或取得逐次人工审批引用",
                        )
                    )
        return violations

    def _invalid(self, location: str) -> RailViolation:
        return RailViolation(
            rail=self.name,
            location=location,
            detail="客户可见草稿文本无效，已失败关闭",
            how_to_fix="提供完整且无控制字符的 subject/body 后重新检查",
        )


class PriceBasisRail:
    """禁止客户可见变更携带参考价。"""

    name = "price_basis"

    def check(self, change_set: ChangeSet) -> list[RailViolation]:
        violations: list[RailViolation] = []
        customer_visible_operations = {
            ("outreach", "create_draft"),
            ("quotations", "create_draft"),
        }
        for index, change in enumerate(change_set.changes):
            if not isinstance(change, Mapping) or (
                change.get("domain"),
                change.get("operation"),
            ) not in customer_visible_operations:
                continue
            payload = change.get("payload")
            risk_ref = change.get(
                "risk_acceptance_ref",
                payload.get("risk_acceptance_ref")
                if isinstance(payload, Mapping)
                else None,
            )
            if isinstance(risk_ref, str) and risk_ref.strip():
                continue
            violations.extend(self._walk(payload, f"changes[{index}].payload"))
        return violations

    def _walk(self, value: object, location: str) -> list[RailViolation]:
        if isinstance(value, Mapping):
            violations: list[RailViolation] = []
            for key, child in value.items():
                child_location = f"{location}.{key}"
                basis = getattr(child, "value", child)
                if key == "price_basis" and basis == "indicative":
                    violations.append(
                        RailViolation(
                            rail=self.name,
                            location=child_location,
                            detail="客户可见内容引用了 indicative 参考价",
                            how_to_fix=(
                                "改用供应商 quoted 价格，或附人工风险接受记录"
                            ),
                        )
                    )
                else:
                    violations.extend(self._walk(child, child_location))
            return violations
        if isinstance(value, (list, tuple)):
            violations = []
            for index, child in enumerate(value):
                violations.extend(self._walk(child, f"{location}[{index}]"))
            return violations
        return []


class NoModelMoneyRail:
    """禁止模型金额直接进入成本计算。"""

    name = "no_model_money"

    def check(self, change_set: ChangeSet) -> list[RailViolation]:
        violations: list[RailViolation] = []
        for index, change in enumerate(change_set.changes):
            if not isinstance(change, Mapping) or change.get("domain") != "costing":
                continue
            payload = change.get("payload")
            is_pending_suggestion = (
                change.get("operation") == "suggest_cost_item"
                and isinstance(payload, Mapping)
                and payload.get("is_pending_confirmation") is True
                and payload.get("entered_by") is None
            )
            if is_pending_suggestion:
                continue
            violations.extend(self._walk(payload, f"changes[{index}].payload"))
        return violations

    def _walk(self, value: object, location: str) -> list[RailViolation]:
        if isinstance(value, Money) or self._is_serialized_money(value):
            return [
                RailViolation(
                    rail=self.name,
                    location=location,
                    detail="模型金额可能直接进入成本计算",
                    how_to_fix="改为待人工确认的成本建议，或移除模型生成金额",
                )
            ]
        if isinstance(value, Mapping):
            violations: list[RailViolation] = []
            for key, child in value.items():
                violations.extend(self._walk(child, f"{location}.{key}"))
            return violations
        if isinstance(value, (list, tuple)):
            violations = []
            for index, child in enumerate(value):
                violations.extend(self._walk(child, f"{location}[{index}]"))
            return violations
        return []

    @staticmethod
    def _is_serialized_money(value: object) -> bool:
        return (
            isinstance(value, Mapping)
            and set(value) == {"amount", "currency"}
            and isinstance(value.get("currency"), str)
            and isinstance(value.get("amount"), (str, Decimal))
        )


class LanguageCheckRail:
    """核对客户内容与目标市场语言。"""

    name = "language_check"

    def check(self, change_set: ChangeSet) -> list[RailViolation]:
        violations: list[RailViolation] = []
        for index, change in enumerate(change_set.changes):
            if (
                not isinstance(change, Mapping)
                or change.get("domain") != "outreach"
                or change.get("operation") != "create_draft"
            ):
                continue
            payload = change.get("payload")
            base = f"changes[{index}].payload"
            if not isinstance(payload, Mapping):
                violations.append(self._violation(base, "客户草稿缺少语言元数据"))
                continue
            target = self._primary_language(payload.get("target_language"))
            content = self._primary_language(payload.get("content_language"))
            if target is None:
                violations.append(
                    self._violation(
                        f"{base}.target_language", "目标市场语言无效或缺失"
                    )
                )
                continue
            if content is None or content != target:
                violations.append(
                    self._violation(
                        f"{base}.content_language", "客户内容语言与目标语言不一致"
                    )
                )
                continue
            if target == "en":
                for field_name in ("subject", "body"):
                    text = payload.get(field_name)
                    if isinstance(text, str) and _CJK_TEXT.search(text) is not None:
                        violations.append(
                            self._violation(
                                f"{base}.{field_name}", "英文客户内容包含中文正文"
                            )
                        )
        return violations

    @staticmethod
    def _primary_language(value: object) -> str | None:
        if not isinstance(value, str) or _LANGUAGE_TAG.fullmatch(value) is None:
            return None
        return value.split("-", maxsplit=1)[0].lower()

    def _violation(self, location: str, detail: str) -> RailViolation:
        return RailViolation(
            rail=self.name,
            location=location,
            detail=detail,
            how_to_fix="按目标市场语言重写客户可见 subject/body 并声明语言标签",
        )


class TenantConsistencyRail:
    """拒绝 Change Set 中任何越出顶层租户边界的嵌套数据。"""

    name = "tenant_consistency"

    def check(self, change_set: ChangeSet) -> list[RailViolation]:
        violations: list[RailViolation] = []
        for index, change in enumerate(change_set.changes):
            violations.extend(
                self._walk(change, f"changes[{index}]", change_set.tenant_id)
            )
        return violations

    def _walk(
        self,
        value: object,
        location: str,
        expected_tenant: str,
    ) -> list[RailViolation]:
        violations: list[RailViolation] = []
        if isinstance(value, Mapping):
            for key, child in value.items():
                child_location = f"{location}.{key}"
                if key == "tenant_id" and (
                    not isinstance(child, str) or child != expected_tenant
                ):
                    violations.append(
                        RailViolation(
                            rail=self.name,
                            location=child_location,
                            detail="变更数据的租户与 Change Set 租户不一致",
                            how_to_fix="移除跨租户数据，并用当前租户重新读取业务对象",
                        )
                    )
                else:
                    violations.extend(
                        self._walk(child, child_location, expected_tenant)
                    )
        elif isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                violations.extend(
                    self._walk(child, f"{location}[{index}]", expected_tenant)
                )
        return violations


_PROBABILITY_KEY_PARTS = ("confidence", "probability", "likelihood")
_NUMERIC_VALUE = re.compile(r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)\s*%?")
_PROBABILITY_TEXT = re.compile(
    r"(?:置信度|概率|confidence|probability|likelihood)"
    r"\s*(?:(?:约|为|is|of)\s*|[:=]\s*)?"
    r"(?:\d+(?:\.\d+)?|\.\d+)\s*%?",
)
_LANGUAGE_TAG = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*")
_CJK_TEXT = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


class NoProbabilityOutputRail:
    """禁止把模型产生的数值概率伪装成可测量的业务置信度。"""

    name = "no_probability_output"

    def check(self, change_set: ChangeSet) -> list[RailViolation]:
        violations: list[RailViolation] = []
        for index, change in enumerate(change_set.changes):
            violations.extend(self._walk(change, f"changes[{index}]", None))
        return violations

    def _walk(
        self,
        value: object,
        location: str,
        field_name: str | None,
    ) -> list[RailViolation]:
        violations: list[RailViolation] = []
        if self._is_numeric_probability(field_name, value) or (
            isinstance(value, str)
            and _PROBABILITY_TEXT.search(
                unicodedata.normalize("NFKC", value).casefold()
            )
            is not None
        ):
            violations.append(
                RailViolation(
                    rail=self.name,
                    location=location,
                    detail="模型输出包含数值概率或置信度",
                    how_to_fix="改用证据等级，由确定性代码推导离散置信度",
                )
            )
            return violations
        if isinstance(value, Mapping):
            for key, child in value.items():
                key_text = str(key)
                violations.extend(
                    self._walk(child, f"{location}.{key_text}", key_text)
                )
        elif isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                violations.extend(
                    self._walk(child, f"{location}[{index}]", field_name)
                )
        return violations

    @staticmethod
    def _is_numeric_probability(field_name: str | None, value: object) -> bool:
        if field_name is None:
            return False
        normalized_name = field_name.casefold()
        if not any(part in normalized_name for part in _PROBABILITY_KEY_PARTS):
            return False
        if isinstance(value, bool):
            return False
        if isinstance(value, (int, float, Decimal)):
            return True
        return isinstance(value, str) and _NUMERIC_VALUE.fullmatch(value.strip()) is not None


class GuardrailChecker:
    """护栏检查器。跑全部护栏后一次返回所有违规——
    Agent 需要一次看到全部要改的地方，不是试八轮。

    Phase 1 必备护栏（每条一个 Rail 实现）：

    ``fact_inference_separation``
        推断写进事实字段（FactualField 的 provenance 是
        AGENT_INFERENCE）→ 拦。硬边界 5。

    ``evidence_required``
        无 based_on 的推断、无 provenance 的关键字段 → 拦。硬边界 4/5。

    ``no_probability_output``
        变更里出现 confidence 数值（含藏在自由文本里的「置信度约
        70%」）→ 拦。硬边界 3。

    ``no_forbidden_commitment``
        对外内容命中 quotations.contains_forbidden_commitment 且无
        审批引用 → 拦。规则判定"有承诺"时模型判定"没有"不能翻案——
        漏放一条承诺的代价远大于误拦。

    ``no_model_money``
        模型产出的金额进入计算字段 → 拦（只能进待确认建议区）。
        硬边界 2。

    ``price_basis``
        indicative 价格出现在客户可见内容 → 拦。硬边界 7。

    ``tenant_consistency``
        变更集内出现多个 tenant_id → 拦并告警。硬边界 8。

    ``language_check``
        客户可见内容语言与目标市场不符 → 拦。
    """

    def __init__(self) -> None:
        self._rails: list[Rail] = []
        self._rail_names: set[str] = set()

    def register(self, rail: Rail) -> None:
        try:
            valid = isinstance(rail, Rail)
        except TypeError:
            valid = False
        if not valid or not isinstance(rail.name, str) or not rail.name.strip():
            raise ValidationError("护栏实现无效")
        if rail.name in self._rail_names:
            raise ValidationError("护栏名称重复")
        self._rails.append(rail)
        self._rail_names.add(rail.name)

    def check_all(self, change_set: ChangeSet) -> RailResult:
        if not isinstance(change_set, ChangeSet):
            raise ValidationError("变更集无效")
        violations: list[RailViolation] = []
        for rail in self._rails:
            try:
                result = rail.check(change_set)
                if not isinstance(result, list) or any(
                    not isinstance(item, RailViolation) for item in result
                ):
                    raise TypeError
                violations.extend(result)
            except Exception:  # noqa: BLE001 -- 护栏异常必须失败关闭且不得泄露详情
                violations.append(
                    RailViolation(
                        rail=rail.name,
                        location="change_set",
                        detail="护栏执行失败，变更集已拒绝",
                        how_to_fix="修复护栏后重新检查",
                    )
                )
        return RailResult(passed=not violations, violations=violations)
