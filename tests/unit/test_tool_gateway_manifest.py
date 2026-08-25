"""Tool manifest 与 registry fail-closed 行为。"""

from __future__ import annotations

import importlib
from typing import Any

import pytest

from shared.errors import ValidationError
from shared.schemas.identifiers import TenantId
from tool_gateway.manifest import (
    CostClass,
    IdempotencyRequirement,
    RiskLevel,
    ToolHandler,
    ToolManifest,
    ToolRegistry,
)
from tool_gateway.pipeline import (
    STAGE_ORDER,
    PreparedToolCall,
    ToolCallContext,
)


def test_email_feedback_fetch_manifest_is_low_risk_read_only_plugin() -> None:
    module = __import__("tool_gateway.handlers.email_feedback", fromlist=["MANIFEST"])
    manifest = module.MANIFEST
    assert manifest.tool_id == "email.feedback.fetch"
    assert manifest.risk_level is RiskLevel.LOW
    assert manifest.cost_class is CostClass.FREE
    assert manifest.idempotency is IdempotencyRequirement.NONE
    assert manifest.checks == ("tenant", "permission")
    assert manifest.required_permissions == ("email:feedback_read",)
    assert manifest.requires_approval is False


EMAIL_CHECKS = (
    "tenant",
    "permission",
    "suppression",
    "approval",
    "idempotency",
    "rate_limit",
)
INTERNAL_TRANSACTIONAL_CHECKS = (
    "tenant",
    "permission",
    "idempotency",
    "rate_limit",
)


def _internal_transactional_profile() -> object:
    manifest_module = importlib.import_module("tool_gateway.manifest")
    profile_type = getattr(manifest_module, "HighRiskStageProfile", None)
    if profile_type is None:
        pytest.fail("RED：HIGH internal-transactional stage profile 尚未实现")
    return profile_type.INTERNAL_TRANSACTIONAL


class _Handler:
    async def prepare(
        self, ctx: ToolCallContext, preflight: object | None
    ) -> PreparedToolCall:
        del ctx, preflight
        return PreparedToolCall("a" * 64, "fp-v1", {}, object())

    async def execute(
        self, tenant_id: TenantId, prepared: PreparedToolCall
    ) -> dict[str, str | int | bool | None]:
        del tenant_id, prepared
        return {"provider_ref": "gmail_abc123", "already_existed": False}


def _manifest(**changes: Any) -> ToolManifest:
    values: dict[str, Any] = {
        "tool_id": "email.send",
        "version": "v1",
        "description": "发送一封人工触发邮件",
        "risk_level": RiskLevel.HIGH,
        "cost_class": CostClass.LOW,
        "requires_approval": False,
        "idempotency": IdempotencyRequirement.REQUIRED,
        "required_permissions": ("outreach:message_send",),
        "checks": EMAIL_CHECKS,
        "input_schema": {"type": "object", "required": ["attempt_id"]},
        "output_schema": {"type": "object"},
        "redact_fields": ("subject", "body"),
    }
    values.update(changes)
    return ToolManifest(**values)


def test_email_send_manifest_registers_with_exact_ordered_checks() -> None:
    """漏段或错序会让高风险发送绕过拒绝或先消耗额度。"""
    registry = ToolRegistry()
    handler = _Handler()
    registry.register(_manifest(), handler)
    manifest, registered = registry.get("email.send")
    assert manifest.checks == EMAIL_CHECKS
    assert registered is handler
    assert isinstance(handler, ToolHandler)
    assert tuple(stage for stage in STAGE_ORDER if stage in EMAIL_CHECKS) == EMAIL_CHECKS


def test_high_internal_transactional_profile_accepts_only_exact_four_stages() -> None:
    """员工固定模板事务发送需要 HIGH 审计且不能复用客户 suppression/approval。"""
    profile = _internal_transactional_profile()
    manifest = _manifest(
        tool_id="notification.email.send",
        checks=INTERNAL_TRANSACTIONAL_CHECKS,
        high_risk_stage_profile=profile,
    )
    assert manifest.risk_level is RiskLevel.HIGH
    assert manifest.high_risk_stage_profile is profile
    assert manifest.checks == INTERNAL_TRANSACTIONAL_CHECKS


@pytest.mark.parametrize(
    "checks",
    [
        ("tenant", "permission", "idempotency"),
        (*INTERNAL_TRANSACTIONAL_CHECKS, "approval"),
        ("permission", "tenant", "idempotency", "rate_limit"),
    ],
)
def test_high_internal_transactional_profile_rejects_missing_extra_or_reordered_stages(
    checks: tuple[str, ...],
) -> None:
    """profile 必须精确，不允许未来把它扩成普通邮件旁路。"""
    with pytest.raises(ValidationError):
        _manifest(
            tool_id="notification.email.send",
            checks=checks,
            high_risk_stage_profile=_internal_transactional_profile(),
        )


def test_legacy_high_defaults_to_customer_outbound_and_still_requires_six_stages() -> None:
    """既有 HIGH manifest 不声明 profile 时必须保持客户外发兼容约束。"""
    manifest_module = importlib.import_module("tool_gateway.manifest")
    manifest = _manifest()
    profile_type = getattr(manifest_module, "HighRiskStageProfile", None)
    if profile_type is None:
        pytest.fail("RED：HIGH stage profile 尚未实现")
    assert manifest.high_risk_stage_profile is profile_type.CUSTOMER_OUTBOUND
    with pytest.raises(ValidationError):
        _manifest(checks=INTERNAL_TRANSACTIONAL_CHECKS)


@pytest.mark.parametrize("risk_level", [RiskLevel.LOW, RiskLevel.MEDIUM])
def test_non_high_manifest_rejects_high_risk_stage_profile(
    risk_level: RiskLevel,
) -> None:
    """显式 HIGH profile 不得被非 HIGH 工具借用。"""
    with pytest.raises(ValidationError):
        _manifest(
            risk_level=risk_level,
            checks=INTERNAL_TRANSACTIONAL_CHECKS,
            high_risk_stage_profile=_internal_transactional_profile(),
        )


@pytest.mark.parametrize("missing", ["suppression", "approval", "idempotency"])
def test_high_risk_registration_rejects_missing_mandatory_stage(missing: str) -> None:
    """删除 suppression/approval/idempotency 任一项都必须在启动期失败。"""
    checks = tuple(item for item in EMAIL_CHECKS if item != missing)
    with pytest.raises(ValidationError):
        ToolRegistry().register(_manifest(checks=checks), _Handler())


@pytest.mark.parametrize(
    "changes",
    [
        {"checks": ("permission", "tenant", "suppression", "approval", "idempotency", "rate_limit")},
        {"checks": (*EMAIL_CHECKS, "unknown")},
        {"checks": (*EMAIL_CHECKS, "approval")},
        {"required_permissions": ()},
        {"required_permissions": ("",)},
        {"required_permissions": ("token:read",)},
        {"idempotency": IdempotencyRequirement.OPTIONAL},
        {"idempotency": IdempotencyRequirement.NONE},
        {"tool_id": "Email.Send"},
        {"tool_id": "email"},
        {"version": " secret-v1"},
        {"description": ""},
        {"requires_approval": 1},
        {"risk_level": "high"},
        {"cost_class": "low"},
    ],
)
def test_manifest_rejects_malformed_or_fail_open_configuration(
    changes: dict[str, object]
) -> None:
    """宽松配置会把运行时拼写错误变成静默放行。"""
    with pytest.raises(ValidationError):
        _manifest(**changes)


def test_manifest_defensively_freezes_schema_and_sequence_inputs() -> None:
    """注册后修改 dict/list 不能静默改变工具能力或审计策略。"""
    required = ["attempt_id"]
    schema: dict[str, object] = {"type": "object", "required": required}
    permissions = ["outreach:message_send"]
    checks = list(EMAIL_CHECKS)
    redact = ["subject", "body"]
    manifest = _manifest(
        input_schema=schema,
        required_permissions=permissions,
        checks=checks,
        redact_fields=redact,
    )
    required.append("recipient")
    schema["secret"] = "value"
    permissions.append("admin")
    checks.clear()
    redact.append("token")
    assert manifest.input_schema == {
        "type": "object",
        "required": ("attempt_id",),
    }
    assert manifest.required_permissions == ("outreach:message_send",)
    assert manifest.checks == EMAIL_CHECKS
    assert manifest.redact_fields == ("subject", "body")
    with pytest.raises(TypeError):
        manifest.input_schema["type"] = "array"  # type: ignore[index]


@pytest.mark.parametrize(
    "output_schema",
    [
        {"type": "array"},
        {"type": "object", "required": ("missing",), "properties": {}},
        {
            "type": "object",
            "properties": {"provider_ref": {"type": "number"}},
        },
        {
            "type": "object",
            "properties": {
                "provider_ref": {"type": "string", "enum": None}
            },
        },
        {
            "type": "object",
            "properties": {
                "provider_ref": {"type": "string", "pattern": None}
            },
        },
        {
            "type": "object",
            "properties": {
                "provider_ref": {"type": "string", "maxLength": 200}
            },
        },
    ],
)
def test_manifest_rejects_output_schema_outside_bounded_object_subset(
    output_schema: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _manifest(output_schema=output_schema)


class _ExecuteOnly:
    async def execute(self, tenant_id: TenantId, prepared: PreparedToolCall) -> dict[str, str]:
        del tenant_id, prepared
        return {}


class _SyncPrepare(_Handler):
    def prepare(  # type: ignore[override]
        self, ctx: ToolCallContext, preflight: object | None
    ) -> PreparedToolCall:
        del ctx, preflight
        return PreparedToolCall("a" * 64, "fp-v1", {}, object())


@pytest.mark.parametrize("handler", [_ExecuteOnly(), _SyncPrepare(), object()])
def test_registry_rejects_handler_without_two_async_phases(handler: object) -> None:
    """只检查属性存在会让同步方法或旧 execute-only handler 进入生产。"""
    with pytest.raises(ValidationError):
        ToolRegistry().register(_manifest(), handler)  # type: ignore[arg-type]


def test_registry_rejects_duplicates_unknown_ids_and_returns_sorted_snapshot() -> None:
    """覆盖注册或非确定顺序会让启动装配静默换掉工具行为。"""
    registry = ToolRegistry()
    registry.register(_manifest(tool_id="web.search", risk_level=RiskLevel.LOW, idempotency=IdempotencyRequirement.NONE, checks=("tenant",)), _Handler())
    registry.register(_manifest(), _Handler())
    assert [item.tool_id for item in registry.list_manifests()] == ["email.send", "web.search"]
    with pytest.raises(ValidationError):
        registry.register(_manifest(), _Handler())
    with pytest.raises(ValidationError):
        registry.get("email.unknown")
