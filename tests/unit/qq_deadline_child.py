"""合成 IO 子进程：仅测试命令替换此入口，生产配置不能选择它。"""
import signal
import socket
import sys
import time
from pathlib import Path

# 导入任何生产模块之前也受同一 OS 截止时间约束。
signal.signal(signal.SIGALRM, signal.SIG_DFL)
signal.setitimer(signal.ITIMER_REAL, max(.001, float(sys.argv[2])-time.monotonic()))

from connectors._deadline_child import serve
from connectors.qq_mail.inbound_transport import _BoundedIMAP

port, mode = int(sys.argv[4]), sys.argv[5]


def block(stage):
    if len(sys.argv) > 6:
        Path(sys.argv[6]).write_text(stage)
    time.sleep(60)
    if len(sys.argv) > 6:
        Path(sys.argv[6]+".late").write_text(stage)


def synthetic_socket(connection, timeout):
    assert (connection.host, connection.port) == ("imap.qq.com", 993)
    if mode == "dns":
        original = socket.getaddrinfo
        def stalled_dns(*args, **kwargs):
            block("dns")
            return original(*args, **kwargs)
        socket.getaddrinfo = stalled_dns
    if mode == "tcp":
        original_socket = socket.socket
        class StalledSocket(original_socket):
            def connect(self, address):
                block("tcp")
                return super().connect(address)
        socket.socket = StalledSocket
    raw = socket.create_connection(("127.0.0.1", port), timeout)
    if mode in {"tls", "tls_verified"}:
        import ssl
        if mode == "tls_verified":
            return ssl.create_default_context(cafile=sys.argv[6]).wrap_socket(raw, server_hostname="imap.qq.com")
        if len(sys.argv) > 6:
            Path(sys.argv[6]).write_text("tls")
        return ssl.create_default_context().wrap_socket(raw, server_hostname="imap.qq.com")
    return raw


_BoundedIMAP._create_socket = synthetic_socket
if mode == "close":
    _BoundedIMAP.shutdown = lambda self: block("close")
if mode.startswith("object_"):
    from connectors.object_store.qq_preparation_worker import ObjectWorker
    original_call = ObjectWorker.call
    def object_call(self, operation, *args):
        if mode == "object_put_ack" and operation == "put":
            value = original_call(self, operation, *args)
            block(mode)
            return value
        if mode == "object_"+operation:
            block(mode)
        return original_call(self, operation, *args)
    ObjectWorker.call = object_call
serve(int(sys.argv[1]), float(sys.argv[2]), sys.argv[3])
