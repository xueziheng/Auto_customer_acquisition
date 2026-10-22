"""仅用本机 socketpair 的合成 IMAP 服务；无 DNS、TCP 或真实邮箱。"""
import socket
import threading
from contextlib import contextmanager

from connectors.qq_mail import inbound_transport as transport
from tests.unit.test_qq_inbound import TO, mail

MARKER = "SYNTHETIC-SECRET\r\nFORGED-LOG-LINE"
HEADER_QUERY = "BODY.PEEK[HEADER.FIELDS"


@contextmanager
def synthetic_imap(monkeypatch, *, fail=None, raw=None, trial_gate=None, bodystructure=None, body_override=None):
    raw = mail() if raw is None else raw
    commands, failures, workers = [], [], []

    def reject_network(*args, **kwargs):
        raise AssertionError("测试禁止真实网络连接")

    monkeypatch.setattr(socket, "create_connection", reject_network)

    def open_socket(_self, _timeout):
        client, server = socket.socketpair()
        client.settimeout(3)
        server.settimeout(3)

        def serve():
            try:
                with server, server.makefile("rwb", buffering=0) as stream:
                    stream.write(b"* OK synthetic-only\r\n")
                    while line := stream.readline():
                        tag, command = line.rstrip(b"\r\n").split(b" ", 1)
                        text = command.decode()
                        # 仅记录命令类型；LOGIN 的参数从不进入记录。
                        kind = text.split(" ", 1)[0]
                        commands.append(kind)
                        stage = {
                            "LOGIN": "authenticate", "EXAMINE": "select_readonly",
                            "STATUS": "uid_state",
                        }.get(kind)
                        if kind == "UID":
                            stage = "search" if text.startswith("UID SEARCH") else (
                                "headers" if HEADER_QUERY in text else "body")
                        if fail is not None and fail == stage:
                            stream.write(tag + b" NO SYNTHETIC-SECRET FORGED-LOG-LINE\r\n")
                            continue
                        if kind == "CAPABILITY":
                            stream.write(b"* CAPABILITY IMAP4rev1\r\n")
                        elif kind == "EXAMINE":
                            stream.write(b"* 1 EXISTS\r\n* OK [UIDVALIDITY 7]\r\n")
                        elif kind == "STATUS":
                            stream.write(b"* STATUS INBOX (UIDVALIDITY 7 UIDNEXT 2)\r\n")
                        elif text.startswith("UID SEARCH"):
                            assert " ALL" not in text
                            assert 'HEADER In-Reply-To "' in text
                            assert text.count("HEADER ") == 1
                            assert 'HEADER From "' not in text and 'HEADER To "' not in text
                            assert " UID 1:1 " in text
                            stream.write(b"* SEARCH 1\r\n")
                        elif text.startswith("UID FETCH"):
                            assert "BODY.PEEK[" in text and "UID FETCH 1 " in text
                            part = raw.split(b"\r\n\r\n")[0] + b"\r\n\r\n" if HEADER_QUERY in text else raw
                            if HEADER_QUERY in text:
                                # 按真实HEADER.FIELDS裁剪，不能替身多返回MIME/Bcc掩盖查询缺失。
                                import re
                                from email import policy
                                from email.parser import BytesHeaderParser
                                match = re.search(r"HEADER.FIELDS \(([^)]+)\)", text)
                                assert match is not None
                                requested = set(match[1].lower().split())
                                parsed = BytesHeaderParser(policy=policy.default).parsebytes(part)
                                part = ("\r\n".join(f"{k}: {v}" for k, v in parsed.raw_items()
                                    if k.lower() in requested) + "\r\n\r\n").encode()
                            structure = b""
                            if "BODYSTRUCTURE" in text:
                                from tests.unit.qq_mime_fixture import (
                                    structure_for,
                                    wire_value,
                                )
                                structure = b" BODYSTRUCTURE " + (bodystructure if bodystructure is not None else wire_value(structure_for(raw)))
                            label = (f"BODY[HEADER.FIELDS ({match[1]})]".encode()
                                     if HEADER_QUERY in text else b"BODY[]")
                            if HEADER_QUERY not in text and body_override is not None:
                                part = body_override
                            stream.write(b"* 1 FETCH (UID 1 RFC822.SIZE " + str(len(raw)).encode()
                                         + structure + b" " + label + b" {" + str(len(part)).encode()
                                         + b"}\r\n" + part + b")\r\n")
                        elif kind == "LOGOUT":
                            stream.write(b"* BYE synthetic\r\n" + tag + b" OK done\r\n")
                            break
                        elif kind != "LOGIN":
                            raise AssertionError("未许可合成命令")
                        stream.write(tag + b" OK done\r\n")
            except Exception as error:  # noqa: BLE001 测试线程必须把失败交给主线程
                failures.append(type(error).__name__)

        thread = threading.Thread(target=serve, daemon=True)
        workers.append((thread, client, server))
        thread.start()
        return client

    monkeypatch.setattr(transport._BoundedIMAP, "_create_socket", open_socket)

    class Secrets:
        def resolve(self, ref):
            assert ref == "fixture-only"
            return "synthetic-password"

    try:
        yield transport.QQImapInboundTransport(
            TO, Secrets(), "fixture-only", trial_gate=trial_gate), commands
    finally:
        for thread, client, server in workers:
            thread.join(4)
            client.close()
            server.close()
            assert not thread.is_alive()
        assert failures == []
