"""仅固定 MIME 与假 transport；不连接邮箱、不调用模型。"""

from datetime import UTC, datetime, timedelta

import pytest

from connectors.qq_mail.inbound import QQInboundReader, decode_cursor, initial_cursor
from connectors.qq_mail.sent_alias import verify_sent_alias
from shared.schemas.qq_trial import QQTrialLimits, QQTrialScope, QQTrialStopped
from tests.unit.test_qq_inbound import (
    ATTEMPT,
    EMPLOYEE,
    FROM,
    OUTBOUND,
    ROUTE,
    TO,
    Scope,
    Session,
    mail,
)

ALIAS = "<provider-test@qq.com>"


def trial_scope(**changes):
    now = datetime.now(UTC)
    values = {
        "trial_id": "qqt_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "route": ROUTE,
        "attempt_id": ATTEMPT,
        "employee_id": EMPLOYEE,
        "user_id": "usr_fixture",
        "classifier_employee_id": "emp_boss",
        "classifier_user_id": "usr_boss",
        "model": "fixture-model",
        "model_configuration_version": "fixture-v1",
        "canonical_header": OUTBOUND,
        "provider_header": ALIAS,
        "alias_evidence_id": "qae_fixture",
        "authorization_ref": "synthetic-fixture-only",
        "authorization_source": "explicit_user_request",
        "starts_at": now,
        "expires_at": now + timedelta(minutes=10),
        "limits": QQTrialLimits(),
    }
    values.update(changes)
    return QQTrialScope(**values)


def sent_header(alias=ALIAS, **changes):
    values = {
        "Message-ID": alias,
        "From": TO,
        "To": FROM,
        "X-TradeOS-Idempotency-V1": OUTBOUND[1:].split("@")[0],
    }
    values.update(changes)
    return ("\r\n".join(f"{k}: {v}" for k, v in values.items()) + "\r\n\r\n").encode()


def verify(headers):
    return verify_sent_alias(
        headers,
        source="sender_sent",
        canonical_header=OUTBOUND,
        idempotency_header=OUTBOUND[1:].split("@")[0],
        sender=TO,
        recipient=FROM,
    )


@pytest.mark.parametrize(
    "headers",
    [
        (),
        (sent_header(OUTBOUND),),
        (sent_header(), sent_header()),
        (sent_header(), sent_header("<different@qq.com>")),
        (sent_header(From=FROM),),
        (sent_header(To=TO),),
        (sent_header(**{"X-TradeOS-Idempotency-V1": "other"}),),
        (sent_header(Cc=FROM),),
        (sent_header(Bcc=FROM),),
        (sent_header().replace(b"Message-ID:", b"Subject:"),),
        (sent_header().replace(b"\r\n\r\n", b"\r\nFrom: other@qq.com\r\n\r\n"),),
    ],
)
def test_alias_requires_unique_exact_sent_header_evidence(headers):
    with pytest.raises(QQTrialStopped):
        verify(headers)


def test_alias_excludes_client_appended_canonical_copy_and_preserves_source():
    raw = sent_header()
    result = verify((sent_header(OUTBOUND), raw))
    assert result.provider_header == ALIAS and result.raw_headers == raw
    assert ALIAS not in repr(result)


def test_alias_evidence_must_not_contain_body():
    with pytest.raises(QQTrialStopped):
        verify((sent_header() + b"synthetic body",))


@pytest.mark.parametrize(
    "field,value",
    [
        ("duration_seconds", 601),
        ("interval_seconds", 29),
        ("sessions", 21),
        ("searches", 21),
        ("headers", 21),
        ("bodies", 2),
        ("classifications", 2),
        ("body_bytes", 262145),
        ("provider_requests", 5),
        ("model_input_bytes", 32769),
        ("model_output_tokens", 513),
        ("model_timeout_seconds", 181),
    ],
)
def test_trial_limits_cannot_be_raised(field, value):
    with pytest.raises(ValueError):
        QQTrialLimits(**{field: value})


@pytest.mark.parametrize(
    "field", ["bodies", "classifications", "sessions", "model_output_tokens"]
)
@pytest.mark.parametrize("value", [True, 1.0, "1"])
def test_trial_integer_limits_are_strict(field, value):
    with pytest.raises(ValueError):
        QQTrialLimits(**{field: value})


@pytest.mark.parametrize("mode", ["ok", "revoked", "model_changed", "version_changed"])
async def test_current_trial_access_reuses_original_model_permission(mode):
    from types import SimpleNamespace

    from apps.scheduler_worker.adapters.reply_current_access import QQTrialCurrentAccess
    from shared.schemas.model_invocation import ModelGenerationError

    scope = trial_scope()

    class Configuration:
        async def authorize(self, actor, version, *, probe):
            assert actor.user_id == scope.classifier_user_id and not probe
            assert version == scope.model_configuration_version
            if mode == "revoked":
                raise ModelGenerationError("permission")

        async def get_public(self, actor):
            return SimpleNamespace(
                status="verified",
                model="other" if mode == "model_changed" else scope.model,
                configuration_version="other"
                if mode == "version_changed"
                else scope.model_configuration_version,
            )

    access = QQTrialCurrentAccess(scope, Scope(), Configuration())
    if mode == "revoked":
        with pytest.raises(ModelGenerationError):
            await access()
    else:
        assert await access() == (mode == "ok")


def test_incomplete_inbound_or_model_wiring_is_refused():
    from apps.composition_support.email_inbound import InboundProviderAdapter
    from apps.scheduler_worker.reply_composition import CurrentEmployeeReplyFactory
    from shared.schemas.email_inbound import InboundError

    gate = Gate()
    with pytest.raises(InboundError):
        InboundProviderAdapter(
            lambda r: None, lambda *a: "", lambda *a: None, trial_gate=gate
        )
    with pytest.raises(ValueError):
        CurrentEmployeeReplyFactory(
            ROUTE.tenant_id, EMPLOYEE, classifier=object(), trial_gate=gate
        )


class Gate:
    def __init__(self):
        self.scope = trial_scope()
        self.start = None
        self.calls = []
        self.revoked = False
        self.stopped = None

    async def check(self):
        if self.revoked or self.stopped:
            raise QQTrialStopped()

    async def stop(self, reason):
        self.stopped = reason

    async def watermark(self):
        return self.start

    async def initialize(self, value):
        self.start = value

    async def reserve(self, kind, **kwargs):
        await self.check()
        self.calls.append(kind)


async def test_initial_snapshot_reads_zero_history_then_only_new_uid_and_preserves_raw():
    gate = Gate()
    session = Session({1: mail(reply=ALIAS)})
    reader = QQInboundReader(
        ROUTE, EMPLOYEE, session, Scope(), address=TO, trial_gate=gate
    )
    cursor = initial_cursor(ROUTE, attempt_id=ATTEMPT)
    page = await reader.fetch_inbound_page(ROUTE.mailbox_alias, cursor, 20)
    assert page.items == () and session.searches == [] and session.downloads == []
    assert gate.start.high_uid == 1 and gate.calls == ["sessions"]
    session.messages[2] = mail(reply=ALIAS)
    session.next_uid = 3
    page2 = await reader.fetch_inbound_page(ROUTE.mailbox_alias, page.next_cursor, 20)
    assert len(page2.items) == 1 and session.downloads == [2]
    assert page2.items[0].in_reply_to == OUTBOUND
    assert page2.items[0].raw_mime == session.messages[2]
    assert decode_cursor(page2.next_cursor, ROUTE).last_uid == 2


@pytest.mark.parametrize(
    "change", ["validity", "scope", "oversize", "attachment", "mismatched_body"]
)
async def test_trial_stops_before_unauthorized_body_or_commit(change):
    from shared.schemas.email_inbound import InboundError

    gate = Gate()
    session = Session({1: mail(reply=ALIAS)})
    reader = QQInboundReader(
        ROUTE, EMPLOYEE, session, Scope(), address=TO, trial_gate=gate
    )
    initial = await reader.fetch_inbound_page(
        ROUTE.mailbox_alias, initial_cursor(ROUTE, attempt_id=ATTEMPT), 20
    )
    raw = mail(reply=ALIAS)
    if change == "validity":
        session.validity += 1
    if change == "scope":
        gate.revoked = True
    if change == "oversize":
        raw = mail(reply=ALIAS, body="x" * 262145)
    if change == "attachment":
        raw = mail(reply=ALIAS, extra="Content-Disposition: attachment\r\n")
    if change == "mismatched_body":
        session.changed = mail(reply=OUTBOUND)
    session.messages[2] = raw
    session.next_uid = 3
    with pytest.raises((InboundError, QQTrialStopped)):
        await reader.fetch_inbound_page(ROUTE.mailbox_alias, initial.next_cursor, 20)
    assert session.downloads == ([2] if change == "mismatched_body" else [])
