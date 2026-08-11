"""检查管线。

顺序固定，理由见 AGENTS.md：便宜且否决率高的在前；
幂等必须在执行前——顺序错了会重复发送。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from shared.errors import ValidationError
from shared.schemas.identifiers import IdempotencyKey, RunId, TenantId, UserId

from .errors import ToolCallStatus, ToolErrorCategory

type SafeScalar = str | int | bool | None
_LABEL_RE = re.compile(r"[a-z][a-z0-9_.:-]{0,99}")
_AUDIT_VALUE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}")
_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")
_CANONICAL_ID_RE = re.compile(r"[a-z]{2,8}_[0-7][0-9A-HJKMNP-TV-Z]{25}")
_SECRET_TOKENS = frozenset(
    {
        "recipient",
        "sender",
        "subject",
        "body",
        "header",
        "headers",
        "token",
        "secret",
        "password",
        "authorization",
        "bearer",
        "cookie",
        "dsn",
        "address",
        "url",
    }
)
_SAFE_OUTPUT_KEYS = frozenset(
    {"provider_ref", "already_existed", "duplicate", "retry_after_seconds"}
)
_SECRET_AUDIT_KEYS = frozenset(
    {
        "api_key",
        "access_key",
        "private_key",
        "oauth_credential",
        "oauth_token",
        "client_secret",
    }
)
_TRANSIENT_RESULT_CATEGORIES = frozenset(
    {
        ToolErrorCategory.IN_PROGRESS,
        ToolErrorCategory.RATE_LIMITED,
        ToolErrorCategory.PROVIDER_AUTH_REQUIRED,
        ToolErrorCategory.PROVIDER_TRANSIENT,
        ToolErrorCategory.RECONCILIATION_REQUIRED,
    }
)
_PERMANENT_RESULT_CATEGORIES = frozenset(ToolErrorCategory) - (
    _TRANSIENT_RESULT_CATEGORIES
)


@dataclass(frozen=True)
class ToolCallContext:
    """一次工具调用的上下文。

    字段：
        tenant_id, user_id
        run_id:           发起的 Agent Run（人工操作为 None）
        tool_id
        params
        idempotency_key:  manifest 要求 REQUIRED 时必填
        approval_ref:     已批准的审批引用（approval stage 校验）
        campaign_ref:     所属 Campaign（边界内发送的依据）
    """

    tenant_id: TenantId
    user_id: UserId
    tool_id: str
    params: Mapping[str, Any]
    run_id: RunId | None = None
    idempotency_key: IdempotencyKey | None = None
    approval_ref: str | None = None
    campaign_ref: str | None = None

    def __post_init__(self) -> None:
        _require_label(self.tool_id, "tool_id")
        if not isinstance(self.tenant_id, str) or not self.tenant_id:
            raise ValidationError("tenant_id 无效")
        if not isinstance(self.user_id, str) or not self.user_id:
            raise ValidationError("user_id 无效")
        if not isinstance(self.params, Mapping):
            raise ValidationError("tool params 无效")
        copied: dict[str, Any] = {}
        for key, value in self.params.items():
            if not isinstance(key, str) or _LABEL_RE.fullmatch(key) is None:
                raise ValidationError("tool param key 无效")
            copied[key] = value
        object.__setattr__(self, "params", MappingProxyType(copied))


@dataclass(frozen=True)
class PreparedToolCall:
    """只读准备结果；payload 仅在当前进程存活且永不 repr。"""

    request_fingerprint: str
    fingerprint_version: str
    audit_projection: Mapping[str, SafeScalar]
    payload: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.request_fingerprint, str)
            or _FINGERPRINT_RE.fullmatch(self.request_fingerprint) is None
        ):
            raise ValidationError("request fingerprint 无效")
        _require_label(self.fingerprint_version, "fingerprint_version")
        if not isinstance(self.audit_projection, Mapping):
            raise ValidationError("audit projection 无效")
        object.__setattr__(
            self,
            "audit_projection",
            _freeze_safe_mapping(self.audit_projection, audit=True),
        )


@dataclass(frozen=True)
class CheckRejection:
    """结构化拒绝 —— **不是异常**。

    Agent 要靠它决定下一步：``remediation`` 说明能否补救以及怎么补
    （"联系方式未验证 → 先调 contact.verify"）。裸异常会被当成
    临时故障重试，那正是最不该发生的。

    字段：
        stage:        哪个 stage 拒的
        rule:         哪条规则
        reason:       人类可读原因
        remediation:  怎么补救（None = 不可补救，别再试）
    """

    stage: str
    rule: str
    reason: str
    remediation: str | None = None

    def __post_init__(self) -> None:
        _require_label(self.stage, "rejection stage")
        _require_label(self.rule, "rejection rule")
        _require_safe_text(self.reason, "rejection reason", max_length=300)
        if self.remediation is not None:
            _require_safe_text(
                self.remediation,
                "rejection remediation",
                max_length=300,
            )


@dataclass(frozen=True)
class ToolCallResult:
    """调用结果。

    ``rejected`` 与 ``output`` 互斥。``duplicate_of`` 非 None 表示
    幂等命中，``output`` 是上次的结果——这是正常路径，不是错误。
    """

    tool_id: str
    status: ToolCallStatus
    output: Mapping[str, SafeScalar] | None = None
    rejected: CheckRejection | None = None
    duplicate_of: str | None = None
    tool_call_id: str | None = None
    cost_note: str | None = None
    error_category: ToolErrorCategory | None = None
    retry_after_seconds: int | None = None

    def __post_init__(self) -> None:
        _require_label(self.tool_id, "tool_id")
        if not isinstance(self.status, ToolCallStatus):
            raise ValidationError("tool call status 无效")
        if self.rejected is not None and not isinstance(self.rejected, CheckRejection):
            raise ValidationError("tool rejection 无效")
        if self.error_category is not None and not isinstance(
            self.error_category, ToolErrorCategory
        ):
            raise ValidationError("tool error category 无效")
        if self.tool_call_id is not None:
            _require_canonical_id(self.tool_call_id, "tool_call_id", prefix="tcl")
        if self.duplicate_of is not None:
            _require_canonical_id(self.duplicate_of, "duplicate_of", prefix="tcl")
        if self.cost_note is not None:
            _require_label(self.cost_note, "cost_note")
        if self.retry_after_seconds is not None and (
            not isinstance(self.retry_after_seconds, int)
            or isinstance(self.retry_after_seconds, bool)
            or not 1 <= self.retry_after_seconds <= 86_400
        ):
            raise ValidationError("retry_after_seconds 无效")
        if self.status is ToolCallStatus.FAILED_TRANSIENT:
            if self.error_category not in _TRANSIENT_RESULT_CATEGORIES:
                raise ValidationError("transient result 缺少匹配错误分类")
        elif self.status is ToolCallStatus.FAILED_PERMANENT:
            if self.error_category not in _PERMANENT_RESULT_CATEGORIES:
                raise ValidationError("permanent result 缺少匹配错误分类")
        elif self.status is not ToolCallStatus.REJECTED and self.error_category is not None:
            raise ValidationError("非失败结果不能携带错误分类")
        if self.retry_after_seconds is not None and (
            self.status is not ToolCallStatus.FAILED_TRANSIENT
            or self.error_category not in _TRANSIENT_RESULT_CATEGORIES
        ):
            raise ValidationError("非临时失败不能携带重试时间")
        if self.status is ToolCallStatus.REJECTED:
            if self.rejected is None or self.duplicate_of is not None:
                raise ValidationError("rejected result 字段不匹配")
        elif self.status is ToolCallStatus.DUPLICATE:
            if self.duplicate_of is None or self.rejected is not None:
                raise ValidationError("duplicate result 字段不匹配")
        elif self.rejected is not None or self.duplicate_of is not None:
            raise ValidationError("tool result 状态字段不匹配")
        if self.output is not None:
            if self.status not in {ToolCallStatus.SUCCEEDED, ToolCallStatus.DUPLICATE}:
                raise ValidationError("非成功结果不能携带 output")
            object.__setattr__(
                self,
                "output",
                _freeze_safe_mapping(self.output, audit=False),
            )


@runtime_checkable
class CheckStage(Protocol):
    """检查 stage 接口。每个 stage 一个实现，见 ``checks/``。

    返回 None = 通过；返回 ``CheckRejection`` = 拒绝并终止管线。
    stage 内部**只读**——检查不产生副作用（幂等 stage 的占位写入
    是唯一例外，它必须原子）。
    """

    name: str

    async def check(self, ctx: ToolCallContext) -> CheckRejection | None: ...


STAGE_ORDER: tuple[str, ...] = (
    "tenant",
    "permission",
    "playbook",
    "country_policy",
    "suppression",
    "approval",
    "idempotency",
    "rate_limit",
)
"""Stage 顺序。**不可随意调换**：

- tenant/permission 是内存判断，最便宜，放最前
- suppression/idempotency 要查库，居中
- approval 可能等人工，放执行前最后一段
- idempotency 在 rate_limit 之前：幂等命中直接返回旧结果，
  不该消耗限额
"""


class ToolGateway:
    """网关入口。"""

    async def invoke(self, ctx: ToolCallContext) -> ToolCallResult:
        """执行一次工具调用。

        实现要求：
        1. 从注册表取 manifest + handler（未注册 → 拒绝）
        2. 按 ``manifest.checks`` 的交集依 ``STAGE_ORDER`` 逐个跑，
           任一拒绝立即返回（仍要写审计）
        3. 幂等命中 → 返回旧结果，不执行、不重复计费
        4. 执行 handler，捕获错误分类（可重试 / 不可重试）
        5. **写 tool_call 审计（含被拒的调用）**；审计写入失败必须
           让整个调用失败——审计不完整时继续发客户邮件是合规裸奔
        6. 记录成本（Phase 1 只记录，Phase 3 接结算）
        """
        raise NotImplementedError


def _require_safe_text(value: object, field_name: str, *, max_length: int) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= max_length
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValidationError(f"{field_name} 无效")
    return value


def _require_label(value: object, field_name: str) -> str:
    label = _require_safe_text(value, field_name, max_length=100)
    if _LABEL_RE.fullmatch(label) is None:
        raise ValidationError(f"{field_name} 无效")
    tokens = frozenset(re.split(r"[_.:-]", label.casefold()))
    if tokens & _SECRET_TOKENS:
        raise ValidationError(f"{field_name} 无效")
    return label


def _safe_key(key: object, *, audit: bool) -> str:
    if not isinstance(key, str) or _LABEL_RE.fullmatch(key) is None:
        raise ValidationError("safe mapping key 无效")
    tokens = frozenset(re.split(r"[_.:-]", key.casefold()))
    if audit:
        sensitive = tokens & _SECRET_TOKENS
        if key.casefold() in _SECRET_AUDIT_KEYS or (
            sensitive
            and not (
            key.endswith("_bytes")
            and len(sensitive) == 1
            and next(iter(sensitive)) in {"recipient", "sender", "subject", "body"}
            )
        ):
            raise ValidationError("audit projection key 无效")
    elif key not in _SAFE_OUTPUT_KEYS and not key.endswith("_id"):
        raise ValidationError("tool output key 无效")
    return key


def _freeze_safe_mapping(
    values: Mapping[str, SafeScalar],
    *,
    audit: bool,
) -> Mapping[str, SafeScalar]:
    copied: dict[str, SafeScalar] = {}
    for raw_key, value in values.items():
        key = _safe_key(raw_key, audit=audit)
        if value is not None and not isinstance(value, (str, int, bool)):
            raise ValidationError("safe mapping value 无效")
        if isinstance(value, str):
            if audit:
                _require_audit_string(value, key)
            else:
                _require_safe_text(value, key, max_length=200)
        if isinstance(value, int) and not isinstance(value, bool) and value < 0:
            raise ValidationError("safe mapping integer 无效")
        if not audit:
            if key in {"already_existed", "duplicate"} and not isinstance(value, bool):
                raise ValidationError("tool output boolean 无效")
            if key == "retry_after_seconds" and (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 1 <= value <= 86_400
            ):
                raise ValidationError("tool output retry 无效")
            if key == "provider_ref" and not isinstance(value, str):
                raise ValidationError("tool output provider ref 无效")
            if key == "provider_ref" and isinstance(value, str):
                _require_provider_ref(value)
            if key.endswith("_id"):
                _require_canonical_id(value, key)
        copied[key] = value
    return MappingProxyType(copied)


def _require_audit_string(value: str, field_name: str) -> str:
    _require_safe_text(value, field_name, max_length=200)
    if _AUDIT_VALUE_RE.fullmatch(value) is None:
        raise ValidationError(f"{field_name} 无效")
    tokens = frozenset(re.split(r"[_.:-]", value.casefold()))
    if tokens & _SECRET_TOKENS:
        raise ValidationError(f"{field_name} 无效")
    return value


def _require_canonical_id(
    value: object,
    field_name: str,
    *,
    prefix: str | None = None,
) -> str:
    if (
        not isinstance(value, str)
        or _CANONICAL_ID_RE.fullmatch(value) is None
        or (prefix is not None and not value.startswith(f"{prefix}_"))
    ):
        raise ValidationError(f"{field_name} 无效")
    return value


def _require_provider_ref(value: str) -> str:
    lowered = value.casefold()
    if (
        not 1 <= len(value) <= 200
        or value != value.strip()
        or any(character.isspace() for character in value)
        or "://" in value
        or "@" in value
        or any(
            marker in lowered
            for marker in ("bearer", "token", "secret", "password", "authorization")
        )
    ):
        raise ValidationError("provider_ref 无效")
    return value
