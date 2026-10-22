"""持续 QQ 回信的合成协议回归；不连接邮箱或模型。"""
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from apps.scheduler_worker.inbound_driver import InboundDriver
from connectors.qq_mail.inbound import (
    QQCursor,
    QQInboundReader,
    decode_cursor,
    encode_cursor,
)
from infra.standalone.settings import QQReplySettings
from shared.schemas.email_inbound import InboundError
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

ALIAS = "<verified-continuous@qq.com>"


def cursor(last=10):
    return encode_cursor(QQCursor(route=ROUTE, attempt_id=ATTEMPT,
        outbound_message_header=OUTBOUND, uidvalidity=7, last_uid=last))


def scope():
    value = Scope()
    value.grant = replace(value.grant, provider_header=ALIAS)
    return value


def reader(mailbox, current=None):
    return QQInboundReader(ROUTE, EMPLOYEE, mailbox, current or scope(), address=TO)


async def test_new_delayed_and_reconstructed_reader_continue_without_old_body():
    mailbox = Session({10: mail(reply=ALIAS), 11: mail(reply=ALIAS)})
    first = await reader(mailbox).fetch_inbound_page(ROUTE.mailbox_alias, cursor(), 1)
    assert len(first.items) == 1 and mailbox.downloads == [11]
    mailbox.messages[12] = mail(reply=ALIAS, body="Delayed reply").replace(b"reply1@", b"reply2@")
    mailbox.next_uid = 13
    second = await reader(mailbox).fetch_inbound_page(ROUTE.mailbox_alias, first.next_cursor, 1)
    assert len(second.items) == 1 and mailbox.downloads == [11, 12]
    again = await reader(mailbox).fetch_inbound_page(ROUTE.mailbox_alias, second.next_cursor, 20)
    assert not again.items and decode_cursor(again.next_cursor, ROUTE).last_uid == 12
    assert mailbox.downloads == [11, 12]


async def test_bounded_horizon_catches_backlog_without_unbounded_search():
    mailbox = Session({11: mail(reply=ALIAS), 250: mail(reply=ALIAS)})
    first = await reader(mailbox).fetch_inbound_page(ROUTE.mailbox_alias, cursor(), 20)
    assert decode_cursor(first.next_cursor, ROUTE).last_uid == 110
    second = await reader(mailbox).fetch_inbound_page(ROUTE.mailbox_alias, first.next_cursor, 20)
    third = await reader(mailbox).fetch_inbound_page(ROUTE.mailbox_alias, second.next_cursor, 20)
    assert len(third.items) == 1 and mailbox.downloads == [11, 250]
    assert all(high-low <= 100 for _, _, low, high in mailbox.searches)


@pytest.mark.parametrize("case", ["empty_cursor", "validity", "revoke", "unsafe_header", "body_changed"])
async def test_uncertain_scope_or_message_never_advances(case):
    mailbox = Session({11: mail(reply=ALIAS)})
    current = scope()
    start = cursor()
    if case == "empty_cursor":
        start = encode_cursor(QQCursor(route=ROUTE, attempt_id=ATTEMPT))
    elif case == "validity":
        mailbox.validity = 99
    elif case == "revoke":
        current.revoke = True
    elif case == "unsafe_header":
        mailbox.messages[11] = mail(reply=ALIAS, extra="Cc: 123456789@qq.com\r\n")
    else:
        mailbox.changed = mail(reply=ALIAS, sender="111111111@qq.com")
    with pytest.raises(InboundError):
        await reader(mailbox, current).fetch_inbound_page(ROUTE.mailbox_alias, start, 20)


async def test_continuous_accepts_only_existing_qq_address_compatibility():
    raw = mail(reply=ALIAS).replace(
        f"From: {FROM}".encode(),
        f'From: "=?utf-8?B?YWJjZA==?=" <{FROM}>'.encode(),
    )
    mailbox = Session({11: raw})
    page = await reader(mailbox).fetch_inbound_page(ROUTE.mailbox_alias, cursor(), 20)
    assert len(page.items) == 1 and mailbox.downloads == [11]


@pytest.mark.parametrize("case", ["trial", "recovery", "no_attempt", "too_fast"])
def test_configuration_requires_explicit_single_mode(tmp_path, case):
    values = {"employee_id": "emp_owner", "user_id": "usr_owner", "classifier_employee_id": "emp_boss",
        "mailbox_directory": tmp_path, "attempt_id": ATTEMPT, "continuous": {
            "route": ROUTE.model_dump(), "source_trial_id": "qqt_01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "alias_evidence_id": "qae_01ARZ3NDEKTSV4RRFFQ69G5FAV", "authorization_ref": "fixture"}}
    if case == "trial":
        values["trial"] = {}
    elif case == "recovery":
        values["recovery_only"] = True
    elif case == "no_attempt":
        values["attempt_id"] = None
    else:
        values["continuous"]["poll_interval_seconds"] = 1
    with pytest.raises(ValueError):
        QQReplySettings.model_validate(values)


async def test_restart_obeys_durable_cooldown_and_retry_after():
    now = datetime.now(UTC)
    state = SimpleNamespace(blocked_reason=None, next_retry_at=None, last_succeeded_at=now)
    class Store:
        async def read_cursor(self):
            return state
    composition = SimpleNamespace(store=Store())
    # reader_for 刻意缺省；处于持久冷却时不得尝试外部调用。
    assert await InboundDriver(composition, now=lambda: now+timedelta(seconds=59),
        poll_interval_seconds=60).scan_once() == 0
    state.blocked_reason = "provider_transient"
    state.next_retry_at = now+timedelta(seconds=120)
    assert await InboundDriver(composition, now=lambda: now+timedelta(seconds=61),
        poll_interval_seconds=60).scan_once() == 0


def test_continuous_keeps_original_reply_runner_instructions(monkeypatch, tmp_path):
    from apps.composition_support.model import configured_codex_provider
    from connectors.codex_agent import client
    from shared.schemas.identifiers import new_id
    from shared.schemas.model_invocation import InvocationIdentity
    from tests.unit.test_qq_reply_runtime import qq_settings
    configured = qq_settings(tmp_path)
    qq = configured.qq_reply.model_copy(update={"continuous": SimpleNamespace(route=ROUTE)})
    configured = configured.model_copy(update={"qq_reply": qq, "api_key_file": tmp_path/"key",
        "api_control_tenant": "control", "codex_job_root": tmp_path/"jobs"})
    identity = InvocationIdentity(tenant_id=ROUTE.tenant_id, employee_id=EMPLOYEE,
        user_id="usr_fixture", run_id=new_id("run"), capability="reply_qualification",
        configuration_version=configured.configuration_version, sequence=0)
    captured = []
    def provider(delegate, *, settings, scope, discovery):
        captured.append(settings)
        return object()
    monkeypatch.setattr(client, "codex_agent_provider", provider)
    configured_codex_provider(configured, identity)
    assert captured[0].reply_classification is True



def chain_mail(*, parent="<reply1@qq.com>", references=None, sender=FROM):
    references = references if references is not None else ALIAS + " " + parent
    return mail(reply=parent, sender=sender, extra="References: " + references + "\r\n",
        body="Please confirm MOQ and delivery time.").replace(b"Message-ID: <reply1@", b"Message-ID: <reply2@")


async def test_threaded_new_and_delayed_reply_preserve_original_outbound_link():
    mailbox = Session({11: chain_mail()})
    page = await reader(mailbox).fetch_inbound_page(ROUTE.mailbox_alias, cursor(), 20)
    assert len(page.items) == 1 and mailbox.downloads == [11]
    assert page.items[0].in_reply_to == OUTBOUND
    mailbox.messages[12] = chain_mail(parent="<reply2@qq.com>",
        references=ALIAS + " <reply1@qq.com> <reply2@qq.com>").replace(b"Message-ID: <reply2@", b"Message-ID: <reply3@")
    mailbox.next_uid = 13
    delayed = await reader(mailbox).fetch_inbound_page(ROUTE.mailbox_alias, page.next_cursor, 20)
    assert len(delayed.items) == 1 and mailbox.downloads == [11, 12]


@pytest.mark.parametrize("references", [
    ALIAS + " <different@qq.com>", ALIAS + " <reply1@qq.com> garbage",
    ALIAS + " " + ALIAS + " <reply1@qq.com>",
    ALIAS + " <reply2@qq.com> <reply1@qq.com>",
])
async def test_unsafe_thread_relation_blocks_before_body(references):
    mailbox = Session({11: chain_mail(references=references)})
    with pytest.raises(InboundError):
        await reader(mailbox).fetch_inbound_page(ROUTE.mailbox_alias, cursor(), 20)
    assert mailbox.downloads == []


async def test_other_thread_or_sender_cannot_enter_continuous_conversation():
    for raw in (chain_mail(references="<other@qq.com> <reply1@qq.com>"),
        chain_mail(sender="111111111@qq.com")):
        mailbox = Session({11: raw})
        mailbox.search_result = (11,)
        page = await reader(mailbox).fetch_inbound_page(ROUTE.mailbox_alias, cursor(), 20)
        assert not page.items and not mailbox.downloads


async def test_thread_header_change_during_download_is_rejected():
    mailbox = Session({11: chain_mail()})
    mailbox.changed = chain_mail(references=ALIAS + " <extra@qq.com> <reply1@qq.com>")
    with pytest.raises(InboundError):
        await reader(mailbox).fetch_inbound_page(ROUTE.mailbox_alias, cursor(), 20)


async def test_explicit_reconciliation_has_same_saved_cursor_and_only_one_uid():
    from connectors.qq_mail.reconciliation import QQContinuousReconciliationReader
    mailbox = Session({10: mail(reply=ALIAS), 11: chain_mail(), 12: mail(reply=ALIAS)})
    saved = cursor(12)
    repair = QQContinuousReconciliationReader(reader(mailbox), uidvalidity=7, uid=11)
    page = await repair.fetch_inbound_page(ROUTE.mailbox_alias, saved, 20)
    assert page.starting_cursor == page.next_cursor == saved
    assert len(page.items) == 1 and mailbox.downloads == [11]
    for validity, uid in [(8,11),(7,13)]:
        wrong = QQContinuousReconciliationReader(reader(mailbox),uidvalidity=validity,uid=uid)
        with pytest.raises(InboundError):
            await wrong.fetch_inbound_page(ROUTE.mailbox_alias,saved,20)
    assert mailbox.downloads == [11]
