"""Guardrails —— 模型输出落库前的最后一道闸。

任何一条不过就拒绝并**结构化退回**（让 Agent 知道怎么改），
不是抛裸异常（会被当临时故障盲目重试）。

检查清单见 ``docs/architecture/06-agent-runtime.md`` 第三节。
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
