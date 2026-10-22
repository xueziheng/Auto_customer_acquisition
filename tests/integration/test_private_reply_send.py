"""原API/Gateway单封私有测试：真实PG，邮箱传输及审批/额度事实为fixture。"""

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from apps.api.composition.private_reply_test import PrivateReplyTestApplication
from apps.api.composition.runtime import (
    ManualSendComposition,
    build_phase1_dependencies,
)
from apps.api.main import create_app
from apps.api.middleware import ApiSettings
from apps.composition_support.delivery_material_reader import (
    CurrentDeliveryMaterialReader,
)
from apps.composition_support.outreach_fact_readers import CurrentReplyStatusReader
from domains.conversations.aliyun_connection import AliyunActor
from domains.outreach.schemas import (
    CampaignApprovalSnapshot,
    CampaignApprovalState,
    OutreachSenderRole,
    SendingIdentityEligibilitySnapshot,
)
from domains.sending_identity.schemas import (
    AuthenticationResult,
    DomainRole,
    IdentityRegisterRequest,
)
from domains.sending_identity.service import Actor, ScopeLevel, SendingIdentityScope
from infra.aliyun_mail_store import AliyunBinding, AliyunMailStore
from infra.db.private_reply_test import (
    PrivateReplyContactEligibility,
    PrivateReplyTestStore,
)
from infra.db.session import create_engine_from
from infra.db.tables import (
    ContactPointRow,
    EmployeeRow,
    NeedHypothesisRow,
    OpportunityRow,
    OutreachEnrollmentRow,
    ValidatedNeedRow,
)
from infra.standalone.settings import QQReplySettings
from shared.schemas.identifiers import new_id
from tests.integration.solar_reply_fixture import seed_solar_campaign
from tests.integration.test_manual_email_send_api import (
    _NOW,
    _Secrets,
    _seed_employee,
    _settings,
    _Transport,
)
from tests.outreach_fakes import FakeApprovals, FakeContacts, FakeSenders, Trace


@pytest.mark.parametrize("category", ["clear_interest", "willing_to_continue", "no_current_need", "unknown_send", "revoked_consent", "revoked_owner",
    "search_unsupported", "search_network", "search_excessive", "search_validity"])
async def test_private_prepare_and_original_manual_send_remain_idempotent(db_url, tmp_path, category):
    engine = create_engine_from(db_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant, owner, boss, campaign, approval = [new_id(p) for p in ("tn","emp","emp","cmp","apr")]
    transport, trace = _Transport(), Trace()
    try:
        await _seed_employee(sessions, tenant, owner)
        await _seed_employee(sessions, tenant, boss)
        async with sessions.begin() as db:
            employee = await db.scalar(select(EmployeeRow).where(EmployeeRow.tenant_id == tenant, EmployeeRow.employee_id == owner))
            employee.role = "sales"
            user = employee.user_id
        base = build_phase1_dependencies(_settings(tenant), sessions, now=lambda: _NOW, secret_resolver=_Secrets())
        sender_actor = Actor(boss, SendingIdentityScope(level=ScopeLevel.TENANT), "boss")
        sender = await base.sending_identities.register(tenant,
            IdentityRegisterRequest("1249583590@qq.com", "qq.com", DomainRole.COLD_OUTREACH), actor=sender_actor)
        await base.sending_identities.begin_authentication(tenant, sender, actor=sender_actor)
        await base.sending_identities.record_authentication_result(tenant, sender,
            AuthenticationResult(_NOW, True, True, True, (), "fixture-auth"),
            actor=Actor("system:fixture", SendingIdentityScope(level=ScopeLevel.SYSTEM, allowed_identity_ids=frozenset({sender})), "system"))
        await base.sending_identities.start_warmup(tenant, sender, 5, actor=sender_actor)
        await seed_solar_campaign(sessions, tenant, campaign, sender, boss, approval)
        approvals = FakeApprovals(trace)
        approvals.values[(campaign, 1)] = CampaignApprovalSnapshot(
            tenant, campaign, 1, approval, CampaignApprovalState.APPROVED, boss, _NOW)
        contacts = PrivateReplyContactEligibility(sessions, FakeContacts({}, trace), now=lambda: _NOW)
        senders = FakeSenders({sender: SendingIdentityEligibilitySnapshot(
            tenant, sender, OutreachSenderRole.COLD_OUTREACH, True, True, 5, _NOW)}, trace)
        deps = build_phase1_dependencies(_settings(tenant), sessions, now=lambda: _NOW,
            manual_send=ManualSendComposition(contacts, senders, approvals,
                CurrentReplyStatusReader(tenant, base.prospecting, base.conversations, now=lambda: _NOW),
                CurrentDeliveryMaterialReader(tenant, base.prospecting, base.sending_identities),
                _Secrets(), transport))
        app = create_app(settings=ApiSettings(tenant_id=tenant, dev_mode=True, retry_after_seconds=30), dependencies=deps)
        root = tmp_path / "qq-private";root.mkdir(mode=0o700)
        actor = AliyunActor(tenant_id=tenant, employee_id=owner, user_id=user)
        AliyunMailStore(root).publish(AliyunBinding(actor=actor, email="1249583590@qq.com",
            password=SecretStr("fixture-auth-only"), connected_at=_NOW, operation_id=new_id("aco")))
        cfg = QQReplySettings(employee_id=owner, user_id=user, classifier_employee_id=boss, mailbox_directory=root)
        store = PrivateReplyTestStore(sessions, now=lambda: _NOW)
        app.state.private_reply_test = PrivateReplyTestApplication(cfg, tenant, store, deps.outreach)
        headers = {"X-Tenant-Id": tenant, "X-Employee-Id": owner}
        async with AsyncClient(transport=ASGITransport(app), base_url="http://fixture.test") as client:
            options = await client.get("/inbox/private-reply-test/options", headers=headers)
            assert options.status_code == 200 and len(options.json()["options"]) == 1, options.text
            command = {"campaign_id":campaign, "sending_identity_id":sender, "confirm_controlled_recipient":True}
            first = await client.post("/inbox/private-reply-test/prepare", headers=headers, json=command)
            assert first.status_code == 200, first.text
            repeated = await client.post("/inbox/private-reply-test/prepare", headers=headers, json=command)
            assert repeated.json() == first.json()
            assert transport.send_calls == 0
            enrollment = first.json()["enrollment_id"]
            prepared = await client.post(f"/crm/enrollments/{enrollment}/attempts/prepare", headers=headers)
            assert prepared.status_code == 200, prepared.text
            attempt = prepared.json()["attempt_id"]
            body = {"subject":"Private reply test", "body":"This is a private email test. Please reply if you can read it."}
            if category in {"revoked_consent", "revoked_owner"}:
                from sqlalchemy import update

                from infra.db.tables import ContactLegalBasisRow, OwnershipLockRow
                async with sessions.begin() as db:
                    if category == "revoked_consent":
                        await db.execute(update(ContactLegalBasisRow).where(
                            ContactLegalBasisRow.tenant_id == tenant,
                            ContactLegalBasisRow.contact_point_id == first.json()["contact_point_id"]
                        ).values(source="consent_revoked"))
                    else:
                        await db.execute(update(OwnershipLockRow).where(
                            OwnershipLockRow.tenant_id == tenant, OwnershipLockRow.account_id == first.json()["account_id"]
                        ).values(owner=boss))
                refused = await client.post(f"/crm/message-attempts/{attempt}/send", headers=headers, json=body)
                assert refused.status_code != 200
                assert transport.send_calls == 0
                return
            if category == "unknown_send":
                from connectors.gmail.transport import GmailNetworkError
                async def uncertain(**kwargs):
                    transport.send_calls += 1
                    raise GmailNetworkError(may_have_written=True)
                transport.send = uncertain
                for _ in range(2):
                    uncertain_result = await client.post(f"/crm/message-attempts/{attempt}/send", headers=headers, json=body)
                    assert uncertain_result.status_code != 200
                assert transport.send_calls == 1
                return
            sent = await client.post(f"/crm/message-attempts/{attempt}/send", headers=headers, json=body)
            assert sent.status_code == 200, sent.text
            duplicate = await client.post(f"/crm/message-attempts/{attempt}/send", headers=headers, json=body)
            assert duplicate.status_code == 200, duplicate.text
            assert transport.send_calls == 1
            again = await client.post(f"/crm/enrollments/{enrollment}/attempts/prepare", headers=headers)
            assert again.status_code != 200
            other = await client.post("/inbox/private-reply-test/prepare",
                headers={**headers,"X-Employee-Id":boss}, json=command)
            assert other.status_code == 403
            from tests.integration.qq_reply_chain import exercise_reply
            await exercise_reply(sessions=sessions, tenant=tenant, owner=owner, boss=boss,
                user=user, sender=sender, attempt=attempt, cfg=cfg, deps=deps, client=client,
                headers=headers, category=category, now=lambda: _NOW)
            assert transport.send_calls == 1
        async with sessions() as db:
            point = await db.scalar(select(ContactPointRow).where(ContactPointRow.tenant_id == tenant,
                ContactPointRow.contact_point_id == first.json()["contact_point_id"]))
            assert point.verification_status == "unverified" and point.verified_at is None
            record = await db.scalar(select(OutreachEnrollmentRow).where(
                OutreachEnrollmentRow.tenant_id == tenant, OutreachEnrollmentRow.enrollment_id == enrollment))
            assert record.execution_mode == "manual" and record.source_hypothesis_id is None
            assert record.next_send_at is None
            for row in (NeedHypothesisRow, ValidatedNeedRow, OpportunityRow):
                assert await db.scalar(select(func.count()).select_from(row).where(row.tenant_id == tenant)) == 0
    finally:
        await engine.dispose()
