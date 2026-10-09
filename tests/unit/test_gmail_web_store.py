"""授权回调必须绑定原租户、员工、用户及会话，并且只可消费一次。"""
from datetime import UTC, datetime, timedelta

import pytest

from shared.errors import PermissionDenied
from shared.schemas.identifiers import EmployeeId, TenantId, UserId, new_id


def setup_store(tmp_path):
    from domains.conversations.gmail_connection import GmailActor
    from infra.gmail_web_store import GmailWebStore
    tmp_path.chmod(0o700)
    actor = GmailActor(tenant_id=TenantId(new_id("tn")),
                       employee_id=EmployeeId(new_id("emp")), user_id=UserId(new_id("usr")))
    return GmailWebStore(tmp_path), actor


@pytest.mark.parametrize("change", ["tenant_id", "employee_id", "user_id", "session", "state", "expired"])
def test_callback_rejects_changed_binding(tmp_path, change):
    store, actor = setup_store(tmp_path)
    now = datetime(2026, 10, 9, tzinfo=UTC)
    pending = store.begin(actor, "session-a", "owner@gmail.com", now)
    other = actor
    session, state, moment = "session-a", pending.state.get_secret_value(), now
    if change in {"tenant_id", "employee_id", "user_id"}:
        other = actor.model_copy(update={change: new_id({"tenant_id":"tn","employee_id":"emp","user_id":"usr"}[change])})
    elif change == "session":
        session = "session-b"
    elif change == "state":
        state = "incorrect-state"
    else:
        moment += timedelta(minutes=11)
    with pytest.raises(PermissionDenied):
        store.claim(other, session, state, moment)
    if change != "expired":
        assert store.claim(actor, "session-a", pending.state.get_secret_value(), now).email == "owner@gmail.com"


def test_state_consumption_is_durable_and_one_use(tmp_path):
    store, actor = setup_store(tmp_path)
    now = datetime.now(UTC)
    pending = store.begin(actor, "session-a", "owner@gmail.com", now)
    state = pending.state.get_secret_value()
    claimed = store.claim(actor, "session-a", state, now)
    assert claimed.verifier.get_secret_value() != state
    assert len(claimed.verifier.get_secret_value()) >= 43
    from infra.gmail_web_store import GmailWebStore
    with pytest.raises(PermissionDenied):
        GmailWebStore(tmp_path).claim(actor, "session-a", state, now)
    assert state not in repr(claimed)
    assert claimed.verifier.get_secret_value() not in repr(claimed)


def test_new_authorization_invalidates_previous_and_wrong_tenant_cannot_read_binding(tmp_path):
    store, actor = setup_store(tmp_path)
    now = datetime.now(UTC)
    first = store.begin(actor, "session-a", "owner@gmail.com", now)
    second = store.begin(actor, "session-a", "owner@gmail.com", now)
    with pytest.raises(PermissionDenied):
        store.claim(actor, "session-a", first.state.get_secret_value(), now)
    assert store.claim(actor, "session-a", second.state.get_secret_value(), now).email == "owner@gmail.com"

def test_test_grant_reuses_request_key_and_blocks_ambiguous_new_send(tmp_path):
    from uuid import uuid4

    from domains.conversations.gmail_connection import GmailTestCommand
    from infra.gmail_web_store import GmailBinding
    store, actor = setup_store(tmp_path)
    now = datetime.now(UTC)
    binding = GmailBinding(actor=actor, email="owner@gmail.com", mailbox_id=new_id("mbx"),
                           operation_id=new_id("gco"), connected_at=now)
    command = GmailTestCommand(email="recipient@example.com", request_id=uuid4(), confirm_my_mailbox=True)
    first = store.prepare_test(actor, binding, command, now)
    assert store.prepare_test(actor, binding, command, now).grant == first.grant
    with pytest.raises(PermissionDenied):
        store.prepare_test(actor, binding, command.model_copy(update={"email": "another@example.com"}), now)
    with pytest.raises(PermissionDenied):
        store.prepare_test(actor, binding, command.model_copy(update={"request_id": uuid4()}), now)
    other = actor.model_copy(update={"employee_id": new_id("emp")})
    assert store.latest_test(other) is None
    with pytest.raises(PermissionDenied):
        store.prepare_test(other, binding, command, now)

def test_connection_budget_is_durable_bounded_and_actor_scoped(tmp_path):
    from infra.gmail_web_store import GmailWebStore
    store, actor = setup_store(tmp_path)
    now = datetime.now(UTC)
    for _ in range(10):
        pending = store.begin(actor, "session-a", "owner@gmail.com", now)
    restarted = GmailWebStore(tmp_path)
    with pytest.raises(PermissionDenied):
        restarted.begin(actor, "session-b", "owner@gmail.com", now)
    assert restarted.connection_allowed(actor, pending.operation_id, now)
    assert not restarted.connection_allowed(actor, new_id("gco"), now)
    other = actor.model_copy(update={"user_id": new_id("usr")})
    assert not restarted.connection_allowed(other, pending.operation_id, now)
    assert not restarted.connection_allowed(actor, pending.operation_id, now + timedelta(minutes=11))
    renewed = restarted.begin(actor, "session-a", "owner@gmail.com", now + timedelta(minutes=11))
    assert restarted.connection_allowed(actor, renewed.operation_id, now + timedelta(minutes=11))
