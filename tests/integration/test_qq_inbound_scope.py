"""独占测试库中的合成发送事实：QQ授权须同时匹配当前归属与全部关联。"""
import hashlib

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from apps.api.runtime import build_phase1_dependencies
from connectors.qq_mail.inbound import QQInboundReader, initial_cursor
from domains.sending_identity.permissions import Actor, ScopeLevel, SendingIdentityScope
from domains.sending_identity.schemas import DomainRole, IdentityRegisterRequest
from infra.db.qq_inbound import PostgresProjectReplyScope
from infra.db.session import create_engine_from
from infra.db.tables import (
    ContactLegalBasisRow,
    ContactPointRow,
    EmployeeRow,
    OutreachEnrollmentRow,
    OutreachMessageAttemptRow,
    OwnershipLockRow,
    PrivateReplyTestRow,
    ProspectAccountRow,
    ProspectContactRow,
)
from shared.schemas.email_inbound import InboundError, InboundRoute
from shared.schemas.identifiers import new_id
from tests.integration.test_manual_email_send_api import (
    _NOW,
    _Secrets,
    _seed_campaign,
    _seed_employee,
    _settings,
)
from tests.unit.test_qq_inbound import FROM, TO, Session, mail


async def seed(factory, tenant):
    owner, campaign, approval, account, contact, point, enrollment, attempt, message = [
        new_id(prefix) for prefix in ("emp", "cmp", "apr", "acc", "pc", "cp", "enr", "mat", "out")
    ]
    await _seed_employee(factory, tenant, owner)
    deps = build_phase1_dependencies(_settings(tenant), factory, now=lambda: _NOW, secret_resolver=_Secrets())
    identity = await deps.sending_identities.register(tenant,
        IdentityRegisterRequest(address=TO, domain="qq.com", role=DomainRole.COLD_OUTREACH),
        actor=Actor(owner, SendingIdentityScope(level=ScopeLevel.TENANT), "boss"))
    await _seed_campaign(factory, tenant, campaign, identity, owner, approval)
    header = "<pilot." + hashlib.sha256(attempt.encode()).hexdigest() + "@messages.tradeos.invalid>"
    async with factory.begin() as session:
        employee = await session.scalar(select(EmployeeRow).where(EmployeeRow.tenant_id == tenant, EmployeeRow.employee_id == owner))
        employee.role = "sales"
        user = employee.user_id
        session.add(ProspectAccountRow(tenant_id=tenant, account_id=account, name="isolated fixture",
            country="CN", source_signal_refs=[], field_provenance={}, created_at=_NOW))
        await session.flush()
        session.add(ProspectContactRow(tenant_id=tenant, contact_id=contact, account_id=account,
            full_name="private fixture", created_at=_NOW))
        await session.flush()
        session.add(ContactPointRow(tenant_id=tenant, contact_point_id=point, contact_id=contact,
            kind="email", value=FROM, value_hash=hashlib.sha256(FROM.encode()).hexdigest(),
            verification_status="unverified", created_at=_NOW))
        await session.flush()
        session.add(ContactLegalBasisRow(tenant_id=tenant, contact_point_id=point,
            basis="consent", subject_type="natural_person", contact_type="personal_business",
            source="private_reply_test_confirmation", collected_at=_NOW))
        session.add(PrivateReplyTestRow(tenant_id=tenant, account_id=account,
            contact_point_id=point, sending_identity_id=identity, owner_id=owner,
            created_by=owner, created_at=_NOW))
        session.add(OwnershipLockRow(tenant_id=tenant, lock_id=new_id("own"), account_id=account,
            owner=owner, locked_at=_NOW, locked_by_rule="fixture"))
        session.add(OutreachEnrollmentRow(tenant_id=tenant, enrollment_id=enrollment,
            campaign_id=campaign, campaign_version=1, account_id=account, contact_point_id=point,
            sending_identity_id=identity, state="completed", execution_mode="manual", current_step=1,
            next_send_at=None, enrolled_at=_NOW, stopped_at=_NOW, stop_reason=None, idempotency_key=enrollment))
        await session.flush()
        session.add(OutreachMessageAttemptRow(tenant_id=tenant, attempt_id=attempt, message_id=message,
            campaign_id=campaign, campaign_version=1, enrollment_id=enrollment, step_number=1,
            sending_identity_id=identity, idempotency_key=attempt, state="sent", provider_ref="fixture-receipt",
            created_at=_NOW, updated_at=_NOW, send_claimed_at=_NOW, deterministic_message_id=header,
            idempotency_header=header[1:].split("@")[0]))
    route = InboundRoute(tenant_id=tenant, configured_identity_id=identity, mailbox_alias="qq-project-reply",
        route_id="pilot", config_version="qq-private-test-v1")
    return route, owner, user, header, attempt, account, point


@pytest.mark.parametrize("change", ["none", "unsent", "unknown", "owner", "inactive", "user", "contact", "tenant", "identity", "unrelated", "no_target", "missing_target", "consent", "no_receipt"])
async def test_persisted_scope_filters_before_body(db_url, change):
    engine = create_engine_from(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = new_id("tn")
    try:
        route, owner, user, header, attempt, account, point = await seed(factory, tenant)
        class Binding:
            async def matches(self, t, e, u, address):
                return (t, e, u, address) == (tenant, owner, user, TO)
        async with factory.begin() as session:
            if change == "consent":
                await session.execute(update(ContactLegalBasisRow).where(
                    ContactLegalBasisRow.tenant_id == tenant, ContactLegalBasisRow.contact_point_id == point
                ).values(source="consent_revoked"))
            if change == "no_receipt":
                await session.execute(update(OutreachMessageAttemptRow).where(
                    OutreachMessageAttemptRow.tenant_id == tenant, OutreachMessageAttemptRow.attempt_id == attempt
                ).values(provider_ref=""))
            if change in {"unsent", "unknown"}:
                await session.execute(update(OutreachMessageAttemptRow).where(
                    OutreachMessageAttemptRow.tenant_id == tenant, OutreachMessageAttemptRow.attempt_id == attempt
                ).values(state="reserved" if change == "unsent" else "sending",
                    provider_ref=None, send_claimed_at=None if change == "unsent" else _NOW))
            if change == "owner":
                new_owner = new_id("emp")
                session.add(EmployeeRow(employee_id=new_owner, tenant_id=tenant, name="fixture owner", role="sales", is_active=True))
                await session.flush()
                await session.execute(update(OwnershipLockRow).where(
                    OwnershipLockRow.tenant_id == tenant, OwnershipLockRow.account_id == account
                ).values(owner=new_owner))
            if change in {"inactive", "user"}:
                await session.execute(update(EmployeeRow).where(EmployeeRow.tenant_id == tenant,
                    EmployeeRow.employee_id == owner).values(**(
                    {"is_active": False} if change == "inactive" else {"user_id": new_id("usr")})))
            if change == "contact":
                await session.execute(update(ContactPointRow).where(ContactPointRow.tenant_id == tenant,
                    ContactPointRow.contact_point_id == point).values(value="555555555@qq.com"))
        if change in {"tenant", "identity"}:
            route = route.model_copy(update={
                "tenant_id" if change == "tenant" else "configured_identity_id":
                    new_id("tn" if change == "tenant" else "sid")})
        if change == "unrelated":
            header = "<pilot." + "b" * 64 + "@messages.tradeos.invalid>"
        session = Session({1: mail(reply=header)})
        reader = QQInboundReader(route, owner, session,
            PostgresProjectReplyScope(factory, route, owner, Binding(),
                attempt_id=None if change == "no_target" else new_id("mat") if change == "missing_target" else attempt), address=TO)
        if change not in {"none", "unrelated"}:
            with pytest.raises(InboundError):
                await reader.fetch_inbound_page(route.mailbox_alias, initial_cursor(route, attempt_id=attempt), 10)
            assert session.opens == 0 and not session.read_headers
        else:
            page = await reader.fetch_inbound_page(route.mailbox_alias, initial_cursor(route, attempt_id=attempt), 10)
            assert len(page.items) == (1 if change == "none" else 0)
        assert session.downloads == ([1] if change == "none" else [])
    finally:
        await engine.dispose()


async def test_other_successful_attempt_same_pair_is_not_this_target(db_url):
    from connectors.qq_mail.inbound import reply_headers
    engine = create_engine_from(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = new_id("tn")
    try:
        route, owner, user, header, attempt, account, point = await seed(factory, tenant)
        campaign, approval, enrollment, other_attempt, message = [
            new_id(p) for p in ("cmp", "apr", "enr", "mat", "out")]
        await _seed_campaign(factory, tenant, campaign, route.configured_identity_id, owner, approval)
        other_header = "<pilot." + hashlib.sha256(other_attempt.encode()).hexdigest() + "@messages.tradeos.invalid>"
        async with factory.begin() as db:
            db.add(OutreachEnrollmentRow(tenant_id=tenant, enrollment_id=enrollment,
                campaign_id=campaign, campaign_version=1, account_id=account, contact_point_id=point,
                sending_identity_id=route.configured_identity_id, state="completed", execution_mode="manual",
                current_step=1, next_send_at=None, enrolled_at=_NOW, stopped_at=_NOW,
                stop_reason=None, idempotency_key=enrollment))
            await db.flush()
            db.add(OutreachMessageAttemptRow(tenant_id=tenant, attempt_id=other_attempt, message_id=message,
                campaign_id=campaign, campaign_version=1, enrollment_id=enrollment, step_number=1,
                sending_identity_id=route.configured_identity_id, idempotency_key=other_attempt, state="sent",
                provider_ref="other-fixture-receipt", created_at=_NOW, updated_at=_NOW, send_claimed_at=_NOW,
                deterministic_message_id=other_header, idempotency_header=other_header[1:].split("@")[0]))
        class Binding:
            async def matches(self, t, e, u, address):
                return (t, e, u, address) == (tenant, owner, user, TO)
        scope = PostgresProjectReplyScope(factory, route, owner, Binding(), attempt_id=attempt)
        assert (await scope.target(route, owner)).outbound_message_header == header
        assert await scope.resolve(route, owner, reply_headers(mail(reply=other_header))) is None
        mailbox = Session({1: mail(reply=other_header), 2: mail(reply=header)})
        mailbox.search_result = (1, 2)  # 模拟服务端包含匹配误报，仍须精确拒绝其他成功Attempt。
        reader = QQInboundReader(route, owner, mailbox, scope, address=TO)
        page = await reader.fetch_inbound_page(route.mailbox_alias,
            initial_cursor(route, attempt_id=attempt), 20)
        assert len(page.items) == 1 and mailbox.downloads == [2]
    finally:
        await engine.dispose()
