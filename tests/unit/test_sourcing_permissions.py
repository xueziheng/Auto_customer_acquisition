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
        ("boss", "TENANT", "PLAN_DRAFT"),
        ("boss", "TENANT", "PLAN_CONFIRM"),
        ("boss", "TENANT", "REVIEW_CONFIRM"),
        ("product", "TENANT", "CASE_READ"),
        ("product", "TENANT", "CASE_LIST"),
        ("product", "TENANT", "CANDIDATE_ENTER"),
        ("product", "TENANT", "REVIEW_SUBMIT"),
        ("sourcing", "TENANT", "CASE_READ"),
        ("sourcing", "TENANT", "CASE_LIST"),
        ("sourcing", "TENANT", "PLAN_DRAFT"),
        ("sourcing", "TENANT", "CANDIDATE_ENTER"),
        ("sourcing", "TENANT", "REVIEW_SUBMIT"),
        ("finance", "TENANT", "CASE_READ"),
        ("finance", "TENANT", "CASE_LIST"),
        ("finance", "TENANT", "COSTING_HANDOFF_READ"),
        ("system", "SYSTEM", "CASE_OPEN"),
        ("system", "SYSTEM", "WORKFLOW_PROGRESS"),
        ("system", "SYSTEM", "FACT_PUBLISH"),
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

    rule = module.Phase2SourcingAuthorizer(tenant).require(
        actor, action, scope, tenant
    )

    assert rule == f"phase2:{role}:{scope.value}:{action.value}"


@pytest.mark.parametrize(
    ("role", "scope_name", "action_name"),
    [
        ("boss", "TENANT", "CANDIDATE_ENTER"),
        ("product", "TENANT", "PLAN_CONFIRM"),
        ("sourcing", "TENANT", "REVIEW_CONFIRM"),
        ("finance", "TENANT", "REVIEW_SUBMIT"),
        ("system", "SYSTEM", "CASE_READ"),
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


def test_phase2_authorizer_rejects_cross_tenant_scope_mismatch_and_unknown_action() -> None:
    module = _permissions()
    tenant = TenantId("tenant-a")
    actor = module.SourcingActor(
        "boss-a", tenant, module.SourcingScope.TENANT, "boss"
    )
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


def test_phase2_authorizer_rejects_non_actor_objects_and_invalid_actor_identity() -> None:
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
