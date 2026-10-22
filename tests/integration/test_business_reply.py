"""真实Postgres Gateway与0086处理历史；仅SMTP/IMAP及原件IO端口受控。"""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from connectors.qq_mail.business_reply import QQBusinessReplyConnector
from connectors.qq_mail.client import QQMailConnector
from domains.conversations.business_mail import BusinessMailResult
from domains.conversations.business_mail_followup import (
    BusinessMailFollowupService,
    BusinessMailHandlingCommand,
)
from domains.conversations.business_reply import BusinessReplyScope
from infra.db.business_mail_followup import SqlBusinessMailFollowupRepository
from infra.db.tables import ToolCallRow
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from shared.schemas.identifiers import IdempotencyKey, UserId, new_id
from tests.integration.test_business_mail import setup_mail
from tests.unit.test_aliyun_mail_connector import PASSWORD, MailServer
from tests.unit.test_business_reply import ORIGINAL, scope
from tool_gateway.errors import ToolCallStatus
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.business_reply import (
    MANIFEST,
    STAGES,
    BusinessReplyCheck,
    BusinessReplyHandler,
)
from tool_gateway.manifest import ToolRegistry
from tool_gateway.pipeline import ToolCallContext, ToolGateway
from workflows.business_reply.flow import BusinessReplyDriver


class Policy:
    denied = None

    def __init__(self, sessions, s):
        self.sessions, self.scope = sessions, s

    async def check(self, stage, s):
        return self.denied != stage

    async def authorize(self):
        assert await self.check("permission", self.scope)

    async def candidates(self):
        return []

    async def send_record(self):
        async with self.sessions() as db:
            row = await db.scalar(
                select(ToolCallRow).where(
                    ToolCallRow.tenant_id == self.scope.actor.tenant_id,
                    ToolCallRow.tool_id == MANIFEST.tool_id,
                    ToolCallRow.idempotency_key == self.scope.key,
                )
            )
            return (
                None
                if row is None
                else {
                    "status": row.status,
                    "tool_call_id": row.tool_call_id,
                    "provider_ref": row.provider_ref,
                    "error_category": row.error_category,
                }
            )


def setup_gateway(engine, s=None):
    s = s or scope()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    server = MailServer()
    policy = Policy(sessions, s)
    clock = [datetime.now(UTC)]
    credentials = []

    async def factory():
        credentials.append(True)
        return QQBusinessReplyConnector(
            QQMailConnector(s.sender, PASSWORD, transport=server)
        )

    handler = BusinessReplyHandler(s, HmacFingerprintProvider("v1", b"x" * 32), factory)
    registry = ToolRegistry()
    registry.register(MANIFEST, handler)
    gateway = ToolGateway(
        registry,
        {n: BusinessReplyCheck(n, s, policy, now=lambda: clock[0]) for n in STAGES},
        lambda tenant: SqlAlchemyToolGatewayUnitOfWork(
            sessions, tenant, now=lambda: clock[0]
        ),
        lease_duration=timedelta(seconds=120),
        lease_owner="reply_test",
        now=lambda: clock[0],
        id_factory=new_id,
    )
    ctx = ToolCallContext(
        s.actor.tenant_id,
        UserId(s.actor.employee_id),
        MANIFEST.tool_id,
        {"message_id": s.message_id},
        idempotency_key=IdempotencyKey(s.key),
        approval_ref=s.message_id,
    )
    return gateway, ctx, server, policy, clock, credentials, handler


async def test_one_send_survives_gateway_reconstruction_and_duplicate(
    integration_engine,
):
    g, ctx, server, policy, _, creds, _ = setup_gateway(integration_engine)
    result = await g.invoke(ctx)
    assert result.status is ToolCallStatus.SUCCEEDED, (
        result.error_category,
        result.rejected,
    )
    other, *_ = setup_gateway(integration_engine, policy.scope)
    again = await other.invoke(ctx)
    assert again.status is ToolCallStatus.DUPLICATE
    assert len(server.sent) == len(creds) == 1


@pytest.mark.parametrize("stage", STAGES)
async def test_each_current_gate_denies_before_credentials(integration_engine, stage):
    g, ctx, server, policy, _, creds, _ = setup_gateway(integration_engine)
    policy.denied = stage
    result = await g.invoke(ctx)
    assert result.status in {ToolCallStatus.REJECTED, ToolCallStatus.FAILED_PERMANENT}
    assert not server.sent and not creds


@pytest.mark.parametrize("change", ["tenant", "actor", "key", "recipient", "approval"])
async def test_scope_tampering_denied(integration_engine, change):
    g, ctx, server, _, _, creds, _ = setup_gateway(integration_engine)
    options = {
        "tenant": {"tenant_id": new_id("tn")},
        "actor": {"user_id": new_id("emp")},
        "key": {"idempotency_key": IdempotencyKey("changed")},
        "recipient": {"params": dict(ctx.params, recipient="x@y.com")},
        "approval": {"approval_ref": None},
    }
    result = await g.invoke(replace(ctx, **options[change]))
    assert result.status is ToolCallStatus.REJECTED
    assert not creds and not server.sent


async def test_unknown_gateway_result_reconciles_without_second_data(
    integration_engine,
):
    g, ctx, server, _, clock, _, _ = setup_gateway(integration_engine)
    server.smtp_error = TimeoutError("private")
    first = await g.invoke(ctx)
    assert first.error_category.value == "reconciliation_required"
    clock[0] += timedelta(minutes=3)
    server.search_ids = b"91"
    server.headers = server.sent[0].split(b"\r\n\r\n")[0] + b"\r\n\r\n"
    assert (await g.invoke(ctx)).status is ToolCallStatus.SUCCEEDED
    assert len(server.sent) == 1


async def test_concurrent_gateways_dispatch_only_once(integration_engine):
    g, ctx, server, policy, _, _, _ = setup_gateway(integration_engine)
    other, _, other_server, *_ = setup_gateway(integration_engine, policy.scope)
    results = await asyncio.gather(g.invoke(ctx), other.invoke(ctx))
    assert sum(r.status is ToolCallStatus.SUCCEEDED for r in results) == 1
    assert len(server.sent) + len(other_server.sent) == 1


async def test_expired_scope_blocks_before_credentials(integration_engine):
    g, ctx, server, _, clock, creds, _ = setup_gateway(integration_engine)
    clock[0] += timedelta(days=2)
    assert (await g.invoke(ctx)).status is ToolCallStatus.REJECTED
    assert not server.sent and not creds


async def test_real_policy_current_owner_binding_and_erasure(
    integration_engine, tmp_path
):
    from sqlalchemy import update

    from domains.sending_identity.permissions import (
        Actor,
        Phase1SendingIdentityAuthorizer,
        ScopeLevel,
        SendingIdentityScope,
        StandardAuditLogger,
    )
    from domains.sending_identity.schemas import DomainRole, IdentityRegisterRequest
    from domains.sending_identity.service_impl import SendingIdentityServiceImpl
    from infra.db.business_reply import CurrentBusinessReplyPolicy
    from infra.db.sending_identity_uow import SqlAlchemySendingIdentityUnitOfWork
    from infra.db.tables import (
        EmployeeRow,
        ProspectingErasureSuppressionRow,
        SendingIdentityRow,
    )
    from infra.pilot.config import private_write
    from shared.errors import PermissionDenied

    sessions, actor, binding, source, box, page = await setup_mail(integration_engine)
    item = page.messages[0].model_copy(
        update={
            "sender": "sales@jslingte.com",
            "subject": "[TEST-04] YJ-168 purchase inquiry",
            "body_text": ORIGINAL,
            "message_header": "<inquiry@jslingte.com>",
        }
    )
    await source.apply_page(
        actor,
        await source.checkpoint(actor, box),
        page.model_copy(update={"messages": (item,)}),
    )
    claim = await source.claim_next(actor, box, model="fixture", version="fixture")
    await source.finish(
        actor,
        claim,
        result=BusinessMailResult(
            category="business", reason="测试", evidence_quote="YJ-168"
        ),
        failure=None,
    )
    service = SendingIdentityServiceImpl(
        lambda tenant: SqlAlchemySendingIdentityUnitOfWork(
            sessions, tenant, now=lambda: datetime.now(UTC)
        ),
        Phase1SendingIdentityAuthorizer(actor.tenant_id),
        StandardAuditLogger(),
        now=lambda: datetime.now(UTC),
    )
    identity = await service.register(
        actor.tenant_id,
        IdentityRegisterRequest(
            address="1249583590@qq.com", domain="qq.com", role=DomainRole.COLD_OUTREACH
        ),
        actor=Actor("fixture", SendingIdentityScope(level=ScopeLevel.TENANT), "boss"),
    )
    async with sessions.begin() as db:
        await db.execute(
            update(SendingIdentityRow)
            .where(
                SendingIdentityRow.tenant_id == actor.tenant_id,
                SendingIdentityRow.identity_id == identity,
            )
            .values(state="active", version=2)
        )
    s = BusinessReplyScope.model_validate(
        scope().model_dump()
        | {
            "actor": actor,
            "message_id": claim.message_id,
            "source_sha256": item.source_sha256,
            "identity_id": identity,
        }
    )
    tmp_path.chmod(0o700)
    path = tmp_path / "scope.json"
    private_write(path, s.model_dump_json().encode())
    fp = HmacFingerprintProvider("v1", b"x" * 32)
    hasher = lambda value: fp.fingerprint((b"contact-value-v1", value.encode()))[0]
    policy = CurrentBusinessReplyPolicy(source, path, s, hasher)
    assert await policy.check("permission", s)
    binding.operation = "rotated"
    with pytest.raises(PermissionDenied):
        await policy.check("permission", s)
    binding.operation = "aco-current"
    async with sessions.begin() as db:
        db.add(
            ProspectingErasureSuppressionRow(
                tenant_id=actor.tenant_id,
                value_hash=hasher(s.recipient),
                erased_at=datetime.now(UTC),
            )
        )
    assert not await policy.check("suppression", s)
    async with sessions.begin() as db:
        await db.execute(
            update(EmployeeRow)
            .where(
                EmployeeRow.tenant_id == actor.tenant_id,
                EmployeeRow.employee_id == actor.employee_id,
            )
            .values(is_active=False)
        )
    with pytest.raises(PermissionDenied):
        await policy.check("permission", s)


@pytest.mark.parametrize("incomplete", ["none", "new", "legacy"])
async def test_full_driver_restart_reply_source_and_owner_history(
    integration_engine, incomplete
):
    _sessions, actor, _binding, source, box, page = await setup_mail(integration_engine)
    item = page.messages[0].model_copy(
        update={
            "sender": "sales@jslingte.com",
            "subject": "[TEST-04] YJ-168 purchase inquiry",
            "body_text": ORIGINAL,
            "message_header": "<inquiry@jslingte.com>",
        }
    )
    await source.apply_page(
        actor,
        await source.checkpoint(actor, box),
        page.model_copy(update={"messages": (item,)}),
    )
    claim = await source.claim_next(actor, box, model="fixture", version="fixture")
    await source.finish(
        actor,
        claim,
        result=BusinessMailResult(
            category="business", reason="测试询盘", evidence_quote="YJ-168"
        ),
        failure=None,
    )
    s = BusinessReplyScope.model_validate(
        scope().model_dump()
        | {
            "actor": actor,
            "message_id": claim.message_id,
            "source_sha256": item.source_sha256,
        }
    )
    g, _ctx, server, policy, _clock, _creds, handler = setup_gateway(
        integration_engine, s
    )
    followup = BusinessMailFollowupService(SqlBusinessMailFollowupRepository(source))
    replies = []

    async def candidates():
        return replies

    policy.candidates = candidates

    class Evidence:
        async def read(self, message_id):
            if message_id == s.message_id:
                return {
                    "headers": {
                        "Message-ID": s.original_message_id,
                        "Auto-Submitted": "",
                        "List-Id": "",
                        "Precedence": "",
                        "Reply-To": "",
                        "From": s.recipient,
                    }
                }
            return {
                "headers": {
                    "From": s.recipient,
                    "To": s.sender,
                    "Message-ID": "<reply@jslingte.com>",
                    "In-Reply-To": s.original_message_id,
                },
                "artifact_id": "art-reply",
                "source_sha256": "b" * 64,
                "body": "Product: YJ-168"
                if message_id == "bmi-incomplete"
                else "Product: YJ-168\nApplication: classroom test\nQuantity: 12 test units",
            }

    async def heartbeat():
        pass

    def driver():
        return BusinessReplyDriver(
            s, policy, followup, Evidence(), g, handler, heartbeat
        )

    first = await driver().once()
    assert first["status"] == "waiting_customer" and len(server.sent) == 1
    assert (await driver().once())["status"] == "waiting_customer" and len(
        server.sent
    ) == 1
    if incomplete != "none":
        replies.append("bmi-incomplete")
        if incomplete == "legacy":
            state = await followup.get(actor, s.message_id)
            await followup.handle(
                actor,
                s.message_id,
                BusinessMailHandlingCommand(
                    expected_revision=state.revision,
                    status="in_progress",
                    note="系统已关联真实回信并交当前负责人核对；未冒充人工完成。"
                    + json.dumps(
                        {
                            "source_id": "bmi-incomplete",
                            "missing_fields": ["application", "quantity"],
                        }
                    ),
                ),
                idempotency_key="business-reply-received:" + s.message_id,
            )
        else:
            partial = await driver().once()
            assert partial["status"] == "waiting_customer_details"
            assert partial["evidence"]["missing_fields"] == ["application", "quantity"]
        # 重建进程后不完整回信只记一次，且不会重发追问。
        assert (await driver().once())["status"] == "waiting_customer_details"
        assert len((await followup.get(actor, s.message_id)).history) == 3
        assert len(server.sent) == 1
    replies.insert(0, "bmi-reply")
    expected_history = 3 if incomplete == "none" else 4
    final = await driver().once()
    assert final["status"] == "owner_review"
    assert final["evidence"]["fields"]["quantity"]["source_id"] == "bmi-reply"
    assert final["evidence"]["validated_need"] is False
    state = await followup.get(actor, s.message_id)
    assert (
        state.owner_id == actor.employee_id
        and state.status == "in_progress"
        and len(state.history) == expected_history
    )
    assert (await driver().once())["status"] == "owner_review"
    assert (
        len((await followup.get(actor, s.message_id)).history) == expected_history
        and len(server.sent) == 1
    )
