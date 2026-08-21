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
from shared.errors import ValidationError


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
