"""Phase 1 runtime 的 tenant-bound、默认拒绝授权矩阵。"""

from __future__ import annotations

import logging

import pytest

from domains.employees import permissions as employee_permissions
from domains.employees.permissions import Actor as EmployeeActor
from domains.employees.permissions import EmployeeAction, EmployeeScope
from domains.opportunities import permissions as opportunity_permissions
from domains.opportunities.permissions import Actor as OpportunityActor
from domains.opportunities.permissions import (
    OpportunityAction,
    OpportunityScope,
    ScopeLevel,
)
from shared.errors import PermissionDenied
from shared.schemas.identifiers import EmployeeId, TenantId


class _MissingProductionSymbol:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pytest.fail("RED：Phase 1 concrete authorizer/audit logger 尚未实现")


Phase1EmployeeAuthorizer = getattr(
    employee_permissions, "Phase1EmployeeAuthorizer", _MissingProductionSymbol
)
EmployeeStandardAuditLogger = getattr(
    employee_permissions, "StandardAuditLogger", _MissingProductionSymbol
)
Phase1OpportunityAuthorizer = getattr(
    opportunity_permissions, "Phase1OpportunityAuthorizer", _MissingProductionSymbol
)


def _opportunity_scope(
    level: ScopeLevel, employee_id: EmployeeId
) -> OpportunityScope:
    if level is ScopeLevel.SELF:
        return OpportunityScope(
            level=level,
            allowed_owners=frozenset({employee_id}),
        )
    if level is ScopeLevel.MANAGER:
        return OpportunityScope(
            level=level,
            allowed_owners=frozenset({employee_id}),
        )
    return OpportunityScope(level=level)


def _opportunity_actor_for_role(role: str) -> OpportunityActor:
    levels = {
        "sales": ScopeLevel.SELF,
        "manager": ScopeLevel.MANAGER,
        "boss": ScopeLevel.TENANT,
        "system": ScopeLevel.SYSTEM,
    }
    employee_id = EmployeeId("emp-sales")
    scope = _opportunity_scope(levels[role], employee_id)
    return OpportunityActor(str(employee_id), scope, role)


@pytest.mark.parametrize(
    ("role", "level", "action"),
    [
        ("sales", ScopeLevel.SELF, OpportunityAction.OPPORTUNITY_READ),
        ("sales", ScopeLevel.SELF, OpportunityAction.OPPORTUNITY_LIST),
        ("sales", ScopeLevel.SELF, OpportunityAction.OPPORTUNITY_TRANSITION),
        ("sales", ScopeLevel.SELF, OpportunityAction.OPPORTUNITY_MARK_LOST),
        ("sales", ScopeLevel.SELF, OpportunityAction.HANDOFF_READ),
        ("sales", ScopeLevel.SELF, OpportunityAction.HANDOFF_QUEUE_READ),
        ("sales", ScopeLevel.SELF, OpportunityAction.HANDOFF_ACCEPT),
        ("manager", ScopeLevel.MANAGER, OpportunityAction.OPPORTUNITY_READ),
        ("manager", ScopeLevel.MANAGER, OpportunityAction.OPPORTUNITY_LIST),
        ("manager", ScopeLevel.MANAGER, OpportunityAction.OPPORTUNITY_TRANSITION),
        ("manager", ScopeLevel.MANAGER, OpportunityAction.OPPORTUNITY_MARK_LOST),
        ("manager", ScopeLevel.MANAGER, OpportunityAction.HANDOFF_READ),
        ("manager", ScopeLevel.MANAGER, OpportunityAction.HANDOFF_QUEUE_READ),
        ("manager", ScopeLevel.MANAGER, OpportunityAction.HANDOFF_ACCEPT),
        ("boss", ScopeLevel.TENANT, OpportunityAction.OPPORTUNITY_READ),
        ("boss", ScopeLevel.TENANT, OpportunityAction.OPPORTUNITY_LIST),
        ("boss", ScopeLevel.TENANT, OpportunityAction.OPPORTUNITY_TRANSITION),
        ("boss", ScopeLevel.TENANT, OpportunityAction.OPPORTUNITY_MARK_LOST),
        ("boss", ScopeLevel.TENANT, OpportunityAction.HANDOFF_READ),
        ("boss", ScopeLevel.TENANT, OpportunityAction.HANDOFF_QUEUE_READ),
        ("boss", ScopeLevel.TENANT, OpportunityAction.HANDOFF_ACCEPT),
        ("boss", ScopeLevel.TENANT, OpportunityAction.OPPORTUNITY_CREATE),
        ("boss", ScopeLevel.TENANT, OpportunityAction.OPPORTUNITY_ASSIGN),
        ("boss", ScopeLevel.TENANT, OpportunityAction.HANDOFF_REQUEST),
        ("boss", ScopeLevel.TENANT, OpportunityAction.LOSS_REASON_READ),
        ("system", ScopeLevel.SYSTEM, OpportunityAction.HANDOFF_REQUEST),
        (
            "system",
            ScopeLevel.SYSTEM,
            OpportunityAction.HANDOFF_ESCALATION_RECORD,
        ),
    ],
)
def test_phase1_opportunity_authorizer_allows_only_explicit_matrix(
    role: str, level: ScopeLevel, action: OpportunityAction
) -> None:
    tenant = TenantId("tenant-runtime")
    scope = _opportunity_scope(level, EmployeeId("emp-sales"))
    actor = OpportunityActor("emp-sales", scope, role)
    rule = Phase1OpportunityAuthorizer(tenant).require(actor, action, scope, tenant)
    assert rule == f"phase1:{role}:{level.value}:{action.value}"


@pytest.mark.parametrize("role", ["sales", "manager", "boss", "system"])
def test_phase1_authorizer_denies_mark_won_for_every_role(role: str) -> None:
    tenant = TenantId("tenant-runtime")
    actor = _opportunity_actor_for_role(role)
    with pytest.raises(PermissionDenied, match="Phase 1 机会授权拒绝"):
        Phase1OpportunityAuthorizer(tenant).require(
            actor,
            OpportunityAction.OPPORTUNITY_MARK_WON,
            actor.scope,
            tenant,
        )


@pytest.mark.parametrize(
    ("actor", "action", "tenant"),
    [
        (
            _opportunity_actor_for_role("sales"),
            OpportunityAction.OPPORTUNITY_CREATE,
            TenantId("tenant-runtime"),
        ),
        (
            _opportunity_actor_for_role("manager"),
            OpportunityAction.OPPORTUNITY_ASSIGN,
            TenantId("tenant-runtime"),
        ),
        (
            _opportunity_actor_for_role("system"),
            OpportunityAction.OPPORTUNITY_READ,
            TenantId("tenant-runtime"),
        ),
        (
            _opportunity_actor_for_role("boss"),
            OpportunityAction.OPPORTUNITY_READ,
            TenantId("tenant-other"),
        ),
    ],
)
def test_phase1_opportunity_authorizer_denies_unlisted_or_cross_tenant(
    actor: OpportunityActor,
    action: OpportunityAction,
    tenant: TenantId,
) -> None:
    with pytest.raises(PermissionDenied, match="Phase 1 机会授权拒绝"):
        Phase1OpportunityAuthorizer(TenantId("tenant-runtime")).require(
            actor,
            action,
            actor.scope,
            tenant,
        )


def test_phase1_opportunity_authorizer_denies_scope_different_from_actor() -> None:
    actor = _opportunity_actor_for_role("boss")
    with pytest.raises(PermissionDenied, match="Phase 1 机会授权拒绝"):
        Phase1OpportunityAuthorizer(TenantId("tenant-runtime")).require(
            actor,
            OpportunityAction.OPPORTUNITY_READ,
            OpportunityScope(level=ScopeLevel.SYSTEM),
            TenantId("tenant-runtime"),
        )


@pytest.mark.parametrize(
    ("role", "scope", "action"),
    [
        ("system", EmployeeScope.SYSTEM, EmployeeAction.EMPLOYEE_READ),
        ("system", EmployeeScope.SYSTEM, EmployeeAction.EMPLOYEE_LIST),
        ("boss", EmployeeScope.TENANT, EmployeeAction.OWNERSHIP_READ),
        ("boss", EmployeeScope.TENANT, EmployeeAction.OWNERSHIP_LOCK),
        ("boss", EmployeeScope.TENANT, EmployeeAction.OWNERSHIP_TRANSFER),
        ("boss", EmployeeScope.TENANT, EmployeeAction.TERRITORY_APPLY),
        ("boss", EmployeeScope.TENANT, EmployeeAction.EMPLOYEE_READ),
        ("boss", EmployeeScope.TENANT, EmployeeAction.EMPLOYEE_LIST),
        ("boss", EmployeeScope.TENANT, EmployeeAction.ASSIGNMENT_LIST),
    ],
)
def test_phase1_employee_authorizer_allows_only_explicit_matrix(
    role: str, scope: EmployeeScope, action: EmployeeAction
) -> None:
    tenant = TenantId("tenant-runtime")
    actor = EmployeeActor("actor-runtime", scope, role)
    rule = Phase1EmployeeAuthorizer(tenant).require(actor, action, scope, tenant)
    assert rule == f"phase1:{role}:{scope.value}:{action.value}"


@pytest.mark.parametrize(
    ("role", "scope", "action", "tenant"),
    [
        (
            "sales",
            EmployeeScope.SELF,
            EmployeeAction.EMPLOYEE_READ,
            TenantId("tenant-runtime"),
        ),
        (
            "manager",
            EmployeeScope.MANAGER,
            EmployeeAction.OWNERSHIP_TRANSFER,
            TenantId("tenant-runtime"),
        ),
        (
            "system",
            EmployeeScope.SYSTEM,
            EmployeeAction.OWNERSHIP_LOCK,
            TenantId("tenant-runtime"),
        ),
        (
            "boss",
            EmployeeScope.TENANT,
            EmployeeAction.EMPLOYEE_READ,
            TenantId("tenant-other"),
        ),
    ],
)
def test_phase1_employee_authorizer_denies_unlisted_or_cross_tenant(
    role: str,
    scope: EmployeeScope,
    action: EmployeeAction,
    tenant: TenantId,
) -> None:
    actor = EmployeeActor("actor-runtime", scope, role)
    with pytest.raises(PermissionDenied, match="Phase 1 员工授权拒绝"):
        Phase1EmployeeAuthorizer(TenantId("tenant-runtime")).require(
            actor,
            action,
            scope,
            tenant,
        )


def test_employee_standard_audit_logger_emits_only_safe_authorization_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger_name = "security.authorization.employee.test"
    with caplog.at_level(logging.INFO, logger=logger_name):
        EmployeeStandardAuditLogger(logger_name).log(
            actor="actor-runtime",
            action="employee:read",
            tenant_id=TenantId("tenant-runtime"),
            scope="system",
            rule="phase1:system:system:employee:read",
        )
    record = caplog.records[-1]
    assert record.getMessage() == "authorization"
    assert {
        key: record.__dict__[key]
        for key in ("actor", "action", "tenant_id", "scope", "rule")
    } == {
        "actor": "actor-runtime",
        "action": "employee:read",
        "tenant_id": "tenant-runtime",
        "scope": "system",
        "rule": "phase1:system:system:employee:read",
    }
