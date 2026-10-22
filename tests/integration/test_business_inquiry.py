"""真实PG历史和Gateway验收；仅外部模型、SMTP和原件端口受控。"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from domains.conversations.business_inquiry import (
    InquiryAnalysis,
    InquiryAutomationScope,
)
from domains.conversations.business_mail import BusinessMailResult
from infra.db.business_inquiry import InquiryRecords
from shared.schemas.identifiers import new_id
from tests.integration.test_business_mail import setup_mail
from tests.integration.test_business_reply import setup_gateway
from tests.unit.test_business_inquiry import PLAN, TEXT
from tool_gateway.handlers.business_inquiry import InquiryReplyHandler
from tool_gateway.handlers.business_reply import MANIFEST
from tool_gateway.manifest import ToolRegistry
from workflows.business_inquiry.flow import BusinessInquiryDriver


async def make_driver(engine, tmp_path, *, interrupted=False):
    _sessions, actor, binding, source, box, page = await setup_mail(engine)
    item = page.messages[0].model_copy(
        update={
            "sender": "buyer@example.com",
            "subject": "",
            "body_text": TEXT,
            "message_header": "<buyer-inquiry@example.com>",
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
            category="business", reason="询盘", evidence_quote="solar"
        ),
        failure=None,
    )
    config = InquiryAutomationScope(
        enabled=True,
        actor=actor,
        sender="1249583590@qq.com",
        mailbox_id=box,
        binding_operation_id="aco-current",
        identity_id=new_id("sid"),
        identity_version=2,
        approved_at=datetime.now(UTC) - timedelta(minutes=1),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        allowed_recipients=("buyer@example.com",),
        recovery_message_ids=(),
        purpose="internal_workflow_test",
        approval_source="验收fixture",
    )
    path = tmp_path / "scope.json"
    path.write_text(config.model_dump_json())
    path.chmod(0o600)
    records = InquiryRecords(source, path, config)
    materials = {
        claim.message_id: {
            "message_id": claim.message_id,
            "artifact_id": item.artifact_id,
            "source_sha256": item.source_sha256,
            "body": TEXT,
            "subject": "",
            "revision": 1,
            "classification_status": "classified",
            "category": "business",
            "classification_origin": "model",
            "headers": {
                "From": "buyer@example.com",
                "To": config.sender,
                "Message-ID": "<buyer-inquiry@example.com>",
            },
        }
    }
    model_calls = []
    servers = []
    policies = []
    replies = []

    async def read_source(original, message_id):
        return materials[message_id]

    def planner_factory(original, material, identity):
        class Planner:
            async def analyze(self, ident, source, followup):
                model_calls.append(ident.run_id)
                if interrupted:
                    raise RuntimeError("controlled model interruption")
                return InquiryAnalysis.model_validate(
                    PLAN
                    if not followup
                    else {
                        "intent": "customer_details",
                        "reason": "用途数量补充",
                        "intent_quote": "100 units for indoor ventilation",
                        "fields": [
                            {
                                "name": "quantity",
                                "value": "100 units",
                                "evidence_quote": "100 units",
                            },
                            {
                                "name": "application",
                                "value": "indoor ventilation",
                                "evidence_quote": "indoor ventilation",
                            },
                        ],
                        "next_fields": ["product"],
                    }
                )

        return Planner()

    def send_factory(s):
        gateway, _ctx, server, policy, _clock, _creds, old_handler = setup_gateway(
            engine, s
        )
        handler = InquiryReplyHandler(
            s, old_handler.fingerprints, old_handler.connector_factory
        )
        registry = ToolRegistry()
        registry.register(MANIFEST, handler)
        # Gateway registry is passed through its real constructor using the original fixture's checks/UoW.
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
        from tool_gateway.handlers.business_reply import STAGES, BusinessReplyCheck
        from tool_gateway.pipeline import ToolGateway

        sessions = async_sessionmaker(engine, expire_on_commit=False)
        gateway = ToolGateway(
            registry,
            {n: BusinessReplyCheck(n, s, policy) for n in STAGES},
            lambda tenant: SqlAlchemyToolGatewayUnitOfWork(sessions, tenant),
            lease_duration=timedelta(seconds=120),
            lease_owner="inquiry-test",
            id_factory=new_id,
        )

        async def candidates():
            return replies

        policy.candidates = candidates
        servers.append(server)
        policies.append(policy)
        return policy, handler, gateway

    async def heartbeat():
        pass

    def driver():
        return BusinessInquiryDriver(
            records,
            read_source,
            planner_factory,
            send_factory,
            heartbeat,
            model="fixture",
            version="fixture",
        )

    return (
        driver,
        records,
        claim.message_id,
        model_calls,
        servers,
        replies,
        materials,
        path,
        binding,
    )


async def test_arbitrary_new_inquiry_model_send_reply_and_restart(
    integration_engine, tmp_path
):
    (
        factory,
        records,
        original,
        calls,
        servers,
        replies,
        materials,
        _path,
        _binding,
    ) = await make_driver(integration_engine, tmp_path)
    assert await records.list_messages() == [original]
    assert await factory().once(original) == "waiting_customer"
    assert len(calls) == 1 and sum(len(s.sent) for s in servers) == 1
    assert await factory().once(original) == "waiting_customer"
    assert len(calls) == 1 and sum(len(s.sent) for s in servers) == 1
    reply = new_id("bmi")
    replies.append(reply)
    materials[reply] = {
        "message_id": reply,
        "artifact_id": "art-reply",
        "source_sha256": "a" * 64,
        "revision": 1,
        "classification_status": "classified",
        "category": "business",
        "classification_origin": "model",
        "body": "We need 100 units for indoor ventilation (test data).",
        "headers": {
            "From": "buyer@example.com",
            "To": records.scope.sender,
            "Message-ID": "<reply@example.com>",
            "In-Reply-To": "<buyer-inquiry@example.com>",
        },
    }
    assert await factory().once(original) == "owner_review"
    state = await records.view(original)
    assert (
        state.status == "in_progress"
        and state.owner_id == records.scope.actor.employee_id
    )
    assert len(calls) == 2 and sum(len(s.sent) for s in servers) == 1
    assert await factory().once(original) == "owner_review"
    assert len((await records.view(original)).history) == len(state.history)
    assert len(calls) == 2 and sum(len(s.sent) for s in servers) == 1
    analyses = [
        json.loads(e.note)
        for e in state.history
        if e.request_key.startswith("inquiry-analysis:")
    ]
    assert all(
        v["confirmed_by"] is None and not v["validated_need"] and not v["opportunity"]
        for v in analyses
    )


async def test_model_unknown_does_not_retry_after_reconstruction(
    integration_engine, tmp_path
):
    factory, _records, original, calls, servers, *_ = await make_driver(
        integration_engine, tmp_path, interrupted=True
    )
    with pytest.raises(RuntimeError):
        await factory().once(original)
    assert await factory().once(original) == "model_review_required"
    assert len(calls) == 1 and not servers


async def test_revoked_scope_stops_even_with_prior_result(integration_engine, tmp_path):
    (
        factory,
        records,
        original,
        calls,
        servers,
        _replies,
        _materials,
        path,
        _binding,
    ) = await make_driver(integration_engine, tmp_path)
    await factory().once(original)
    path.write_text(
        records.scope.model_copy(
            update={"approval_source": "changed"}
        ).model_dump_json()
    )
    from shared.errors import PermissionDenied

    with pytest.raises(PermissionDenied):
        await factory().once(original)
    assert len(calls) == 1 and sum(len(s.sent) for s in servers) == 1


@pytest.mark.parametrize("state", ["pending", "classifying"])
async def test_reply_waits_for_triage_without_claim_then_model_review_extracts(
    integration_engine, tmp_path, state
):
    (
        factory,
        records,
        original,
        calls,
        servers,
        replies,
        materials,
        *_,
    ) = await make_driver(integration_engine, tmp_path)
    assert await factory().once(original) == "waiting_customer"
    before = len((await records.view(original)).history)
    reply = new_id("bmi")
    replies.append(reply)
    materials[reply] = {
        "message_id": reply,
        "artifact_id": "art-reply",
        "source_sha256": "a" * 64,
        "revision": 0,
        "classification_status": state,
        "category": "pending",
        "classification_origin": None,
        "body": "100 units for indoor ventilation",
        "headers": {
            "From": "buyer@example.com",
            "To": records.scope.sender,
            "Message-ID": "<reply@example.com>",
            "In-Reply-To": "<buyer-inquiry@example.com>",
        },
    }
    assert await factory().once(original) == "waiting_classification"
    assert await factory().once(original) == "waiting_classification"
    assert len(calls) == 1 and len((await records.view(original)).history) == before
    materials[reply].update(
        revision=1,
        classification_status="classified",
        category="review",
        classification_origin="model",
    )
    assert await factory().once(original) == "owner_review"
    assert await factory().once(original) == "owner_review"
    assert len(calls) == 2 and sum(len(s.sent) for s in servers) == 1


@pytest.mark.parametrize(
    "status,category,origin",
    [
        ("classified", "review", "human"),
        ("classified", "other", "model"),
        ("failed", "review", "system"),
    ],
)
async def test_restricted_reply_does_not_create_model_claim(
    integration_engine, tmp_path, status, category, origin
):
    (
        factory,
        records,
        original,
        calls,
        servers,
        replies,
        materials,
        *_,
    ) = await make_driver(integration_engine, tmp_path)
    await factory().once(original)
    before = len((await records.view(original)).history)
    reply = new_id("bmi")
    replies.append(reply)
    materials[reply] = dict(
        materials[original],
        message_id=reply,
        classification_status=status,
        category=category,
        classification_origin=origin,
        headers={
            "From": "buyer@example.com",
            "To": records.scope.sender,
            "Message-ID": "<reply@example.com>",
            "In-Reply-To": "<buyer-inquiry@example.com>",
        },
    )
    assert await factory().once(original) == "reply_review_required"
    assert len(calls) == 1 and len((await records.view(original)).history) == before
    assert sum(len(s.sent) for s in servers) == 1


async def test_metadata_remembers_human_review(integration_engine, tmp_path):
    from domains.conversations.business_mail import BusinessMailCorrection
    from infra.db.business_inquiry import InquirySourcePolicy

    _, records, original, *_ = await make_driver(integration_engine, tmp_path)
    policy = InquirySourcePolicy(records, original, lambda _: "fixture")
    assert (await policy.metadata(original))["classification_origin"] == "model"
    meta = await policy.metadata(original)
    await records.source.correct(
        records.scope.actor,
        original,
        BusinessMailCorrection(
            expected_revision=meta["revision"], category="review", note="人工核对"
        ),
    )
    current = await policy.metadata(original)
    assert (
        current["classification_origin"] == "human" and current["category"] == "review"
    )
