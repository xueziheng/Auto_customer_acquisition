"""工具 manifest 与注册表。

新增工具 = 一份 manifest + 一个 handler，**不改管线**（插件点 3）。
读 manifest 就能知道一个工具受哪些约束，不用读实现。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from shared.schemas.identifiers import TenantId


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
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    redact_fields: tuple[str, ...] = ()


@runtime_checkable
class ToolHandler(Protocol):
    """工具执行器。管线全部通过后才被调用。

    实现约定：
    - **凭证在 handler 内部**，运行期从密钥服务取，不进参数、
      不进返回值、不进日志（硬边界 1）
    - 外部调用经 ``connectors/``，handler 只做参数组装与结果转换
    - 抛 ``TransientError`` 表示可重试，其他错误不重试
    """

    async def execute(
        self, tenant_id: TenantId, params: dict[str, Any]
    ) -> dict[str, Any]: ...


class ToolRegistry:
    """工具注册表。应用启动时装配，运行期只读。"""

    def register(self, manifest: ToolManifest, handler: ToolHandler) -> None:
        """注册工具。

        实现要求：
        - ``tool_id`` 重复注册直接抛错（覆盖注册会静默换掉工具行为）
        - 校验 manifest 完整性：HIGH 风险的工具 ``checks`` 必须包含
          approval 与 suppression；有副作用的必须 REQUIRED 幂等。
          **注册时就拦住配置错误**，不要等运行时。
        """
        raise NotImplementedError

    def get(self, tool_id: str) -> tuple[ToolManifest, ToolHandler]:
        """按 ID 取。未注册抛错——未知工具默认拒绝，不默认放行。"""
        raise NotImplementedError

    def list_manifests(self) -> list[ToolManifest]:
        raise NotImplementedError
