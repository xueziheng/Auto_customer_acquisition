"""真实PG/RawStore/公开上传/Gateway链；受控parser不代表Linux验收。"""

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import TypeAdapter
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from artifact_store.service_impl import RawArtifactStoreImpl
from artifact_store.store import RawArtifactKind
from artifact_store.transport import BlobReadLimitExceeded
from domains.demand.schemas import NeedUnitAccess
from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork
from infra.db.quote_evidence_context import SqlAlchemyQuoteEvidenceContextReader
from infra.db.tables import (
    ConversationRow,
    EmployeeRow,
    MessageRow,
    OpportunityRow,
    ToolCallEventRow,
    ToolCallRow,
    ValidatedNeedRow,
)
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from infra.db.work_intake_uow import SqlAlchemyWorkIntakeUnitOfWork
from infra.quote_evidence_artifacts import RawQuoteEvidenceAdapter
from shared.errors import PermissionDenied
from shared.schemas import evidence_read as e
from shared.schemas.identifiers import new_id
from shared.schemas.provenance import FactualField, Provenance, SourceType
from tests.unit.test_evidence_text_profiles import parse_limits, pdf_bytes
from tests.unit.test_quote_evidence_gateway import TEXT, Parser
from tool_gateway.checks.quote_evidence import (
    QuoteEvidencePermissionCheck,
    QuoteEvidenceTenantCheck,
)
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.quote_evidence import (
    MANIFEST,
    QuoteEvidenceReadHandler,
    ToolGatewayQuoteEvidenceReader,
)
from tool_gateway.handlers.quote_evidence_slots import QuoteEvidenceResultSlot
from tool_gateway.manifest import ToolRegistry
from tool_gateway.pipeline import ToolGateway
from workflows.employee_work_intake.schemas import WorkSourceKind
from workflows.employee_work_intake.service_impl import WorkIntakeServiceImpl
from workflows.quote_approval.source_access import QuoteEvidenceAccessImpl

NOW = datetime(2026, 8, 28, tzinfo=UTC)


class ControlledNeedAccess:
    """T8A显式测试依赖，无生产默认授权。"""

    def __init__(self, tenant, need, account, opportunity, actor):
        self.fact = NeedUnitAccess(
            tenant_id=tenant,
            need_id=need,
            account_id=account,
            opportunity_id=opportunity,
            actor_id=actor,
            authorization_ref="controlled:need",
        )
        self.denied = False
        self.calls = []
        self._depth = ContextVar("quote_evidence_need_guard_depth", default=0)

    @property
    def depth(self):
        return self._depth.get()

    async def check(self, tenant_id, need_id, actor_id, *, action):
        self.calls.append(action)
        if self.denied or (tenant_id, need_id, actor_id) != (
            self.fact.tenant_id,
            self.fact.need_id,
            self.fact.actor_id,
        ):
            raise PermissionDenied("受控权限拒绝")
        return self.fact

    @asynccontextmanager
    async def guard(self, tenant_id, need_id, actor_id, *, action):
        access = await self.check(tenant_id, need_id, actor_id, action=action)
        token = self._depth.set(self.depth + 1)
        try:
            yield access
        finally:
            self._depth.reset(token)


class Transport:
    def __init__(self):
        self.values = {}
        self.reads = 0
        self.before = None

    async def put(self, key, content):
        self.values[key] = content

    async def delete(self, key):
        self.values.pop(key, None)

    async def get(self, key):
        pytest.fail("禁止无界读取")

    async def get_bounded(self, key, *, maximum_bytes):
        self.reads += 1
        if self.before:
            await self.before()
        content = self.values[key]
        if len(content) > maximum_bytes:
            raise BlobReadLimitExceeded()
        return content


async def build_case(engine, parser=None):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant, need, account, opportunity, conversation, message = (
        new_id(p) for p in ("tn", "need", "acc", "opp", "con", "msg")
    )
    actor = "legacy." + uuid4().hex[:24]
    transport = Transport()
    store = RawArtifactStoreImpl(
        lambda tn: SqlAlchemyArtifactUnitOfWork(factory, tn),
        transport,
        2097152,
        lambda: NOW,
        new_id,
        bounded_transport=transport,
    )
    pdf = await store.put(
        tenant, RawArtifactKind.PDF, pdf_bytes(TEXT), "application/pdf"
    )
    email = await store.put(
        tenant,
        RawArtifactKind.EMAIL_RAW,
        b"Content-Type: text/plain; charset=utf-8\r\n\r\nWe need 50 pieces.\r\n",
        "message/rfc822",
    )
    quantity = FactualField(
        50,
        Provenance(
            SourceType.CONVERSATION,
            message,
            "human",
            NOW,
            confirmed_by=actor,
            confirmed_at=NOW,
            source_quote="We need 50 pieces.",
        ),
    )
    category = FactualField(
        "hardware",
        Provenance(
            SourceType.CONVERSATION,
            message,
            "human",
            NOW,
            confirmed_by=actor,
            confirmed_at=NOW,
            source_quote="Controlled hardware request",
        ),
    )
    async with factory() as session, session.begin():
        session.add(
            EmployeeRow(
                tenant_id=tenant,
                employee_id=actor,
                name="受控员工",
                role="boss",
                is_active=True,
            )
        )
        session.add(
            ConversationRow(
                tenant_id=tenant,
                conversation_id=conversation,
                account_id=account,
                channel="email",
                created_at=NOW,
            )
        )
        await session.flush()
        session.add(
            MessageRow(
                tenant_id=tenant,
                message_id=message,
                conversation_id=conversation,
                direction="inbound",
                sent_at=NOW,
                raw_artifact_ref=email.artifact_id,
                external_message_id="controlled:" + message,
            )
        )
        session.add(
            ValidatedNeedRow(
                tenant_id=tenant,
                need_id=need,
                account_id=account,
                product_category=TypeAdapter(FactualField[str]).dump_python(
                    category, mode="json"
                ),
                quantity=TypeAdapter(FactualField[int]).dump_python(
                    quantity, mode="json"
                ),
                source_message_id=message,
                source_conversation_id=conversation,
                status="validated",
                created_at=NOW,
            )
        )
        session.add(
            OpportunityRow(
                tenant_id=tenant,
                opportunity_id=opportunity,
                need_id=need,
                account_id=account,
                account_name="Controlled account",
                country="DE",
                product_category="hardware",
                owner=actor,
            )
        )
    uploads = WorkIntakeServiceImpl(
        lambda tn: SqlAlchemyWorkIntakeUnitOfWork(factory, tn),
        now=lambda: NOW,
        id_generator=new_id,
    )
    upload = await uploads.register_upload(
        tenant,
        pdf.artifact_id,
        actor,
        WorkSourceKind.PDF_TEXT,
        occurred_at=NOW,
        customer_timezone="UTC",
    )
    contexts = SqlAlchemyQuoteEvidenceContextReader(factory, statement_timeout_ms=1000)
    raw = RawQuoteEvidenceAdapter(store)
    need_access = ControlledNeedAccess(tenant, need, account, opportunity, actor)
    access = QuoteEvidenceAccessImpl(contexts, raw, uploads, need_access)
    parser = parser or Parser()
    slot = QuoteEvidenceResultSlot(new_id)
    handler = QuoteEvidenceReadHandler(
        access,
        raw,
        parser,
        slot,
        HmacFingerprintProvider("test", b"x" * 32),
        maximum_raw_bytes=2097152,
        parser_limits=parse_limits(),
    )
    registry = ToolRegistry()
    registry.register(MANIFEST, handler)
    faults = SimpleNamespace(commit=None)

    class LedgerUow(SqlAlchemyToolGatewayUnitOfWork):
        async def __aexit__(self, exc_type, exc, tb):
            if exc_type is None and faults.commit:
                status = await self._session.scalar(
                    select(ToolCallRow.status)
                    .where(ToolCallRow.tenant_id == tenant)
                    .order_by(
                        ToolCallRow.created_at.desc(), ToolCallRow.tool_call_id.desc()
                    )
                    .limit(1)
                )
                if status == faults.commit:
                    await self._session.rollback()
                    await self._session.close()
                    raise RuntimeError("受控账本提交失败")
            return await super().__aexit__(exc_type, exc, tb)

    class CountingGateway(ToolGateway):
        invocations = 0

        async def invoke(self, ctx):
            self.invocations += 1
            return await super().invoke(ctx)

    gateway = CountingGateway(
        registry,
        {
            "tenant": QuoteEvidenceTenantCheck(tenant),
            "permission": QuoteEvidencePermissionCheck(access, slot),
        },
        lambda tn: LedgerUow(factory, tn, now=lambda: NOW),
        lease_duration=timedelta(seconds=20),
        lease_owner="evidence-test",
        now=lambda: NOW,
        id_factory=new_id,
    )
    reader = ToolGatewayQuoteEvidenceReader(gateway, slot, access)
    return SimpleNamespace(**locals())


def preview(case):
    return e.EvidencePreviewRequest(
        operation="preview",
        source_ref="upload:" + case.upload.upload_id,
        scope=e.PricingEvidenceScope(purpose="pricing"),
        profile="pdf-text-v1",
        page=1,
    )


async def test_real_projection_and_legacy_actor_gateway(integration_engine, caplog):
    case = await build_case(integration_engine)
    actor = await case.contexts.read_actor(case.tenant, case.actor)
    assert actor.employee_id == case.actor and actor.is_active
    assert await case.contexts.read_actor(new_id("tn"), case.actor) is None
    assert await case.contexts.read_actor(case.tenant, "legacy.missing") is None
    fact = await case.contexts.read_message(case.tenant, case.message)
    assert (
        fact.account_id == case.account and fact.artifact_id == case.email.artifact_id
    )
    assert await case.contexts.read_message(new_id("tn"), case.message) is None
    current = await case.contexts.read_need_quantity(case.tenant, case.need)
    assert current.quantity == case.quantity
    assert await case.contexts.read_need_quantity(new_id("tn"), case.need) is None

    async def check_committed():
        async with case.factory() as session, session.begin():
            statuses = (
                await session.scalars(
                    select(ToolCallRow.status).where(
                        ToolCallRow.tenant_id == case.tenant
                    )
                )
            ).all()
            assert "executing" in statuses
            for table, key, identity in [
                (EmployeeRow, "employee_id", case.actor),
                (ValidatedNeedRow, "need_id", case.need),
                (OpportunityRow, "opportunity_id", case.opportunity),
            ]:
                await session.execute(
                    select(getattr(table, key))
                    .where(
                        table.tenant_id == case.tenant, getattr(table, key) == identity
                    )
                    .with_for_update(nowait=True)
                )

    case.transport.before = check_committed
    result = await case.reader.read(case.tenant, preview(case), actor_id=case.actor)
    assert result.reference.actor_id == case.actor and result.text == TEXT
    assert case.slot.is_empty
    async with case.factory() as session:
        calls = (
            await session.scalars(
                select(ToolCallRow).where(ToolCallRow.tenant_id == case.tenant)
            )
        ).all()
        events = (
            await session.scalars(
                select(ToolCallEventRow).where(
                    ToolCallEventRow.tenant_id == case.tenant
                )
            )
        ).all()
        snapshot = repr(
            [
                {
                    key: value
                    for key, value in vars(row).items()
                    if not key.startswith("_")
                }
                for row in [*calls, *events]
            ]
        )
        assert TEXT not in snapshot and TEXT not in caplog.text
        assert calls[-1].status == "succeeded"


@pytest.mark.parametrize("status", ["executing", "succeeded"])
async def test_real_commit_failure_never_returns_content(integration_engine, status):
    case = await build_case(integration_engine)
    case.faults.commit = status
    with pytest.raises(e.QuoteEvidenceError) as caught:
        await case.reader.read(case.tenant, preview(case), actor_id=case.actor)
    expected = "source_unavailable" if status == "executing" else "gateway_unavailable"
    assert caught.value.code == expected and case.slot.is_empty
    assert case.gateway.invocations == 1
    assert (
        case.transport.reads == case.parser.calls == (0 if status == "executing" else 1)
    )


@pytest.mark.parametrize("field,value", [("is_active", False), ("role", "sales")])
async def test_concurrent_revocation_during_raw_read(integration_engine, field, value):
    case = await build_case(integration_engine)
    entered, release = asyncio.Event(), asyncio.Event()

    async def block():
        entered.set()
        await release.wait()

    case.transport.before = block
    call = asyncio.create_task(
        case.reader.read(case.tenant, preview(case), actor_id=case.actor)
    )
    try:
        await asyncio.wait_for(entered.wait(), 3)
        async with case.factory() as session, session.begin():
            await session.execute(
                update(EmployeeRow)
                .where(
                    EmployeeRow.tenant_id == case.tenant,
                    EmployeeRow.employee_id == case.actor,
                )
                .values(**{field: value})
            )
        release.set()
        with pytest.raises(e.QuoteEvidenceError) as caught:
            await call
        assert caught.value.code == "permission_denied" and case.slot.is_empty
    finally:
        release.set()
        if not call.done():
            call.cancel()
        await asyncio.gather(call, return_exceptions=True)


async def test_corrupt_projection_is_fixed_unavailable(integration_engine):
    case = await build_case(integration_engine)
    async with case.factory() as session, session.begin():
        await session.execute(
            update(ValidatedNeedRow)
            .where(
                ValidatedNeedRow.tenant_id == case.tenant,
                ValidatedNeedRow.need_id == case.need,
            )
            .values(quantity={"value": "50", "private": "controlled"})
        )
    with pytest.raises(e.QuoteEvidenceError) as caught:
        await case.contexts.read_need_quantity(case.tenant, case.need)
    assert caught.value.code == "source_unavailable" and "controlled" not in str(
        caught.value
    )


@pytest.mark.parametrize("identity", ["missing", "wrong_tenant", "inactive"])
async def test_real_missing_or_inactive_actor_has_zero_raw_io(
    integration_engine, identity
):
    case = await build_case(integration_engine)
    actor = case.actor
    if identity == "missing":
        actor = "legacy.missing"
    else:
        changes = (
            {"tenant_id": new_id("tn")}
            if identity == "wrong_tenant"
            else {"is_active": False}
        )
        async with case.factory() as session, session.begin():
            await session.execute(
                update(EmployeeRow)
                .where(
                    EmployeeRow.tenant_id == case.tenant,
                    EmployeeRow.employee_id == case.actor,
                )
                .values(**changes)
            )
    with pytest.raises(e.QuoteEvidenceError) as caught:
        await case.reader.read(case.tenant, preview(case), actor_id=actor)
    assert caught.value.code == "permission_denied"
    assert case.transport.reads == case.parser.calls == 0 and case.slot.is_empty


async def test_message_rebinding_during_read_is_rejected(integration_engine):
    case = await build_case(integration_engine)
    replacement = await case.store.put(
        case.tenant,
        RawArtifactKind.EMAIL_RAW,
        b"Content-Type: text/plain\n\nControlled replacement",
        "message/rfc822",
    )

    async def rebind():
        async with case.factory() as session, session.begin():
            await session.execute(
                update(MessageRow)
                .where(
                    MessageRow.tenant_id == case.tenant,
                    MessageRow.message_id == case.message,
                )
                .values(raw_artifact_ref=replacement.artifact_id)
            )

    case.transport.before = rebind
    request = e.EvidencePreviewRequest(
        operation="preview",
        source_ref="message:" + case.message,
        scope=e.NeedUnitEvidenceScope(
            purpose="need_unit", need_id=case.need, action="read"
        ),
        profile="rfc822-plain-v1",
        page=None,
    )
    with pytest.raises(e.QuoteEvidenceError) as caught:
        await case.reader.read(case.tenant, request, actor_id=case.actor)
    assert caught.value.code == "permission_denied" and case.slot.is_empty
