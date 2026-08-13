"""工具 manifest 与注册表。

新增工具 = 一份 manifest + 一个 handler，**不改管线**（插件点 3）。
读 manifest 就能知道一个工具受哪些约束，不用读实现。
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from shared.errors import ValidationError
from shared.schemas.identifiers import TenantId

from .pipeline import PreparedToolCall, ToolCallContext

_TOOL_ID_RE = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+")
_LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,99}")
_SECRET_MARKERS = frozenset(
    {"token", "secret", "password", "authorization", "bearer", "cookie", "dsn"}
)


class RiskLevel(str, Enum):
    LOW = "low"
    """读公开网页、搜索、读本域数据。记账即可。"""

    MEDIUM = "medium"
    """写业务数据、创建草稿、抓供应商页面。记审计，受配额限制。"""

    HIGH = "high"
    """发邮件、客户可见内容、修改发件身份。必须审批或在已批准
    Campaign 边界内。"""


class CostClass(str, Enum):
    """成本类别。Phase 1 只记录（tool_call 的成本字段），
    Phase 3 据此做积分预留——现在不记，将来无法回溯定价。"""

    FREE = "free"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class IdempotencyRequirement(str, Enum):
    REQUIRED = "required"
    """有外部副作用的工具必须 REQUIRED。发送类工具没有例外。"""

    OPTIONAL = "optional"
    NONE = "none"


@dataclass(frozen=True)
class ToolManifest:
    """工具 manifest。

    字段：
        tool_id:        如 "email.send"、"web.search"
        version
        description
        risk_level, cost_class
        requires_approval:  高风险动作是否逐次审批（Campaign 边界内
                            的发送为 False，含承诺内容时由 approval
                            stage 依内容判定）
        idempotency
        required_permissions: RBAC 权限点
        checks:         本工具要过哪些 stage。**显式列表**——
                        读 manifest 即知约束，不用读实现
        input_schema / output_schema: JSON Schema（dict 形式）
        redact_fields:  审计中要脱敏的入参字段
    """

    tool_id: str
    version: str
    description: str
    risk_level: RiskLevel
    cost_class: CostClass
    requires_approval: bool
    idempotency: IdempotencyRequirement
    required_permissions: tuple[str, ...]
    checks: tuple[str, ...]
    input_schema: Mapping[str, object] = field(default_factory=dict)
    output_schema: Mapping[str, object] = field(default_factory=dict)
    redact_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.tool_id, str) or _TOOL_ID_RE.fullmatch(self.tool_id) is None:
            raise ValidationError("tool_id 无效")
        _require_label(self.version, "tool version")
        _require_text(self.description, "tool description", max_length=500)
        if not isinstance(self.risk_level, RiskLevel):
            raise ValidationError("tool risk level 无效")
        if not isinstance(self.cost_class, CostClass):
            raise ValidationError("tool cost class 无效")
        if not isinstance(self.requires_approval, bool):
            raise ValidationError("tool approval flag 无效")
        if not isinstance(self.idempotency, IdempotencyRequirement):
            raise ValidationError("tool idempotency requirement 无效")
        permissions = _freeze_labels(
            self.required_permissions,
            "required permissions",
            required=True,
        )
        checks = _freeze_labels(self.checks, "checks", required=True)
        redact_fields = _freeze_labels(
            self.redact_fields,
            "redact fields",
            required=False,
        )
        from .pipeline import STAGE_ORDER

        if any(check not in STAGE_ORDER for check in checks):
            raise ValidationError("tool checks 包含未知阶段")
        if tuple(stage for stage in STAGE_ORDER if stage in checks) != checks:
            raise ValidationError("tool checks 顺序无效")
        if self.risk_level is RiskLevel.HIGH:
            mandatory = {
                "tenant",
                "permission",
                "suppression",
                "approval",
                "idempotency",
                "rate_limit",
            }
            if not mandatory.issubset(checks):
                raise ValidationError("高风险工具缺少强制检查")
            if self.idempotency is not IdempotencyRequirement.REQUIRED:
                raise ValidationError("高风险工具必须强制幂等")
        if (
            self.idempotency is IdempotencyRequirement.REQUIRED
            and "idempotency" not in checks
        ):
            raise ValidationError("强制幂等工具缺少 idempotency 检查")
        object.__setattr__(self, "required_permissions", permissions)
        object.__setattr__(self, "checks", checks)
        object.__setattr__(self, "redact_fields", redact_fields)
        object.__setattr__(self, "input_schema", _freeze_json(self.input_schema))
        object.__setattr__(self, "output_schema", _freeze_json(self.output_schema))


@runtime_checkable
class ToolHandler(Protocol):
    """工具执行器。管线全部通过后才被调用。

    实现约定：
    - **凭证在 handler 内部**，运行期从密钥服务取，不进参数、
      不进返回值、不进日志（硬边界 1）
    - 外部调用经 ``connectors/``，handler 只做参数组装与结果转换
    - 抛 ``TransientError`` 表示可重试，其他错误不重试
    """

    async def prepare(
        self, ctx: ToolCallContext, preflight: object | None
    ) -> PreparedToolCall: ...

    async def execute(
        self, tenant_id: TenantId, prepared: PreparedToolCall
    ) -> Mapping[str, str | int | bool | None]: ...


class ToolRegistry:
    """工具注册表。应用启动时装配，运行期只读。"""

    def __init__(self) -> None:
        self._items: dict[str, tuple[ToolManifest, ToolHandler]] = {}

    def register(self, manifest: ToolManifest, handler: ToolHandler) -> None:
        """注册工具。

        实现要求：
        - ``tool_id`` 重复注册直接抛错（覆盖注册会静默换掉工具行为）
        - 校验 manifest 完整性：HIGH 风险的工具 ``checks`` 必须包含
          approval 与 suppression；有副作用的必须 REQUIRED 幂等。
          **注册时就拦住配置错误**，不要等运行时。
        """
        if not isinstance(manifest, ToolManifest):
            raise ValidationError("tool manifest 无效")
        if not isinstance(handler, ToolHandler):
            raise ValidationError("tool handler 无效")
        if not inspect.iscoroutinefunction(handler.prepare) or not inspect.iscoroutinefunction(
            handler.execute
        ):
            raise ValidationError("tool handler 必须使用 async 两阶段接口")
        if manifest.tool_id in self._items:
            raise ValidationError("tool_id 已注册")
        self._items[manifest.tool_id] = (manifest, handler)

    def get(self, tool_id: str) -> tuple[ToolManifest, ToolHandler]:
        """按 ID 取。未注册抛错——未知工具默认拒绝，不默认放行。"""
        try:
            return self._items[tool_id]
        except (KeyError, TypeError):
            raise ValidationError("tool_id 未注册") from None

    def list_manifests(self) -> list[ToolManifest]:
        return [self._items[tool_id][0] for tool_id in sorted(self._items)]


def _require_text(value: object, field_name: str, *, max_length: int) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= max_length
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValidationError(f"{field_name} 无效")
    return value


def _require_label(value: object, field_name: str) -> str:
    label = _require_text(value, field_name, max_length=100)
    if _LABEL_RE.fullmatch(label) is None:
        raise ValidationError(f"{field_name} 无效")
    tokens = frozenset(re.split(r"[._:-]", label.casefold()))
    if tokens & _SECRET_MARKERS:
        raise ValidationError(f"{field_name} 无效")
    return label


def _freeze_labels(
    values: object,
    field_name: str,
    *,
    required: bool,
) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        raise ValidationError(f"{field_name} 必须是有序集合")
    result = tuple(_require_label(value, field_name) for value in values)
    if required and not result:
        raise ValidationError(f"{field_name} 不能为空")
    if len(result) != len(set(result)):
        raise ValidationError(f"{field_name} 不能重复")
    return result


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key or key != key.strip():
                raise ValidationError("tool schema key 无效")
            frozen[key] = _freeze_json(item)
        return MappingProxyType(frozen)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise ValidationError("tool schema value 无效")
