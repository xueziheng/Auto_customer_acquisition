"""资料权限仅用于当前用途，不提升成本、员工或会话角色。"""

from datetime import UTC, datetime

import pytest

from domains.demand.schemas import NeedUnitAccess
from shared.errors import PermissionDenied, ValidationError
from shared.schemas import evidence_read as e
from shared.schemas.quote_facts import QuoteEmployeeFact
from tests.unit.test_quote_evidence_contracts import ARTIFACT, TENANT, ULID, raw_meta
from workflows.employee_work_intake.schemas import (
    WorkSourceKind,
    WorkUploadStatus,
    WorkUploadView,
)

ACTOR = "legacy.employee-1"
UPLOAD = "upl_" + ULID
MESSAGE = "msg_" + ULID
CONVERSATION = "con_" + ULID
ACCOUNT = "acc_" + ULID
NEED = "need_" + ULID
NOW = datetime(2026, 8, 28, tzinfo=UTC)


def actor(**changes):
    return QuoteEmployeeFact(
        **{
            "tenant_id": TENANT,
            "employee_id": ACTOR,
            "role": "finance",
            "is_active": True,
            "manager_id": None,
            "team_id": None,
        }
        | changes
    )


def message(**changes):
    return e.QuoteMessageReferenceFact(
        **{
            "tenant_id": TENANT,
            "message_id": MESSAGE,
            "conversation_id": CONVERSATION,
            "account_id": ACCOUNT,
            "channel": "email",
            "direction": "inbound",
            "artifact_id": ARTIFACT,
        }
        | changes
    )


class Contexts:
    def __init__(self):
        self.actor = actor()
        self.message = message()
        self.need = e.NeedQuantitySourceFact(
            tenant_id=TENANT, need_id=NEED, account_id=ACCOUNT, quantity=None
        )
        self.calls = []

    async def read_actor(self, tenant_id, actor_id):
        self.calls.append("actor")
        return self.actor

    async def read_message(self, tenant_id, message_id):
        self.calls.append("message")
        return self.message

    async def read_need_quantity(self, tenant_id, need_id):
        return self.need


class Raw:
    def __init__(self):
        self.meta = raw_meta()
        self.metadata_calls = self.read_calls = 0

    async def get_meta(self, tenant_id, artifact_id):
        self.metadata_calls += 1
        return self.meta

    async def read(self, *args, **kwargs):
        self.read_calls += 1
        pytest.fail("授权不得读bytes")


class Uploads:
    def __init__(self):
        self.view = WorkUploadView(
            upload_id=UPLOAD,
            tenant_id=TENANT,
            artifact_id=ARTIFACT,
            employee_id=ACTOR,
            source_kind=WorkSourceKind.PDF_TEXT,
            status=WorkUploadStatus.UPLOADED,
            occurred_at=NOW,
            created_at=NOW,
            customer_timezone="UTC",
        )

    async def get_upload(self, tenant_id, upload_id, employee_id):
        if self.view is None:
            raise ValidationError("controlled missing")
        if self.view.employee_id != employee_id:
            raise PermissionDenied("controlled denied")
        return self.view


class NeedAuthorizer:
    def __init__(self):
        self.calls = []
        self.denied = False
        self.access = NeedUnitAccess(
            tenant_id=TENANT,
            need_id=NEED,
            opportunity_id="opp_" + ULID,
            account_id=ACCOUNT,
            actor_id=ACTOR,
            authorization_ref="controlled:need",
        )

    async def check(self, tenant_id, need_id, actor_id, *, action):
        self.calls.append((tenant_id, need_id, actor_id, action))
        if self.denied:
            raise PermissionDenied("controlled denied")
        return self.access


@pytest.fixture
def case():
    from workflows.quote_approval.source_access import QuoteEvidenceAccessImpl

    contexts, raw, uploads, need = Contexts(), Raw(), Uploads(), NeedAuthorizer()
    return (
        QuoteEvidenceAccessImpl(contexts, raw, uploads, need),
        contexts,
        raw,
        uploads,
        need,
    )


@pytest.mark.parametrize("role", ["boss", "product", "sourcing", "finance"])
async def test_own_upload_uses_cost_matrix_not_crm(case, role):
    access, contexts, raw, _, need = case
    contexts.actor = actor(role=role)
    result = await access.authorize(
        TENANT,
        "upload:" + UPLOAD,
        actor_id=ACTOR,
        scope=e.PricingEvidenceScope(purpose="pricing"),
    )
    assert result.actor_id == ACTOR and result.raw.artifact_id == ARTIFACT
    assert result.message_id is result.conversation_id is result.account_id is None
    assert raw.read_calls == 0 and not need.calls


@pytest.mark.parametrize(
    "change", ["missing", "inactive", "tenant", "actor", "sales", "viewer"]
)
async def test_current_actor_rejection_precedes_raw(case, change):
    access, contexts, raw, _, _ = case
    contexts.actor = {
        "missing": None,
        "inactive": actor(is_active=False),
        "tenant": actor(tenant_id="tn_" + ULID[:-1] + "2"),
        "actor": actor(employee_id="other"),
        "sales": actor(role="sales"),
        "viewer": actor(role="viewer"),
    }[change]
    with pytest.raises(e.QuoteEvidenceError) as caught:
        await access.authorize(
            TENANT,
            "upload:" + UPLOAD,
            actor_id=ACTOR,
            scope=e.PricingEvidenceScope(purpose="pricing"),
        )
    assert caught.value.code == "permission_denied" and raw.metadata_calls == 0


@pytest.mark.parametrize("value", ["", " bad", "bad ", True, "x" * 41, "a\x80"])
async def test_bad_actor_never_reaches_metadata(case, value):
    access, contexts, raw, _, _ = case
    with pytest.raises(e.QuoteEvidenceError) as caught:
        await access.authorize(
            TENANT,
            "upload:" + UPLOAD,
            actor_id=value,
            scope=e.PricingEvidenceScope(purpose="pricing"),
        )
    assert caught.value.code == "invalid_input"
    assert not contexts.calls and raw.metadata_calls == 0


@pytest.mark.parametrize("change", ["owner", "missing", "tenant", "id"])
async def test_boss_cannot_read_other_or_rebound_upload(case, change):
    access, contexts, raw, uploads, _ = case
    contexts.actor = actor(role="boss")
    updates = {
        "owner": {"employee_id": "other"},
        "tenant": {"tenant_id": "tn_" + ULID[:-1] + "2"},
        "id": {"upload_id": "upl_" + ULID[:-1] + "2"},
    }
    uploads.view = (
        None if change == "missing" else uploads.view.model_copy(update=updates[change])
    )
    with pytest.raises(e.QuoteEvidenceError) as caught:
        await access.authorize(
            TENANT,
            "upload:" + UPLOAD,
            actor_id=ACTOR,
            scope=e.PricingEvidenceScope(purpose="pricing"),
        )
    assert caught.value.code == "permission_denied" and raw.metadata_calls == 0


@pytest.mark.parametrize("action", ["read", "confirm"])
async def test_need_scope_uses_current_boss_message_account(case, action):
    access, contexts, raw, _, need = case
    contexts.actor = actor(role="boss")
    raw.meta = raw_meta(kind="email_raw", mime_type="message/rfc822")
    result = await access.authorize(
        TENANT,
        "message:" + MESSAGE,
        actor_id=ACTOR,
        scope=e.NeedUnitEvidenceScope(purpose="need_unit", need_id=NEED, action=action),
    )
    assert (result.message_id, result.conversation_id, result.account_id) == (
        MESSAGE,
        CONVERSATION,
        ACCOUNT,
    )
    assert need.calls == [(TENANT, NEED, ACTOR, action)] and raw.read_calls == 0


@pytest.mark.parametrize(
    "change", ["finance", "denied", "account", "outbound", "channel", "message", "raw"]
)
async def test_need_sources_reject_wrong_current_binding(case, change):
    access, contexts, raw, _, need = case
    contexts.actor = actor(role="finance" if change == "finance" else "boss")
    raw.meta = raw_meta(kind="email_raw", mime_type="message/rfc822")
    need.denied = change == "denied"
    changes = {
        "account": {"account_id": "acc_" + ULID[:-1] + "2"},
        "outbound": {"direction": "outbound"},
        "channel": {"channel": "sms"},
        "message": {"message_id": "msg_" + ULID[:-1] + "2"},
    }
    if change in changes:
        contexts.message = message(**changes[change])
    if change == "raw":
        raw.meta = raw_meta()
    with pytest.raises(e.QuoteEvidenceError):
        await access.authorize(
            TENANT,
            "message:" + MESSAGE,
            actor_id=ACTOR,
            scope=e.NeedUnitEvidenceScope(
                purpose="need_unit", need_id=NEED, action="confirm"
            ),
        )
    assert raw.read_calls == 0


async def test_pricing_never_uses_message_route(case):
    access, _contexts, raw, _, _ = case
    with pytest.raises(e.QuoteEvidenceError) as caught:
        await access.authorize(
            TENANT,
            "message:" + MESSAGE,
            actor_id=ACTOR,
            scope=e.PricingEvidenceScope(purpose="pricing"),
        )
    assert caught.value.code == "source_unsupported" and raw.metadata_calls == 0
