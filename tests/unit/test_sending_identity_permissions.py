"""发件身份 Phase 1 默认拒绝授权矩阵。"""

from __future__ import annotations

import importlib
import logging

import pytest

from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import SendingIdentityId, TenantId


def _permissions() -> object:
    """延迟导入使 RED 是缺少权限实现的测试失败，而非 collection 错误。"""
    try:
        return importlib.import_module("domains.sending_identity.permissions")
    except ModuleNotFoundError as exc:
        pytest.fail(f"缺少发件身份权限契约: {exc.name}")


def _actor(role: str, scope: object) -> object:
    permissions = _permissions()
    return permissions.Actor(actor_id=f"{role}-1", role=role, scope=scope)


def _scope(level: object, **kwargs: object) -> object:
    permissions = _permissions()
    return permissions.SendingIdentityScope(level=level, **kwargs)


@pytest.mark.parametrize(
    ("role", "scope_name", "allowed_actions"),
    [
        (
            "boss",
            "TENANT",
            {
                "IDENTITY_REGISTER",
                "AUTH_CHECK_BEGIN",
                "WARMUP_START",
                "IDENTITY_READ",
                "IDENTITY_LIST",
                "REPUTATION_READ",
                "SEND_PERMISSION_READ",
                "SUSPENSION_RESUME",
                "IDENTITY_RETIRE",
            },
        ),
        (
            "manager",
            "MANAGER",
            {"IDENTITY_READ", "IDENTITY_LIST", "REPUTATION_READ", "SEND_PERMISSION_READ"},
        ),
        (
            "system",
            "SYSTEM",
            {
                "AUTH_RESULT_RECORD",
                "WARMUP_ADVANCE",
                "IDENTITY_READ",
                "REPUTATION_READ",
                "SEND_PERMISSION_READ",
                "SEND_SLOT_RESERVE",
                "DELIVERY_EVENT_RECORD",
                "REPUTATION_EVALUATE",
                "THROTTLE_RESUME",
            },
        ),
        ("sales", "SELF", set()),
    ],
)
def test_phase1_matrix_allows_only_the_explicit_actions(
    role: str, scope_name: str, allowed_actions: set[str]
) -> None:
    """遗漏或多放行一个 action 都会造成身份管理越权。"""
    permissions = _permissions()
    level = permissions.ScopeLevel[scope_name]
    identity_id = SendingIdentityId("sid-1")
    kwargs: dict[str, object] = {}
    if level is permissions.ScopeLevel.MANAGER:
        kwargs["allowed_identity_ids"] = frozenset({identity_id})
    if level is permissions.ScopeLevel.SYSTEM:
        kwargs["allowed_identity_ids"] = frozenset({identity_id})
    scope = _scope(level, **kwargs)
    actor = _actor(role, scope)
    authorizer = permissions.Phase1SendingIdentityAuthorizer(TenantId("tenant-1"))
    for action in permissions.SendingIdentityAction:
        if action.name in allowed_actions:
            assert authorizer.require(actor, action, scope, TenantId("tenant-1"), identity_id=identity_id) == (
                f"phase1:{role}:{level.value}:{action.value}"
            )
        else:
            with pytest.raises(PermissionDenied, match="Phase 1 发件身份授权拒绝"):
                authorizer.require(actor, action, scope, TenantId("tenant-1"), identity_id=identity_id)


def test_scope_and_actor_fail_closed_without_silent_normalization() -> None:
    """无限制 manager/system、未规范化域名或危险 actor ID 都不能授权。"""
    permissions = _permissions()
    with pytest.raises(ValidationError):
        _scope(permissions.ScopeLevel.MANAGER)
    with pytest.raises(ValidationError):
        _scope(permissions.ScopeLevel.SYSTEM)
    with pytest.raises(ValidationError):
        _scope(permissions.ScopeLevel.MANAGER, allowed_domains=frozenset({"EXAMPLE.COM"}))
    tenant_scope = _scope(permissions.ScopeLevel.TENANT)
    with pytest.raises(ValidationError):
        permissions.Actor(actor_id="boss\n1", role="boss", scope=tenant_scope)


def test_system_write_requires_exact_singleton_target_scope() -> None:
    """无限制或多个身份的 SYSTEM 写权限会把 worker 变成全局管理员。"""
    permissions = _permissions()
    authorizer = permissions.Phase1SendingIdentityAuthorizer(TenantId("tenant-1"))
    scoped = _scope(
        permissions.ScopeLevel.SYSTEM,
        allowed_identity_ids=frozenset({SendingIdentityId("sid-1"), SendingIdentityId("sid-2")}),
    )
    actor = _actor("system", scoped)
    with pytest.raises(PermissionDenied, match="Phase 1 发件身份授权拒绝"):
        authorizer.require(
            actor,
            permissions.SendingIdentityAction.AUTH_RESULT_RECORD,
            scoped,
            TenantId("tenant-1"),
            identity_id=SendingIdentityId("sid-1"),
        )


def test_authorizer_rejects_wrong_tenant_scope_mismatch_and_unknowns() -> None:
    """租户错配、替换 scope 或未知输入必须默认拒绝。"""
    permissions = _permissions()
    scope = _scope(permissions.ScopeLevel.TENANT)
    actor = _actor("boss", scope)
    authorizer = permissions.Phase1SendingIdentityAuthorizer(TenantId("tenant-1"))
    with pytest.raises(PermissionDenied, match="Phase 1 发件身份授权拒绝"):
        authorizer.require(
            actor,
            permissions.SendingIdentityAction.IDENTITY_READ,
            scope,
            TenantId("tenant-2"),
            identity_id=SendingIdentityId("sid-1"),
        )
    with pytest.raises(PermissionDenied, match="Phase 1 发件身份授权拒绝"):
        authorizer.require(
            actor,
            "identity:read",  # type: ignore[arg-type]
            scope,
            TenantId("tenant-1"),
            identity_id=SendingIdentityId("sid-1"),
        )


def test_standard_audit_logger_emits_only_the_fixed_safe_fields(caplog: pytest.LogCaptureFixture) -> None:
    """审计日志出现域名、引用或任意 payload 会扩大凭证暴露面。"""
    permissions = _permissions()
    logger_name = "security.authorization.sending_identity.test"
    with caplog.at_level(logging.INFO, logger=logger_name):
        permissions.StandardAuditLogger(logger_name).log(
            actor="boss-1",
            action="identity:read",
            tenant_id=TenantId("tenant-1"),
            scope="tenant",
            rule="phase1:boss:tenant:identity:read",
        )
    record = caplog.records[-1]
    assert record.getMessage() == "授权审计"
    assert {"actor", "action", "tenant_id", "scope", "rule"} <= set(record.__dict__)
    assert "domain" not in record.__dict__
    assert "connector_ref" not in record.__dict__
