"""真实Demand公开verifier委托的独立与一次性绑定门禁。"""

import importlib

import pytest

from shared.errors import ValidationError


async def test_verifier_unbound_rejected_and_binding_is_once_per_runtime():
    try:
        cls = importlib.import_module(
            "apps.scheduler_worker.reply_binding"
        ).DeferredCustomerReplyEvidenceVerifier
    except ModuleNotFoundError:
        pytest.fail("canonical Demand缺少一次性证据绑定")
    first, second = cls(), cls()
    with pytest.raises(ValidationError):
        await first.verify("tenant", None)

    class Verifier:
        async def verify(self, tenant, claim):
            raise ValidationError("真实下游拒绝")

    original = Verifier()
    first.bind(original)
    with pytest.raises(ValidationError, match="真实下游拒绝"):
        await first.verify("tenant", None)
    with pytest.raises(ValidationError):
        first.bind(original)
    with pytest.raises(ValidationError):
        await second.verify("tenant", None)


def test_reply_read_action_has_independent_current_boss_authorization():
    from domains.conversations import service
    from shared.errors import PermissionDenied
    from shared.schemas.identifiers import new_id
    from shared.schemas.quote_facts import QuoteEmployeeFact

    check = getattr(service, "require_reply_internal_access", None)
    assert callable(check), "下一问缺少独立领域action权限"
    tenant = new_id("tn")
    base = {
        "tenant_id": tenant,
        "employee_id": new_id("emp"),
        "role": "boss",
        "is_active": True,
        "manager_id": None,
        "team_id": None,
    }
    check(tenant, QuoteEmployeeFact.model_validate(base), action="next_questions")
    for changes in (
        {"role": "sales"},
        {"is_active": False},
        {"tenant_id": new_id("tn")},
    ):
        with pytest.raises(PermissionDenied):
            check(
                tenant,
                QuoteEmployeeFact.model_validate(base | changes),
                action="next_questions",
            )


def test_notification_opportunity_read_requires_exact_system_scope_and_does_not_grant_get():
    from domains.opportunities.permissions import (
        Actor,
        OpportunityAction,
        OpportunityScope,
        Phase1OpportunityAuthorizer,
        ScopeLevel,
    )
    from shared.errors import PermissionDenied
    from shared.schemas.identifiers import new_id

    tenant, opportunity = new_id("tn"), new_id("opp")
    authorizer = Phase1OpportunityAuthorizer(tenant)
    action = OpportunityAction.NOTIFICATION_AUDIENCE_READ
    actor = Actor(
        "system:notification",
        OpportunityScope(
            level=ScopeLevel.SYSTEM, notification_opportunity_id=opportunity
        ),
        "system",
    )
    authorizer.require(actor, action, actor.scope, tenant)
    for other, requested, target in (
        (
            Actor(
                "system:notification",
                OpportunityScope(level=ScopeLevel.SYSTEM),
                "system",
            ),
            action,
            tenant,
        ),
        (
            Actor(
                "boss",
                OpportunityScope(
                    level=ScopeLevel.TENANT, notification_opportunity_id=opportunity
                ),
                "boss",
            ),
            action,
            tenant,
        ),
        (actor, action, new_id("tn")),
        (actor, OpportunityAction.OPPORTUNITY_READ, tenant),
    ):
        with pytest.raises(PermissionDenied):
            authorizer.require(other, requested, other.scope, target)


async def test_notification_owner_read_is_narrow_and_keeps_ownership_read_denied():
    from domains.employees.permissions import (
        Actor,
        EmployeeScope,
        Phase1EmployeeAuthorizer,
    )
    from shared.errors import PermissionDenied
    from shared.schemas.identifiers import ProspectAccountId, TenantId, new_id
    from tests.unit.test_employees_service import TENANT, _make_service

    account = ProspectAccountId(new_id("acc"))
    service, *_ = _make_service(authorizer=Phase1EmployeeAuthorizer(TENANT))
    exact = Actor(
        "system:notification",
        EmployeeScope.SYSTEM,
        "system",
        notification_account_id=account,
    )
    assert await service.get_notification_owner(TENANT, account, actor=exact) is None
    for tenant, target, actor in (
        (TENANT, account, Actor("system:notification", EmployeeScope.SYSTEM, "system")),
        (
            TENANT,
            account,
            Actor(
                "boss", EmployeeScope.TENANT, "boss", notification_account_id=account
            ),
        ),
        (TENANT, ProspectAccountId(new_id("acc")), exact),
        (TenantId(new_id("tn")), account, exact),
    ):
        with pytest.raises(PermissionDenied):
            await service.get_notification_owner(tenant, target, actor=actor)
    with pytest.raises(PermissionDenied):
        await service.get_ownership(TENANT, account, actor=exact)


async def test_notification_audience_never_falls_back_to_inactive_event_owner():
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from apps.scheduler_worker.bootstrap import CurrentNotificationAudience
    from domains.opportunities.schemas import NotificationAudienceTarget
    from shared.events.catalog import HandoffRequested
    from shared.schemas.identifiers import (
        EmployeeId,
        HandoffId,
        OpportunityId,
        ProspectAccountId,
        TenantId,
        new_id,
    )

    tenant, account = TenantId(new_id("tn")), ProspectAccountId(new_id("acc"))
    boss, inactive, current = (EmployeeId(new_id("emp")) for _ in range(3))

    class Opportunities:
        async def get_notification_audience_target(
            self, tenant_id, opportunity_id, *, actor
        ):
            assert actor.scope.notification_opportunity_id == opportunity_id
            return NotificationAudienceTarget(account)

    class Employees:
        async def list_active(self, tenant_id, *, actor):
            return [
                SimpleNamespace(
                    employee_id=boss, tenant_id=tenant, role="boss", is_active=True
                )
            ]

        async def get_notification_owner(self, tenant_id, account_id, *, actor):
            assert actor.notification_account_id == account_id == account
            return current

    @asynccontextmanager
    async def scope(tenant_id):
        yield Employees()

    from datetime import UTC, datetime

    event = HandoffRequested(
        tenant_id=tenant,
        occurred_at=datetime.now(UTC),
        handoff_id=HandoffId(new_id("hand")),
        opportunity_id=OpportunityId(new_id("opp")),
        assigned_to=inactive,
    )
    audience = CurrentNotificationAudience(tenant, scope, Opportunities())
    assert [
        member.employee_id for member in await audience.recipients_for(tenant, event)
    ] == [boss]
