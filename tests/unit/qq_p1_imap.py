"""P1 独立合成邮箱：socketpair 承载原 imaplib，绝不连接 QQ。"""
import re
import socket
import ssl
import threading
from contextlib import contextmanager
from email import policy
from email.parser import BytesHeaderParser

from connectors._deadline_process import DeadlineProcess
from connectors.qq_mail import inbound_transport
from tests.unit.qq_mime_fixture import structure_for, wire_value


@contextmanager
def p1_mailbox(monkeypatch, sent_headers: bytes, *, allow_disconnect: bool = False):
    state = {"sent":sent_headers,"reply":None,"sent_search":"1","commands":[],"failures":[],"processes":[],"slow":None}
    workers = []

    def start_peer(server, client=None):
        server.settimeout(3)

        def serve():
            folder = None
            try:
                with server, server.makefile("rwb",buffering=0) as stream:
                    if state["slow"] == "tls":
                        while server.recv(4096):
                            pass
                        return
                    if state["slow"] == "greeting":
                        import time
                        for byte in b"* OK synthetic-p1-mailbox\r\n":
                            stream.write(bytes([byte]))
                            time.sleep(.25)
                    else:
                        stream.write(b"* OK synthetic-p1-mailbox\r\n")
                    while line := stream.readline():
                        tag,command = line.rstrip(b"\r\n").split(b" ",1)
                        text = command.decode()
                        kind = text.split(" ",1)[0]
                        state["commands"].append((folder,"LOGIN" if kind=="LOGIN" else text))
                        if kind=="CAPABILITY":
                            stream.write(b"* CAPABILITY IMAP4rev1\r\n")
                        elif kind=="LOGIN":
                            assert "synthetic-p1-password" in text
                        elif kind=="EXAMINE":
                            folder = "sent" if '"Sent"' in text else "inbox"
                            assert folder=="sent" or text=="EXAMINE INBOX"
                            stream.write(b"* 1 EXISTS\r\n* OK [UIDVALIDITY 7]\r\n")
                        elif kind=="STATUS":
                            next_uid = 2 if folder=="sent" or state["reply"] is not None else 1
                            stream.write(f'* STATUS {"Sent" if folder=="sent" else "INBOX"} (UIDVALIDITY 7 UIDNEXT {next_uid})\r\n'.encode())
                        elif text.startswith("UID SEARCH"):
                            assert " ALL" not in text and " UID 1:1 " in text
                            if folder=="sent":
                                assert "HEADER X-TradeOS-Idempotency-V1" in text
                                result = state["sent_search"]
                            else:
                                assert 'HEADER In-Reply-To "' in text
                                result = "1" if state["reply"] is not None else ""
                            if state["slow"] == "eof":
                                stream.write(b"* SEARCH")
                                return
                            if state["slow"] in {"line", "missing_newline"}:
                                import time
                                for byte in b"* SEARCH 1":
                                    stream.write(bytes([byte]))
                                    time.sleep(.25)
                            else:
                                stream.write(f"* SEARCH {result}\r\n".encode())
                        elif text.startswith("UID FETCH"):
                            assert "UID FETCH 1 " in text and "BODY.PEEK[" in text
                            raw = state["sent"] if folder=="sent" else state["reply"]
                            assert isinstance(raw,bytes)
                            match = re.search(r"HEADER.FIELDS \(([^)]+)\)",text)
                            if match:
                                names = set(match[1].lower().split())
                                head = BytesHeaderParser(policy=policy.default).parsebytes(raw)
                                part = ("\r\n".join(f"{k}: {v}" for k,v in head.raw_items()
                                    if k.lower() in names)+"\r\n\r\n").encode()
                                label = f"BODY[HEADER.FIELDS ({match[1]})]".encode()
                            else:
                                assert folder=="inbox"
                                part,label = raw,b"BODY[]"
                            structure = b" BODYSTRUCTURE "+wire_value(structure_for(raw)) if "BODYSTRUCTURE" in text else b""
                            prefix=b"* 1 FETCH (UID 1 RFC822.SIZE "+str(len(raw)).encode()+structure+b" "+label+b" {"+str(len(part)).encode()+b"}\r\n"
                            if state["slow"] == "literal":
                                import time
                                stream.write(prefix)
                                for byte in part:
                                    stream.write(bytes([byte]))
                                    time.sleep(.25)
                                stream.write(b")\r\n")
                            else:
                                response=prefix+part+b")\r\n"
                                if state["slow"]=="fragment":
                                    for byte in response:
                                        stream.write(bytes([byte]))
                                else:
                                    stream.write(response)
                        elif kind=="LOGOUT":
                            stream.write(b"* BYE synthetic\r\n"+tag+b" OK done\r\n")
                            break
                        else:
                            raise AssertionError("合成邮箱收到非许可命令")
                        stream.write(tag+b" OK done\r\n")
            except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
                if not allow_disconnect and state["slow"] is None:
                    state["failures"].append("unexpected_peer_disconnect")
            except Exception as error:  # noqa: BLE001 - 合成服务线程把异常类型交给主测试
                state["failures"].append(type(error).__name__)

        thread=threading.Thread(target=serve,daemon=True)
        workers.append((thread,client,server))
        thread.start()

    def open_socket(connection, timeout):
        assert (connection.host,connection.port) == ("imap.qq.com",993)
        client,server = socket.socketpair()
        client.settimeout(3)
        start_peer(server,client)
        return client

    listener=socket.socket()
    listener.bind(("127.0.0.1",0))
    listener.listen()
    listener.settimeout(.1)
    stopped=threading.Event()
    def accept():
        while not stopped.is_set():
            try:
                server,_=listener.accept()
                if state.get("tls_context") is not None:
                    server=state["tls_context"].wrap_socket(server,server_side=True)
                start_peer(server)
            except TimeoutError:
                pass
    acceptor=threading.Thread(target=accept,daemon=True)
    acceptor.start()
    original_command=DeadlineProcess._command
    original_start=DeadlineProcess._start
    def command(self,fd):
        if self.kind=="sent":
            import sys
            args=[sys.executable,"-m","tests.unit.qq_deadline_child",str(fd),
                str(self.deadline.at),self.kind,str(listener.getsockname()[1])]
            return args+(["tls_verified",str(state["tls_cert"])] if state.get("tls_cert") else ["plain"])
        return original_command(self,fd)
    def start(self):
        original_start(self)
        state["processes"].append(self.process)
    monkeypatch.setattr(DeadlineProcess,"_command",command)
    monkeypatch.setattr(DeadlineProcess,"_start",start)
    monkeypatch.setattr(inbound_transport._BoundedIMAP,"_create_socket",open_socket)
    try:
        yield state
    finally:
        stopped.set()
        acceptor.join(1)
        listener.close()
        assert not acceptor.is_alive()
        for thread,client,server in workers:
            thread.join(4)
            if client is not None:
                client.close()
            server.close()
            assert not thread.is_alive()
        assert all(p.poll() is not None for p in state["processes"])
        assert state["failures"]==[], state["failures"]
