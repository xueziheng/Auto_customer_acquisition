"""Sourcing V2 tenant-bound、默认拒绝授权矩阵。"""

from __future__ import annotations

import importlib

import pytest

from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import TenantId


def _permissions() -> object:
    try:
        return importlib.import_module("domains.sourcing.permissions")
    except ModuleNotFoundError:
        pytest.fail("RED：domains.sourcing.permissions 尚未实现")


@pytest.mark.parametrize(
    ("role", "scope_name", "action_name"),
    [
        ("boss", "TENANT", "CASE_READ"),
        ("boss", "TENANT", "CASE_LIST"),
        ("boss", "TENANT", "ADMISSION_READ"),
        ("boss", "TENANT", "ADMISSION_MANUAL_START"),
        ("boss", "TENANT", "PLAN_DRAFT"),
        ("boss", "TENANT", "PLAN_CONFIRM"),
        ("boss", "TENANT", "REVIEW_CONFIRM"),
        ("product", "TENANT", "CASE_READ"),
        ("product", "TENANT", "CASE_LIST"),
        ("product", "TENANT", "ADMISSION_READ"),
        ("product", "TENANT", "CANDIDATE_ENTER"),
        ("product", "TENANT", "REVIEW_SUBMIT"),
        ("sourcing", "TENANT", "CASE_READ"),
        ("sourcing", "TENANT", "CASE_LIST"),
        ("sourcing", "TENANT", "ADMISSION_READ"),
        ("sourcing", "TENANT", "ADMISSION_MANUAL_START"),
        ("sourcing", "TENANT", "PLAN_DRAFT"),
        ("sourcing", "TENANT", "CANDIDATE_ENTER"),
        ("sourcing", "TENANT", "REVIEW_SUBMIT"),
        ("finance", "TENANT", "CASE_READ"),
        ("finance", "TENANT", "CASE_LIST"),
        ("finance", "TENANT", "ADMISSION_READ"),
        ("finance", "TENANT", "COSTING_HANDOFF_READ"),
        ("system", "SYSTEM", "CASE_OPEN"),
        ("system", "SYSTEM", "WORKFLOW_PROGRESS"),
        ("system", "SYSTEM", "FACT_PUBLISH"),
        ("system", "SYSTEM", "ADMISSION_ENQUEUE"),
        ("system", "SYSTEM", "ADMISSION_REFRESH"),
        ("system", "SYSTEM", "ADMISSION_CLAIM"),
        ("system", "SYSTEM", "ADMISSION_COMPLETE"),
        # 交接事件由受信任的系统 worker 消费；它只能读取已冻结的快照。
        ("system", "SYSTEM", "COSTING_HANDOFF_READ"),
    ],
)
def test_phase2_authorizer_allows_only_explicit_matrix(
    role: str, scope_name: str, action_name: str
) -> None:
    module = _permissions()
    tenant = TenantId("tenant-a")
    scope = module.SourcingScope[scope_name]
    action = module.SourcingAction[action_name]
    actor = module.SourcingActor("actor-a", tenant, scope, role)

    rule = module.Phase2SourcingAuthorizer(tenant).require(actor, action, scope, tenant)

    assert rule == f"phase2:{role}:{scope.value}:{action.value}"


@pytest.mark.parametrize(
    ("role", "scope_name", "action_name"),
    [
        ("boss", "TENANT", "CANDIDATE_ENTER"),
        ("product", "TENANT", "PLAN_CONFIRM"),
        ("sourcing", "TENANT", "REVIEW_CONFIRM"),
        ("finance", "TENANT", "REVIEW_SUBMIT"),
        ("system", "SYSTEM", "CASE_READ"),
        ("finance", "TENANT", "ADMISSION_MANUAL_START"),
        ("product", "TENANT", "ADMISSION_MANUAL_START"),
    ],
)
def test_phase2_authorizer_rejects_unlisted_role_actions(
    role: str, scope_name: str, action_name: str
) -> None:
    module = _permissions()
    tenant = TenantId("tenant-a")
    scope = module.SourcingScope[scope_name]
    actor = module.SourcingActor("actor-a", tenant, scope, role)

    with pytest.raises(PermissionDenied, match="Phase 2 寻源授权拒绝"):
        module.Phase2SourcingAuthorizer(tenant).require(
            actor,
            module.SourcingAction[action_name],
            scope,
            tenant,
        )


def test_phase2_authorizer_rejects_cross_tenant_scope_mismatch_and_unknown_action() -> (
    None
):
    module = _permissions()
    tenant = TenantId("tenant-a")
    actor = module.SourcingActor("boss-a", tenant, module.SourcingScope.TENANT, "boss")
    authorizer = module.Phase2SourcingAuthorizer(tenant)

    for action, scope, requested_tenant in (
        (
            module.SourcingAction.CASE_READ,
            module.SourcingScope.TENANT,
            TenantId("tenant-other"),
        ),
        (
            module.SourcingAction.CASE_READ,
            module.SourcingScope.SYSTEM,
            tenant,
        ),
        ("case:read", module.SourcingScope.TENANT, tenant),
    ):
        with pytest.raises(PermissionDenied, match="Phase 2 寻源授权拒绝"):
            authorizer.require(actor, action, scope, requested_tenant)


def test_phase2_authorizer_rejects_non_actor_objects_and_invalid_actor_identity() -> (
    None
):
    module = _permissions()
    tenant = TenantId("tenant-a")
    authorizer = module.Phase2SourcingAuthorizer(tenant)
    with pytest.raises(PermissionDenied, match="Phase 2 寻源授权拒绝"):
        authorizer.require(
            {"actor_id": "boss-a", "role": "boss"},
            module.SourcingAction.PLAN_CONFIRM,
            module.SourcingScope.TENANT,
            tenant,
        )
    with pytest.raises(ValidationError):
        module.SourcingActor("", tenant, module.SourcingScope.TENANT, "boss")


def test_phase2_admission_actions_have_no_implicit_role_or_scope_grants() -> None:
    module = _permissions()
    tenant = TenantId("tenant-a")
    expected = {
        "ADMISSION_ENQUEUE": {("system", "SYSTEM")},
        "ADMISSION_REFRESH": {("system", "SYSTEM")},
        "ADMISSION_CLAIM": {("system", "SYSTEM")},
        "ADMISSION_COMPLETE": {("system", "SYSTEM")},
        "ADMISSION_READ": {
            ("boss", "TENANT"),
            ("product", "TENANT"),
            ("sourcing", "TENANT"),
            ("finance", "TENANT"),
        },
        "ADMISSION_MANUAL_START": {
            ("boss", "TENANT"),
            ("sourcing", "TENANT"),
        },
    }

    for action_name, allowed in expected.items():
        action = module.SourcingAction[action_name]
        for role in ("boss", "product", "sourcing", "finance", "system", "unknown"):
            for scope_name in ("TENANT", "SYSTEM"):
                scope = module.SourcingScope[scope_name]
                if role == "system" and scope is module.SourcingScope.TENANT:
                    continue
                if role != "system" and scope is module.SourcingScope.SYSTEM:
                    continue
                actor = module.SourcingActor("actor-a", tenant, scope, role)
                if (role, scope_name) in allowed:
                    assert (
                        module.Phase2SourcingAuthorizer(tenant).require(
                            actor, action, scope, tenant
                        )
                        == f"phase2:{role}:{scope.value}:{action.value}"
                    )
                else:
                    with pytest.raises(PermissionDenied):
                        module.Phase2SourcingAuthorizer(tenant).require(
                            actor, action, scope, tenant
                        )
