"""假 IMAP wire → 原头/结构/合成正文 → 原 Gateway/独立 PG UoW。"""

import hashlib
import re
from email import policy
from email.parser import BytesHeaderParser

import pytest
from sqlalchemy import func, select

from connectors.qq_mail.inbound_transport import _Session
from infra.db.qq_trial_tables import QQTrialEventRow
from infra.db.tables import EmailInboundReceiptRow, MessageRow
from shared.schemas.qq_trial import QQTrialStopped
from tests.integration.test_qq_trial_ledger import snapshot
from tests.integration.test_qq_trial_ledger import (
    trial as trial,  # noqa: PLC0414 - pytest fixture re-export
)
from tests.integration.test_qq_trial_pipeline import setup_pipeline
from tests.unit.qq_address_fixture import address_mail, bad_value, replace_field
from tests.unit.qq_mime_fixture import structure_for, wire_value
from tests.unit.test_qq_bounded_trial import ALIAS
from tests.unit.test_qq_inbound import FROM, TO


async def prepare(trial, change=None):
    gate, sessions, _, state, *_ = trial
    driver, store, qq, blobs, *_ = await setup_pipeline(trial)
    raw = address_mail().replace(ALIAS.encode(), gate.scope.provider_header.encode())
    if change in {"from_only", "to_only"}:
        name, address = (b"To", TO) if change == "from_only" else (b"From", FROM)
        raw = replace_field(raw, name, address.encode())
    elif change in {"bad_base64", "encoded_crlf", "wrong_party", "folded"}:
        case = "invalid_base64" if change == "bad_base64" else change
        raw = replace_field(raw, b"From", bad_value(case, FROM.encode()))
    elif change == "duplicate":
        raw = f"From: {FROM}\r\n".encode() + raw
    elif change == "bcc":
        raw = f"Bcc: {TO}\r\n".encode() + raw
    plan = structure_for(raw)
    if change == "attachment":
        plan[0][9] = [b"ATTACHMENT", None]
    elif change == "integrity":
        plan[0][6] += 1
    qq.messages[11] = raw
    qq.search_result = (11,)  # 包含搜索结果始终仍须由本地精确检验。
    body = raw
    if change == "body_label":
        body = raw.replace(b"WHh4eA==", b"QUJDRA==")
    elif change == "body_party":
        body = raw.replace(FROM.encode(), b"111111111@qq.com")
    elif change == "body_new_defect":
        body = raw.replace(b"WHh4eA==", b"WHh4eB==")
    elif change == "body_size":
        body = raw + b"EXTRA"
    wire_calls = []

    class Wire:
        def uid(self, command, uid, query):
            assert command == "FETCH" and uid == "11"
            wire_calls.append(query)
            if "HEADER.FIELDS" in query:
                requested = re.search(r"HEADER.FIELDS \(([^)]+)\)", query)[1]
                assert "BODYSTRUCTURE" in query and "BCC" in requested
                names = requested.lower().split()
                h = BytesHeaderParser(policy=policy.default).parsebytes(raw.split(b"\r\n\r\n")[0] + b"\r\n\r\n")
                part = ("\r\n".join(f"{k}: {v}" for k, v in h.raw_items() if k.lower() in names) + "\r\n\r\n").encode()
                label = f"BODY[HEADER.FIELDS ({requested})]".encode()
                structure = b" BODYSTRUCTURE " + wire_value(plan)
            else:
                assert query == "(UID BODY.PEEK[])"
                part, label, structure = body, b"BODY[]", b""
            meta = (b"1 (UID 11 RFC822.SIZE " + str(len(raw)).encode() + structure + b" " + label
                    + b" {" + str(len(part)).encode() + b"}")
            if change == "uid" and "HEADER.FIELDS" in query:
                meta = meta.replace(b"UID 11", b"UID 12")
            return "OK", [(meta, part), b")"]

    wire = _Session(Wire(), gate)

    async def headers(uid):
        qq.read_headers.append(uid)
        return await wire.headers(uid)

    async def download(uid, size):
        qq.downloads.append(uid)
        result = await wire.body(uid, size)
        if change == "revoked_after_body":
            state["mailbox"] = False
        return result

    qq.headers, qq.body = headers, download
    return gate, sessions, driver, store, qq, blobs, wire_calls


async def counts(sessions, tenant):
    async with sessions() as s:
        messages = await s.scalar(select(func.count()).select_from(MessageRow).where(
            MessageRow.tenant_id == tenant, MessageRow.direction == "inbound"))
        receipts = (await s.scalars(select(EmailInboundReceiptRow).where(
            EmailInboundReceiptRow.tenant_id == tenant))).all()
        kinds = (await s.scalars(select(QQTrialEventRow.kind).where(
            QQTrialEventRow.tenant_id == tenant))).all()
    return messages, receipts, kinds


@pytest.mark.parametrize("change,fields", [(None, ("from", "to")), ("from_only", ("from",)), ("to_only", ("to",))])
async def test_pipeline_preserves_raw_hash_defects_audit_and_exactly_one_receipt(trial, change, fields):
    gate, sessions, driver, store, qq, blobs, calls = await prepare(trial, change)
    original_audit = gate.record_address_compatibility

    async def repeat_observation(fields):
        await original_audit(fields)
        await original_audit(fields)

    gate.record_address_compatibility = repeat_observation
    assert await driver.scan_once() == 1
    after = await store.read_cursor()
    row = await snapshot(gate, sessions)
    assert row.state == "received" and row.bodies == row.headers == 1
    assert qq.downloads == [11] and len(calls) == 2
    assert list(blobs.values.values()) == [qq.messages[11]]
    n, receipts, kinds = await counts(sessions, gate.scope.route.tenant_id)
    assert n == len(receipts) == 1
    receipt = receipts[0]
    assert receipt.raw_hash == hashlib.sha256(qq.messages[11]).hexdigest()
    assert receipt.raw_size == len(qq.messages[11]) and receipt.raw_artifact_id
    for name in fields:
        assert kinds.count(f"address_{name}_quoted_utf8_b_v1") == 1
    assert sum(k.startswith("address_") for k in kinds) == len(fields)
    h = BytesHeaderParser(policy=policy.default).parsebytes(next(iter(blobs.values.values())))
    for name in fields:
        assert len(h[name].defects) == 2  # 未重写原件抹掉缺陷。
    opens = qq.opens
    assert await driver.scan_once() == 0 and qq.opens == opens
    assert (await store.read_cursor()).cursor == after.cursor
    assert (await snapshot(gate, sessions)).bodies == 1
    assert (await counts(sessions, gate.scope.route.tenant_id))[0] == 1


@pytest.mark.parametrize("change,reason,bodies", [
    ("bad_base64", "reply_headers_unsafe", 0),
    ("encoded_crlf", "reply_headers_unsafe", 0),
    ("wrong_party", "reply_headers_unsafe", 0),
    ("folded", "reply_headers_unsafe", 0),
    ("duplicate", "reply_headers_unsafe", 0),
    ("body_label", "reply_headers_unsafe", 1),
    ("body_party", "reply_headers_unsafe", 1),
    ("body_new_defect", "reply_headers_unsafe", 1),
    ("body_size", "mime_body_mismatch", 1),
    ("uid", "mime_structure_invalid", 0),
    ("bcc", "bcc_not_allowed", 0),
    ("attachment", "attachments_not_allowed", 0),
    ("integrity", "mime_body_mismatch", 1),
    ("revoked_after_body", "failed_or_unknown", 1),
])
async def test_pipeline_failure_persists_and_never_archives_commits_retries_or_refunds(trial, change, reason, bodies):
    gate, sessions, driver, store, qq, blobs, _ = await prepare(trial, change)
    before, old = await store.read_cursor(), await snapshot(gate, sessions)
    assert await driver.scan_once() == 0
    row = await snapshot(gate, sessions)
    assert row.state == "stopped" and row.reason == reason
    assert row.bodies == bodies and row.headers == 1 and row.watermark == old.watermark
    assert not row.message_id and not blobs.values
    assert (await store.read_cursor()).cursor == before.cursor
    opens = qq.opens
    assert await driver.scan_once() == 0 and qq.opens == opens
    after = await snapshot(gate, sessions)
    assert (after.bodies, after.headers, after.searches, after.sessions) == (
        row.bodies, row.headers, row.searches, row.sessions)
    n, receipts, kinds = await counts(sessions, gate.scope.route.tenant_id)
    assert n == 0 and not receipts and kinds.count(reason) == 1
    assert not any(k.startswith("address_") for k in kinds)


async def test_revoked_trial_stops_before_header_compatibility_or_body(trial):
    gate, sessions, driver, store, qq, blobs, calls = await prepare(trial)
    trial[3]["mailbox"] = False
    before = await store.read_cursor()
    assert await driver.scan_once() == 0
    row = await snapshot(gate, sessions)
    assert row.state == "stopped" and row.bodies == row.headers == 0
    assert not calls and not qq.downloads and not blobs.values
    assert (await store.read_cursor()).cursor == before.cursor


@pytest.mark.parametrize("fields", [(), ("from", "from"), ("to", "from"), ("other",)])
async def test_audit_cannot_record_unrecognized_defects_or_fields(trial, fields):
    gate, sessions, *_ = trial
    with pytest.raises(QQTrialStopped):
        await gate.record_address_compatibility(fields)
    assert not any(k.startswith("address_") for k in (await counts(sessions, gate.scope.route.tenant_id))[2])


async def test_audit_requires_current_claim_and_reserved_body(trial):
    gate, sessions, *_ = trial
    with pytest.raises(QQTrialStopped):
        await gate.record_address_compatibility(("from", "to"))
    assert await gate.claim_scan()
    with pytest.raises(QQTrialStopped):
        await gate.record_address_compatibility(("from", "to"))
    assert not any(k.startswith("address_") for k in (await counts(sessions, gate.scope.route.tenant_id))[2])
