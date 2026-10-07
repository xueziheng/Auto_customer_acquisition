"""耐久入站指纹必须覆盖默认序列化隐藏的完整关联事实。"""

import importlib
from datetime import UTC, datetime

import pytest

from shared.schemas.email_inbound import (
    ArchivedInboundItem,
    InboundDisposition,
    InboundRoute,
)
from shared.schemas.identifiers import SendingIdentityId, TenantId, new_id


def test_fingerprint_covers_hidden_headers():
    try:
        module = importlib.import_module("workflows.reply_qualification.inbound")
    except ModuleNotFoundError:
        pytest.fail("耐久整页入站指纹能力缺失")
    route = InboundRoute(
        tenant_id=TenantId(new_id("tn")),
        mailbox_alias="primary",
        configured_identity_id=SendingIdentityId(new_id("sid")),
        route_id="controlled",
        config_version="v1",
    )
    item = ArchivedInboundItem(
        provider_ref_digest="a" * 64,
        disposition=InboundDisposition.CANDIDATE,
        external_message_id="<one@controlled.test>",
        in_reply_to="<one@outbound.test>",
        sent_at=datetime(2026, 9, 5, tzinfo=UTC),
    )
    changed = item.model_copy(update={"in_reply_to": "<two@outbound.test>"})
    assert item.model_dump() == changed.model_dump()
    assert module.item_fingerprint(route, item) != module.item_fingerprint(
        route, changed
    )


@pytest.mark.parametrize(
    "role,active", [("sales", True), ("boss", False), ("manager", True)]
)
def test_review_access_requires_current_active_boss(role, active):
    from domains.conversations import service
    from shared.errors import PermissionDenied
    from shared.schemas.identifiers import EmployeeId
    from shared.schemas.quote_facts import QuoteEmployeeFact

    assert hasattr(service, "require_inbound_review_access"), "待核对窄公开授权缺失"
    tenant = TenantId(new_id("tn"))
    fact = QuoteEmployeeFact(
        tenant_id=tenant,
        employee_id=EmployeeId(new_id("emp")),
        role=role,
        is_active=active,
        manager_id=None,
        team_id=None,
    )
    with pytest.raises(PermissionDenied):
        service.require_inbound_review_access(tenant, fact, action="read")


@pytest.mark.asyncio
async def test_identity_binding_has_distinct_boss_action():
    from domains.sending_identity.permissions import SendingIdentityAction

    assert hasattr(SendingIdentityAction, "INBOUND_BIND"), "人工入站绑定授权缺失"


@pytest.mark.asyncio
async def test_scheduler_requires_lock_for_inbound_phase():
    from apps.scheduler_worker.main import SchedulerConfig, SchedulerRuntime, _run_cycle

    class MustNotRun:
        async def drain(self):
            pytest.fail("缺锁时不能进入outbox")

        async def poll_due(self, tenant, limit):
            pytest.fail("缺锁时不能进入workflow")

        async def scan_once(self):
            pytest.fail("缺锁时不能抓取入站")

    ports = MustNotRun()
    runtime = SchedulerRuntime(
        None,
        ports,
        ports,
        TenantId(new_id("tn")),
        SchedulerConfig(1, 20, 7440159),
        inbound_driver=ports,
    )
    with pytest.raises(RuntimeError, match="缺少 scheduler 锁确认"):
        await _run_cycle(runtime, 1)


@pytest.mark.parametrize("stage", ["commit", "audit_flush"])
async def test_new_cancellation_survives_close_failure(stage):
    import asyncio
    from types import SimpleNamespace

    from infra.db.email_feedback_uow import _TransactionAwareAudit
    from infra.db.email_inbound_uow import SqlAlchemyInboundPageUnitOfWork

    cancellation = asyncio.CancelledError("controlled cancellation")

    class Session:
        closed = False

        async def commit(self):
            if stage == "commit":
                raise cancellation

        async def close(self):
            self.closed = True
            raise RuntimeError("controlled close failure")

    class Sink:
        def log(self, **kwargs):
            raise cancellation

    session = Session()
    audit = _TransactionAwareAudit(Sink())
    if stage == "audit_flush":
        audit.records.append(
            SimpleNamespace(
                actor="system:test",
                action="read",
                tenant_id=TenantId(new_id("tn")),
                scope="system",
                rule="allow:test",
            )
        )
    uow = object.__new__(SqlAlchemyInboundPageUnitOfWork)
    uow._session, uow._audit, uow._sink = session, audit, Sink()
    with pytest.raises(asyncio.CancelledError) as raised:
        await uow.__aexit__(None, None, None)
    assert raised.value is cancellation
    assert session.closed and audit.records == []
