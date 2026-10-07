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


_ALL_ACTION_NAMES = {
    "IDENTITY_REGISTER",
    "INBOUND_BIND",
    "AUTH_CHECK_BEGIN",
    "AUTH_RESULT_RECORD",
    "WARMUP_START",
    "WARMUP_ADVANCE",
    "IDENTITY_READ",
    "IDENTITY_LIST",
    "REPUTATION_READ",
    "SEND_PERMISSION_READ",
    "SEND_SLOT_RESERVE",
    "DELIVERY_EVENT_RECORD",
    "REPUTATION_EVALUATE",
    "THROTTLE_RESUME",
    "SUSPENSION_RESUME",
    "IDENTITY_RETIRE",
}
_EXPECTED_ALLOW_NAMES = {
    ("boss", "TENANT"): {
        "IDENTITY_REGISTER",
        "INBOUND_BIND",
        "AUTH_CHECK_BEGIN",
        "WARMUP_START",
        "IDENTITY_READ",
        "IDENTITY_LIST",
        "REPUTATION_READ",
        "SEND_PERMISSION_READ",
        "SUSPENSION_RESUME",
        "IDENTITY_RETIRE",
    },
    ("manager", "MANAGER"): {
        "IDENTITY_READ",
        "IDENTITY_LIST",
        "REPUTATION_READ",
        "SEND_PERMISSION_READ",
    },
    ("system", "SYSTEM"): {
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
    ("sales", "SELF"): set(),
}


@pytest.mark.parametrize("role,scope_name", _EXPECTED_ALLOW_NAMES)
def test_phase1_matrix_allows_only_the_explicit_actions(
    role: str, scope_name: str
) -> None:
    """遗漏或多放行一个 action 都会造成身份管理越权。"""
    permissions = _permissions()
    assert {action.name for action in permissions.SendingIdentityAction} == _ALL_ACTION_NAMES
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
        if action.name in _EXPECTED_ALLOW_NAMES[(role, scope_name)]:
            assert authorizer.preauthorize(actor, action, scope, TenantId("tenant-1")) == (
                f"phase1:preauthorize:{role}:{level.value}:{action.value}"
            )
            assert authorizer.require(actor, action, scope, TenantId("tenant-1"), identity_id=identity_id) == (
                f"phase1:{role}:{level.value}:{action.value}"
            )
        else:
            with pytest.raises(PermissionDenied, match="Phase 1 发件身份授权拒绝"):
                authorizer.preauthorize(actor, action, scope, TenantId("tenant-1"))
            with pytest.raises(PermissionDenied, match="Phase 1 发件身份授权拒绝"):
                authorizer.require(actor, action, scope, TenantId("tenant-1"), identity_id=identity_id)


def test_preauthorize_checks_eligibility_without_granting_manager_resource_access() -> None:
    """把 domain 缺失当 full allow 会使 domain-only manager 绕过资源 ABAC。"""
    permissions = _permissions()
    scope = _scope(
        permissions.ScopeLevel.MANAGER,
        allowed_domains=frozenset({"example.com"}),
    )
    actor = _actor("manager", scope)
    authorizer = permissions.Phase1SendingIdentityAuthorizer(TenantId("tenant-1"))
    action = permissions.SendingIdentityAction.IDENTITY_READ
    assert authorizer.preauthorize(
        actor, action, scope, TenantId("tenant-1")
    ) == "phase1:preauthorize:manager:manager:identity:read"
    with pytest.raises(PermissionDenied, match="Phase 1 发件身份授权拒绝"):
        authorizer.require(
            actor,
            action,
            scope,
            TenantId("tenant-1"),
            identity_id=SendingIdentityId("sid-1"),
        )
    assert authorizer.require(
        actor,
        action,
        scope,
        TenantId("tenant-1"),
        identity_id=SendingIdentityId("sid-1"),
        domain="example.com",
    ) == "phase1:manager:manager:identity:read"


def test_default_deny_authorizer_rejects_both_authorization_phases() -> None:
    """默认拒绝实现缺少任一阶段都会在 future caller 中形成隐式放行。"""
    permissions = _permissions()
    scope = _scope(permissions.ScopeLevel.TENANT)
    actor = _actor("boss", scope)
    authorizer = permissions.DefaultDenyAuthorizer()
    for method_name, kwargs in (
        ("preauthorize", {}),
        (
            "require",
            {"identity_id": SendingIdentityId("sid-1"), "domain": "example.com"},
        ),
    ):
        with pytest.raises(PermissionDenied, match="Phase 1 发件身份授权拒绝"):
            getattr(authorizer, method_name)(
                actor,
                permissions.SendingIdentityAction.IDENTITY_READ,
                scope,
                TenantId("tenant-1"),
                **kwargs,
            )


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
    with pytest.raises(ValidationError):
        permissions.Actor(actor_id="", role="boss", scope=tenant_scope)
    with pytest.raises(ValidationError):
        _scope(permissions.ScopeLevel.MANAGER, allowed_identity_ids=[SendingIdentityId("sid-1")])


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


def test_scope_defensively_copies_mutable_identity_set_before_actor_creation() -> None:
    """构造后修改原 set 不能把 SYSTEM 写权限从一个身份换到另一个。"""
    permissions = _permissions()
    source_ids = {SendingIdentityId("sid-1")}
    scope = _scope(permissions.ScopeLevel.SYSTEM, allowed_identity_ids=source_ids)
    actor = _actor("system", scope)
    source_ids.clear()
    source_ids.add(SendingIdentityId("sid-2"))
    authorizer = permissions.Phase1SendingIdentityAuthorizer(TenantId("tenant-1"))
    with pytest.raises(PermissionDenied, match="Phase 1 发件身份授权拒绝"):
        authorizer.require(
            actor,
            permissions.SendingIdentityAction.SEND_SLOT_RESERVE,
            scope,
            TenantId("tenant-1"),
            identity_id=SendingIdentityId("sid-2"),
        )


@pytest.mark.parametrize("field", ["allowed_identity_ids", "allowed_domains"])
def test_empty_manager_scope_dimension_denies_even_identity_list(field: str) -> None:
    """空集合表示全拒，不能因 list 没有 resource target 而退化为全读。"""
    permissions = _permissions()
    scope = _scope(permissions.ScopeLevel.MANAGER, **{field: frozenset()})
    actor = _actor("manager", scope)
    with pytest.raises(PermissionDenied, match="Phase 1 发件身份授权拒绝"):
        permissions.Phase1SendingIdentityAuthorizer(TenantId("tenant-1")).require(
            actor,
            permissions.SendingIdentityAction.IDENTITY_LIST,
            scope,
            TenantId("tenant-1"),
        )


def test_manager_resource_scope_checks_identity_and_domain_targets() -> None:
    """manager 的 identity/domain ABAC 必须在资源操作时共同匹配。"""
    permissions = _permissions()
    scope = _scope(
        permissions.ScopeLevel.MANAGER,
        allowed_identity_ids=frozenset({SendingIdentityId("sid-1")}),
        allowed_domains=frozenset({"example.com"}),
    )
    actor = _actor("manager", scope)
    authorizer = permissions.Phase1SendingIdentityAuthorizer(TenantId("tenant-1"))
    assert authorizer.require(
        actor,
        permissions.SendingIdentityAction.IDENTITY_READ,
        scope,
        TenantId("tenant-1"),
        identity_id=SendingIdentityId("sid-1"),
        domain="example.com",
    ) == "phase1:manager:manager:identity:read"
    for identity_id, domain in ((SendingIdentityId("sid-2"), "example.com"), (SendingIdentityId("sid-1"), "other.example")):
        with pytest.raises(PermissionDenied, match="Phase 1 发件身份授权拒绝"):
            authorizer.require(
                actor,
                permissions.SendingIdentityAction.IDENTITY_READ,
                scope,
                TenantId("tenant-1"),
                identity_id=identity_id,
                domain=domain,
            )


@pytest.mark.parametrize(
    "identity_id,domain,allowed",
    [
        (None, None, True),
        (SendingIdentityId("sid-1"), "example.com", True),
        (SendingIdentityId("sid-2"), "example.com", False),
        (SendingIdentityId("sid-1"), "other.example", False),
    ],
)
def test_manager_list_allows_only_targetless_empty_or_fully_matching_row(
    identity_id: SendingIdentityId | None,
    domain: str | None,
    allowed: bool,
) -> None:
    """按 action 跳过 list scope 会信任错误 repo 返回的越权 row。"""
    permissions = _permissions()
    scope = _scope(
        permissions.ScopeLevel.MANAGER,
        allowed_identity_ids=frozenset({SendingIdentityId("sid-1")}),
        allowed_domains=frozenset({"example.com"}),
    )
    actor = _actor("manager", scope)
    authorizer = permissions.Phase1SendingIdentityAuthorizer(TenantId("tenant-1"))
    action = permissions.SendingIdentityAction.IDENTITY_LIST
    assert authorizer.preauthorize(
        actor, action, scope, TenantId("tenant-1")
    ) == "phase1:preauthorize:manager:manager:identity:list"
    if allowed:
        assert authorizer.require(
            actor,
            action,
            scope,
            TenantId("tenant-1"),
            identity_id=identity_id,
            domain=domain,
        ) == "phase1:manager:manager:identity:list"
    else:
        with pytest.raises(PermissionDenied, match="Phase 1 发件身份授权拒绝"):
            authorizer.require(
                actor,
                action,
                scope,
                TenantId("tenant-1"),
                identity_id=identity_id,
                domain=domain,
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
            _actor("unknown", scope),
            permissions.SendingIdentityAction.IDENTITY_READ,
            scope,
            TenantId("tenant-1"),
            identity_id=SendingIdentityId("sid-1"),
        )
    different_scope = _scope(
        permissions.ScopeLevel.TENANT,
        allowed_identity_ids=frozenset({SendingIdentityId("sid-2")}),
    )
    with pytest.raises(PermissionDenied, match="Phase 1 发件身份授权拒绝"):
        authorizer.require(
            actor,
            permissions.SendingIdentityAction.IDENTITY_READ,
            different_scope,
            TenantId("tenant-1"),
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
