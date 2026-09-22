"""触达域 Phase 1 默认拒绝与逐资源 ABAC 契约。"""

from __future__ import annotations

import importlib
import logging

import pytest

from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import (
    CampaignId,
    EnrollmentId,
    MessageAttemptId,
    ProspectAccountId,
    SendingIdentityId,
    TenantId,
    new_id,
)


def _permissions() -> object:
    try:
        return importlib.import_module("domains.outreach.permissions")
    except ModuleNotFoundError as exc:
        pytest.fail(f"缺少触达权限契约: {exc.name}")


TENANT = TenantId(new_id("tn"))
OTHER_TENANT = TenantId(new_id("tn"))
CAMPAIGN = CampaignId(new_id("cmp"))
OTHER_CAMPAIGN = CampaignId(new_id("cmp"))
ACCOUNT = ProspectAccountId(new_id("acc"))
OTHER_ACCOUNT = ProspectAccountId(new_id("acc"))
ENROLLMENT = EnrollmentId(new_id("enr"))
OTHER_ENROLLMENT = EnrollmentId(new_id("enr"))
ATTEMPT = MessageAttemptId(new_id("mat"))
OTHER_ATTEMPT = MessageAttemptId(new_id("mat"))
IDENTITY = SendingIdentityId(new_id("sid"))
OTHER_IDENTITY = SendingIdentityId(new_id("sid"))
TARGET = str(ACCOUNT)
OTHER_TARGET = str(OTHER_ACCOUNT)


_ALL_ACTIONS = {
    "CAMPAIGN_CREATE",
    "CAMPAIGN_SUBMIT",
    "CAMPAIGN_REVISE",
    "CAMPAIGN_ACTIVATE",
    "CAMPAIGN_PAUSE",
    "CAMPAIGN_CANCEL",
    "CAMPAIGN_READ",
    "CAMPAIGN_LIST",
    "ENROLLMENT_CREATE",
    "ENROLLMENT_PREPARE_SEND",
    "ENROLLMENT_RECORD_SENT",
    "ENROLLMENT_RECORD_FAILURE",
    "ENROLLMENT_STOP",
    "ENROLLMENT_READ",
    "ENROLLMENT_LIST",
    "SUPPRESSION_ADD",
    "SUPPRESSION_READ",
    "SUPPRESSION_LIST",
    "MESSAGE_DELIVERY_BIND",
    "DELIVERY_FEEDBACK_RESOLVE",
    "REPLY_SOURCE_READ",
    "HARD_BOUNCE_APPLY",
    "COMPLAINT_APPLY",
}

_ALLOWED = {
    ("boss", "TENANT"): {
        "REPLY_SOURCE_READ",
        "CAMPAIGN_CREATE",
        "CAMPAIGN_SUBMIT",
        "CAMPAIGN_REVISE",
        "CAMPAIGN_ACTIVATE",
        "CAMPAIGN_PAUSE",
        "CAMPAIGN_CANCEL",
        "CAMPAIGN_READ",
        "CAMPAIGN_LIST",
        "ENROLLMENT_CREATE",
        "ENROLLMENT_STOP",
        "ENROLLMENT_READ",
        "ENROLLMENT_LIST",
        "SUPPRESSION_ADD",
        "SUPPRESSION_READ",
        "SUPPRESSION_LIST",
    },
    ("manager", "MANAGER"): {
        "REPLY_SOURCE_READ",
        "CAMPAIGN_SUBMIT",
        "CAMPAIGN_REVISE",
        "CAMPAIGN_PAUSE",
        "CAMPAIGN_CANCEL",
        "CAMPAIGN_READ",
        "CAMPAIGN_LIST",
        "ENROLLMENT_CREATE",
        "ENROLLMENT_STOP",
        "ENROLLMENT_READ",
        "ENROLLMENT_LIST",
        "SUPPRESSION_READ",
        "SUPPRESSION_LIST",
    },
    ("system", "SYSTEM"): {
        "ENROLLMENT_PREPARE_SEND",
        "ENROLLMENT_RECORD_SENT",
        "ENROLLMENT_RECORD_FAILURE",
        "ENROLLMENT_STOP",
        "ENROLLMENT_READ",
        "SUPPRESSION_ADD",
        "SUPPRESSION_READ",
        "MESSAGE_DELIVERY_BIND",
        "DELIVERY_FEEDBACK_RESOLVE",
        "HARD_BOUNCE_APPLY",
        "COMPLAINT_APPLY",
    },
    ("sales", "SELF"): {
        "REPLY_SOURCE_READ",
        "CAMPAIGN_READ",
        "CAMPAIGN_LIST",
        "ENROLLMENT_READ",
        "ENROLLMENT_LIST",
    },
}


def _scope_for(role: str, action: object) -> object:
    permissions = _permissions()
    if role == "boss":
        return permissions.OutreachScope(level=permissions.ScopeLevel.TENANT)
    if role == "manager":
        return permissions.OutreachScope(
            level=permissions.ScopeLevel.MANAGER,
            allowed_campaign_ids={CAMPAIGN},
            allowed_account_ids={ACCOUNT},
            allowed_enrollment_ids={ENROLLMENT},
            allowed_suppression_targets={TARGET},
        )
    if role in {"sales", "manager"} and action.name == "REPLY_SOURCE_READ":
        return permissions.OutreachScope(
            level=permissions.ScopeLevel.SELF
            if role == "sales"
            else permissions.ScopeLevel.MANAGER,
            allowed_account_ids={ACCOUNT},
        )
    if role == "sales":
        return permissions.OutreachScope(
            level=permissions.ScopeLevel.SELF,
            allowed_campaign_ids={CAMPAIGN},
            allowed_enrollment_ids={ENROLLMENT},
        )
    if action.name.startswith("SUPPRESSION_"):
        return permissions.OutreachScope(
            level=permissions.ScopeLevel.SYSTEM,
            allowed_suppression_targets={TARGET},
        )
    if action.name == "MESSAGE_DELIVERY_BIND":
        return permissions.OutreachScope(
            level=permissions.ScopeLevel.SYSTEM,
            allowed_attempt_ids={ATTEMPT},
        )
    if action.name in {
        "DELIVERY_FEEDBACK_RESOLVE",
        "HARD_BOUNCE_APPLY",
        "COMPLAINT_APPLY",
    }:
        return permissions.OutreachScope(
            level=permissions.ScopeLevel.SYSTEM,
            allowed_sending_identity_ids={IDENTITY},
        )
    return permissions.OutreachScope(
        level=permissions.ScopeLevel.SYSTEM,
        allowed_enrollment_ids={ENROLLMENT},
    )


def _actor(role: str, scope: object) -> object:
    permissions = _permissions()
    return permissions.Actor(actor_id=f"{role}-1", role=role, scope=scope)


@pytest.mark.parametrize("role,level_name", _ALLOWED)
def test_phase1_matrix_allows_only_explicit_role_actions(
    role: str, level_name: str
) -> None:
    """老板不能伪造 worker 结果，manager/system/sales 也不能扩权。"""
    permissions = _permissions()
    assert {action.name for action in permissions.OutreachAction} == _ALL_ACTIONS
    authorizer = permissions.Phase1OutreachAuthorizer(TENANT)
    for action in permissions.OutreachAction:
        scope = _scope_for(role, action)
        actor = _actor(role, scope)
        if action.name in _ALLOWED[(role, level_name)]:
            assert authorizer.preauthorize(actor, action, scope, TENANT).startswith(
                "phase1:preauthorize:"
            )
        else:
            with pytest.raises(PermissionDenied, match="Phase 1 触达授权拒绝"):
                authorizer.preauthorize(actor, action, scope, TENANT)


def test_manager_resource_authorization_applies_every_configured_dimension() -> None:
    """只核 Campaign 而忽略 account/enrollment 会泄露范围外行。"""
    permissions = _permissions()
    action = permissions.OutreachAction.ENROLLMENT_READ
    scope = _scope_for("manager", action)
    actor = _actor("manager", scope)
    authorizer = permissions.Phase1OutreachAuthorizer(TENANT)
    assert authorizer.require(
        actor,
        action,
        scope,
        TENANT,
        campaign_id=CAMPAIGN,
        account_id=ACCOUNT,
        enrollment_id=ENROLLMENT,
    ).startswith("phase1:manager:")
    mutations = (
        {
            "campaign_id": OTHER_CAMPAIGN,
            "account_id": ACCOUNT,
            "enrollment_id": ENROLLMENT,
        },
        {
            "campaign_id": CAMPAIGN,
            "account_id": OTHER_ACCOUNT,
            "enrollment_id": ENROLLMENT,
        },
        {
            "campaign_id": CAMPAIGN,
            "account_id": ACCOUNT,
            "enrollment_id": OTHER_ENROLLMENT,
        },
    )
    for resources in mutations:
        with pytest.raises(PermissionDenied):
            authorizer.require(actor, action, scope, TENANT, **resources)


def test_sales_resource_authorization_checks_owned_campaign_and_enrollment() -> None:
    """SELF 只读不能把 targetless list 变成跨 ownership 全读。"""
    permissions = _permissions()
    scope = _scope_for("sales", permissions.OutreachAction.ENROLLMENT_READ)
    actor = _actor("sales", scope)
    authorizer = permissions.Phase1OutreachAuthorizer(TENANT)
    with pytest.raises(PermissionDenied):
        authorizer.require(
            actor,
            permissions.OutreachAction.ENROLLMENT_READ,
            scope,
            TENANT,
            campaign_id=OTHER_CAMPAIGN,
            enrollment_id=ENROLLMENT,
        )


@pytest.mark.parametrize("reason", ["unsubscribe", "complaint", "hard_bounce"])
def test_system_may_add_only_automatic_suppression_reasons(reason: str) -> None:
    """SYSTEM 只能把已确认的自动合规事实写入精确 target。"""
    permissions = _permissions()
    models = importlib.import_module("domains.outreach.models")
    action = permissions.OutreachAction.SUPPRESSION_ADD
    scope = _scope_for("system", action)
    actor = _actor("system", scope)
    authorizer = permissions.Phase1OutreachAuthorizer(TENANT)
    assert authorizer.require(
        actor,
        action,
        scope,
        TENANT,
        suppression_target=TARGET,
        suppression_reason=models.SuppressionReason(reason),
    ).startswith("phase1:system:")


@pytest.mark.parametrize(
    "reason", ["manual_block", "competitor", "existing_customer_conflict"]
)
def test_system_cannot_add_human_suppression_reasons(reason: str) -> None:
    """人工原因不能被 worker 冒充。"""
    permissions = _permissions()
    models = importlib.import_module("domains.outreach.models")
    action = permissions.OutreachAction.SUPPRESSION_ADD
    scope = _scope_for("system", action)
    actor = _actor("system", scope)
    with pytest.raises(PermissionDenied):
        permissions.Phase1OutreachAuthorizer(TENANT).require(
            actor,
            action,
            scope,
            TENANT,
            suppression_target=TARGET,
            suppression_reason=models.SuppressionReason(reason),
        )


def test_wrong_tenant_scope_instance_and_unknown_actor_fail_closed() -> None:
    """tenant、scope 或 actor 任一不可信都不能进入资源加载。"""
    permissions = _permissions()
    action = permissions.OutreachAction.CAMPAIGN_READ
    scope = permissions.OutreachScope(
        level=permissions.ScopeLevel.TENANT,
    )
    actor = _actor("boss", scope)
    authorizer = permissions.Phase1OutreachAuthorizer(TENANT)
    with pytest.raises(PermissionDenied):
        authorizer.preauthorize(actor, action, scope, OTHER_TENANT)
    with pytest.raises(PermissionDenied):
        authorizer.preauthorize(
            actor,
            action,
            permissions.OutreachScope(level=permissions.ScopeLevel.TENANT),
            TENANT,
        )
    with pytest.raises(PermissionDenied):
        authorizer.preauthorize(object(), action, scope, TENANT)


def test_scope_defensively_freezes_sets_and_rejects_unrestricted_levels() -> None:
    """外部可变 set 不能在 actor 构造后扩权。"""
    permissions = _permissions()
    campaigns = {CAMPAIGN}
    scope = permissions.OutreachScope(
        level=permissions.ScopeLevel.MANAGER,
        allowed_campaign_ids=campaigns,
    )
    campaigns.add(OTHER_CAMPAIGN)
    assert scope.allowed_campaign_ids == frozenset({CAMPAIGN})
    for level in (permissions.ScopeLevel.MANAGER, permissions.ScopeLevel.SELF):
        with pytest.raises(ValidationError):
            permissions.OutreachScope(level=level)
    with pytest.raises(ValidationError):
        permissions.OutreachScope(level=permissions.ScopeLevel.SYSTEM)


def test_system_scope_requires_one_target_and_cannot_expand_after_actor_creation() -> (
    None
):
    """SYSTEM 多 target 或 mutable target 是直接写权限突破。"""
    permissions = _permissions()
    targets = {ENROLLMENT}
    scope = permissions.OutreachScope(
        level=permissions.ScopeLevel.SYSTEM,
        allowed_enrollment_ids=targets,
    )
    actor = _actor("system", scope)
    targets.add(OTHER_ENROLLMENT)
    assert actor.scope.allowed_enrollment_ids == frozenset({ENROLLMENT})
    with pytest.raises(ValidationError):
        permissions.OutreachScope(
            level=permissions.ScopeLevel.SYSTEM,
            allowed_enrollment_ids={ENROLLMENT, OTHER_ENROLLMENT},
        )
    with pytest.raises(ValidationError):
        permissions.OutreachScope(
            level=permissions.ScopeLevel.SYSTEM,
            allowed_enrollment_ids={ENROLLMENT},
            allowed_suppression_targets={TARGET},
        )


@pytest.mark.parametrize(
    ("action_name", "scope_field", "resource_field", "resource", "other"),
    [
        (
            "MESSAGE_DELIVERY_BIND",
            "allowed_attempt_ids",
            "attempt_id",
            ATTEMPT,
            OTHER_ATTEMPT,
        ),
        (
            "DELIVERY_FEEDBACK_RESOLVE",
            "allowed_sending_identity_ids",
            "sending_identity_id",
            IDENTITY,
            OTHER_IDENTITY,
        ),
        (
            "HARD_BOUNCE_APPLY",
            "allowed_sending_identity_ids",
            "sending_identity_id",
            IDENTITY,
            OTHER_IDENTITY,
        ),
        (
            "COMPLAINT_APPLY",
            "allowed_sending_identity_ids",
            "sending_identity_id",
            IDENTITY,
            OTHER_IDENTITY,
        ),
    ],
)
def test_system_delivery_feedback_actions_require_exact_resource(
    action_name: str,
    scope_field: str,
    resource_field: str,
    resource: str,
    other: str,
) -> None:
    """Correlation worker 不能从一个 Attempt/Identity 横向扩到另一个。"""
    permissions = _permissions()
    action = permissions.OutreachAction[action_name]
    mutable_resources = {resource}
    scope = permissions.OutreachScope(
        level=permissions.ScopeLevel.SYSTEM,
        **{scope_field: mutable_resources},
    )
    actor = _actor("system", scope)
    mutable_resources.add(other)
    authorizer = permissions.Phase1OutreachAuthorizer(TENANT)
    assert authorizer.require(
        actor,
        action,
        scope,
        TENANT,
        **{resource_field: resource},
    ).startswith("phase1:system:")
    with pytest.raises(PermissionDenied):
        authorizer.require(
            actor,
            action,
            scope,
            TENANT,
            **{resource_field: other},
        )


def test_scope_rejects_wrong_namespaces_and_non_set_containers() -> None:
    """裸字符串/错 prefix 不能伪装成 typed ABAC target。"""
    permissions = _permissions()
    with pytest.raises(ValidationError):
        permissions.OutreachScope(
            level=permissions.ScopeLevel.MANAGER,
            allowed_campaign_ids=[CAMPAIGN],
        )
    with pytest.raises(ValidationError):
        permissions.OutreachScope(
            level=permissions.ScopeLevel.MANAGER,
            allowed_campaign_ids={CampaignId(str(ACCOUNT))},
        )


def test_default_deny_rejects_both_authorization_phases() -> None:
    """未装配正式策略时 preauth/full require 都必须拒绝。"""
    permissions = _permissions()
    scope = permissions.OutreachScope(level=permissions.ScopeLevel.TENANT)
    actor = _actor("boss", scope)
    action = permissions.OutreachAction.CAMPAIGN_READ
    authorizer = permissions.DefaultDenyAuthorizer()
    with pytest.raises(PermissionDenied):
        authorizer.preauthorize(actor, action, scope, TENANT)
    with pytest.raises(PermissionDenied):
        authorizer.require(actor, action, scope, TENANT, campaign_id=CAMPAIGN)


def test_standard_audit_logger_uses_fixed_message_and_safe_fields(caplog) -> None:
    """审计不能携带 Campaign 名称、target 或 provider ref。"""
    permissions = _permissions()
    logger = permissions.StandardAuditLogger("test.outreach.audit")
    with caplog.at_level(logging.INFO, logger="test.outreach.audit"):
        logger.log(
            actor="boss-1",
            action="campaign:read",
            tenant_id=TENANT,
            scope="tenant",
            rule="phase1:boss:tenant:campaign:read",
        )
    record = caplog.records[-1]
    assert record.message == "授权审计"
    assert {
        "actor": record.actor,
        "action": record.action,
        "tenant_id": record.tenant_id,
        "scope": record.scope,
        "rule": record.rule,
    } == {
        "actor": "boss-1",
        "action": "campaign:read",
        "tenant_id": str(TENANT),
        "scope": "tenant",
        "rule": "phase1:boss:tenant:campaign:read",
    }


@pytest.mark.parametrize("role,level", [("sales", "SELF"), ("manager", "MANAGER")])
def test_reply_source_scope_requires_one_account_and_cannot_read_other_account(
    role, level
):
    p = _permissions()
    authorizer = p.Phase1OutreachAuthorizer(TENANT)
    for accounts in (None, {ACCOUNT, OTHER_ACCOUNT}):
        scope = p.OutreachScope(
            level=getattr(p.ScopeLevel, level),
            allowed_campaign_ids={CAMPAIGN},
            allowed_account_ids=accounts,
        )
        with pytest.raises(PermissionDenied):
            authorizer.preauthorize(
                _actor(role, scope), p.OutreachAction.REPLY_SOURCE_READ, scope, TENANT
            )
    scope = p.OutreachScope(
        level=getattr(p.ScopeLevel, level), allowed_account_ids={ACCOUNT}
    )
    actor = _actor(role, scope)
    with pytest.raises(PermissionDenied):
        authorizer.require(
            actor,
            p.OutreachAction.REPLY_SOURCE_READ,
            scope,
            TENANT,
            account_id=OTHER_ACCOUNT,
        )
    if role == "sales":
        for action in (
            p.OutreachAction.ENROLLMENT_READ,
            p.OutreachAction.CAMPAIGN_READ,
            p.OutreachAction.DELIVERY_FEEDBACK_RESOLVE,
        ):
            with pytest.raises(PermissionDenied):
                authorizer.preauthorize(actor, action, scope, TENANT)
