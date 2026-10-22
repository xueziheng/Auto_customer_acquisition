"""合成socketpair走真实imaplib/QQ只读传输，验证首信、大件占位和固定尾部。"""

import hashlib
import re
import socket
import threading
from contextlib import contextmanager

import pytest

from connectors.qq_mail import inbound_transport
from connectors.qq_mail.business_inbound import QQBusinessInboundConnector
from shared.schemas.email_inbound import MIME_BYTES

RAW = b"From: sales@example.com\r\nTo: 1249583590@qq.com\r\nMessage-ID: <first@example.com>\r\nSubject: Inquiry\r\nDate: Sun, 20 Oct 2099 12:00:00 +0000\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nPlease quote hinges."


@contextmanager
def ordinary_imap(
    monkeypatch,
    *,
    oversize=False,
    growing=False,
    fail_body=False,
    actual_raw=RAW,
    declared_size=None,
    body_uid=None,
    body_tail=b")",
    extra_fetch=False,
    literal_delta=0,
    ignore_partial=False,
):
    commands = []
    errors = []
    sockets = []

    def no_network(*args, **kwargs):
        raise AssertionError("不允许真实网络")

    monkeypatch.setattr(socket, "create_connection", no_network)

    def open_socket(_self, _timeout):
        client, server = socket.socketpair()
        sockets.append((client, server))
        client.settimeout(3)
        server.settimeout(3)

        def serve():
            try:
                with server, server.makefile("rwb", buffering=0) as stream:
                    server.sendall(b"* OK synthetic\r\n")
                    snapshots = 0
                    while line := stream.readline():
                        tag, command = line.rstrip(b"\r\n").split(b" ", 1)
                        text = command.decode()
                        if not text.startswith("LOGIN"):
                            commands.append(text)
                        kind = text.split(" ", 1)[0]
                        if kind == "CAPABILITY":
                            server.sendall(b"* CAPABILITY IMAP4rev1\r\n")
                        elif kind == "LOGIN":
                            pass
                        elif kind == "EXAMINE":
                            server.sendall(b"* 2 EXISTS\r\n* OK [UIDVALIDITY 7]\r\n")
                        elif kind == "STATUS":
                            snapshots += 1
                            end = 10 if growing and snapshots > 1 else 3
                            server.sendall(
                                f"* STATUS INBOX (UIDVALIDITY 7 UIDNEXT {end})\r\n".encode()
                            )
                        elif text.startswith("UID SEARCH"):
                            match = re.fullmatch(
                                r"UID SEARCH UID ([0-9]+):([0-9]+)", text
                            )
                            assert match
                            lo, hi = map(int, match.groups())
                            found = " ".join(
                                str(uid) for uid in (1, 2) if lo <= uid <= hi
                            )
                            server.sendall(("* SEARCH " + found + "\r\n").encode())
                        elif text.startswith("UID FETCH"):
                            uid = int(text.split(" ")[2])
                            header = "BODY.PEEK[HEADER]" in text
                            if fail_body and not header:
                                server.sendall(tag + b" NO synthetic failure\r\n")
                                continue
                            part = (
                                actual_raw.split(b"\r\n\r\n")[0] + b"\r\n\r\n"
                                if header
                                else actual_raw
                                if ignore_partial
                                else actual_raw[:MIME_BYTES]
                            )
                            size = (
                                MIME_BYTES + 1
                                if oversize and uid == 1
                                else (
                                    len(actual_raw)
                                    if declared_size is None
                                    else declared_size
                                )
                            )
                            partial = "BODY.PEEK[]<0." in text
                            label = (
                                b"BODY[HEADER]"
                                if header
                                else (b"BODY[]<0>" if partial else b"BODY[]")
                            )
                            response_uid = (
                                uid if header or body_uid is None else body_uid
                            )
                            meta = f'* {uid} FETCH (UID {response_uid} RFC822.SIZE {size} INTERNALDATE "21-Oct-2026 12:34:56 +0800" '.encode()
                            server.sendall(
                                meta
                                + label
                                + b" {"
                                + str(
                                    len(part) + (0 if header else literal_delta)
                                ).encode()
                                + b"}\r\n"
                                + part
                                + (b")" if header else body_tail)
                                + b"\r\n"
                            )
                            if extra_fetch and not header:
                                server.sendall(b"* 3 FETCH (UID 3 FLAGS ())\r\n")
                        elif kind == "LOGOUT":
                            server.sendall(b"* BYE done\r\n" + tag + b" OK logout\r\n")
                            break
                        else:
                            raise AssertionError("未允许的IMAP命令")
                        server.sendall(tag + b" OK done\r\n")
            except Exception as exc:  # noqa: BLE001 -- 只报告安全异常类型
                if not (ignore_partial and isinstance(exc, OSError)):
                    errors.append(type(exc).__name__)

        worker = threading.Thread(target=serve, daemon=True)
        worker.start()
        sockets.append(worker)
        return client

    monkeypatch.setattr(inbound_transport._BoundedIMAP, "_create_socket", open_socket)

    class Resolver:
        def resolve(self, ref):
            assert ref == "fixture-only"
            return "synthetic-password"

    try:
        yield (
            inbound_transport.QQImapInboundTransport(
                "1249583590@qq.com", Resolver(), "fixture-only"
            ),
            commands,
        )
    finally:
        for item in sockets:
            if isinstance(item, threading.Thread):
                item.join(4)
                assert not item.is_alive()
            else:
                item[0].close()
                item[1].close()
        assert errors == []


async def test_first_non_reply_and_oversize_do_not_block_next_uid(monkeypatch):
    with ordinary_imap(monkeypatch, oversize=True) as (transport, commands):
        page = await QQBusinessInboundConnector(
            transport, initial_uid_window=500, page_size=20
        ).fetch(uidvalidity=None, after_uid=0)
    assert page.next_uid == 2 and len(page.messages) == 2
    assert (
        page.messages[0].source_kind == "headers_only"
        and page.messages[0].parse_failure == "oversize"
    )
    assert (
        page.messages[1].source_kind == "full_mime" and page.messages[1].raw_mime == RAW
    )
    assert page.messages[1].received_at.year == 2026
    assert not any("In-Reply-To" in command for command in commands)
    assert not any(
        command.startswith(("SELECT", "STORE", "APPEND", "DELETE", "EXPUNGE"))
        for command in commands
    )
    assert len([c for c in commands if "BODY.PEEK[]" in c]) == 1


async def test_explicit_tail_stays_fixed_when_new_messages_arrive(monkeypatch):
    with ordinary_imap(monkeypatch, growing=True) as (transport, commands):
        page = await QQBusinessInboundConnector(
            transport, initial_uid_window=500, page_size=20
        ).fetch(uidvalidity=7, after_uid=1, before_uid=2)
    assert page.snapshot_end_uid == 2 and page.next_uid == 2
    assert "UID SEARCH UID 2:2" in commands


async def test_body_failure_never_returns_successful_page(monkeypatch):
    from shared.schemas.email_inbound import InboundError

    with (
        ordinary_imap(monkeypatch, fail_body=True) as (transport, _),
        pytest.raises(InboundError),
    ):
        await QQBusinessInboundConnector(transport).fetch(uidvalidity=None, after_uid=0)


@pytest.mark.parametrize("delta", [-17, 23])
async def test_declared_size_mismatch_keeps_actual_source_for_review(
    monkeypatch, delta
):
    with ordinary_imap(monkeypatch, declared_size=len(RAW) + delta) as (
        transport,
        commands,
    ):
        page = await QQBusinessInboundConnector(transport).fetch(
            uidvalidity=None, after_uid=0
        )
    assert page.next_uid == 2 and len(page.messages) == 2
    for message in page.messages:
        assert message.parse_failure == "size_mismatch"
        assert message.source_kind == "full_mime"
        assert message.raw_mime == RAW
        assert hashlib.sha256(message.raw_mime).digest() == hashlib.sha256(RAW).digest()
    assert all(
        f"BODY.PEEK[]<0.{MIME_BYTES}>" in c for c in commands if "BODY.PEEK[]" in c
    )


@pytest.mark.parametrize(
    "options",
    [
        {"body_uid": 99},
        {"body_tail": b") trailing"},
        {"body_tail": b""},
        {"extra_fetch": True},
        {"literal_delta": 1},
    ],
)
async def test_body_protocol_mismatch_never_returns_a_page(monkeypatch, options):
    from shared.schemas.email_inbound import InboundError

    with (
        ordinary_imap(monkeypatch, **options) as (transport, _),
        pytest.raises(InboundError),
    ):
        await QQBusinessInboundConnector(transport).fetch(uidvalidity=None, after_uid=0)


@pytest.mark.parametrize("length", [MIME_BYTES - 1, MIME_BYTES, MIME_BYTES + 91])
async def test_actual_body_bound_is_independent_of_declared_size(monkeypatch, length):
    raw = RAW + b" " * (length - len(RAW))
    with ordinary_imap(monkeypatch, actual_raw=raw, declared_size=len(RAW)) as (
        transport,
        _,
    ):
        page = await QQBusinessInboundConnector(transport).fetch(
            uidvalidity=None, after_uid=0
        )
    assert page.next_uid == 2 and len(page.messages) == 2
    for message in page.messages:
        if length < MIME_BYTES:
            assert message.source_kind == "full_mime"
            assert message.raw_mime == raw
            assert message.parse_failure == "size_mismatch"
        else:
            assert message.source_kind == "headers_only"
            assert message.parse_failure == "oversize"
            assert message.raw_mime == raw.split(b"\r\n\r\n")[0] + b"\r\n\r\n"


async def test_server_ignoring_partial_bound_is_rejected_before_literal_read(
    monkeypatch,
):
    import imaplib

    from shared.schemas.email_inbound import InboundError

    reads = []
    original_read = imaplib.IMAP4.read

    def observed_read(connection, length):
        reads.append(length)
        return original_read(connection, length)

    monkeypatch.setattr(imaplib.IMAP4, "read", observed_read)
    raw = RAW + b" " * (MIME_BYTES + 1 - len(RAW))
    with (
        ordinary_imap(
            monkeypatch, actual_raw=raw, declared_size=len(RAW), ignore_partial=True
        ) as (transport, _),
        pytest.raises(InboundError),
    ):
        await QQBusinessInboundConnector(transport).fetch(uidvalidity=None, after_uid=0)
    assert reads and max(reads) <= MIME_BYTES
