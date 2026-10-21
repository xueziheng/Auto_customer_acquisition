"""固定 QQ 准备 IO 子进程入口；不读配置文件、不写业务表、不输出原始错误。"""
from __future__ import annotations

import ctypes
import os
import signal
import socket
import sys
from time import monotonic
from typing import Any


def serve(fd: int, deadline: float, kind: str) -> None:
    # 先装 OS 截止计时，再导入 SDK；DNS/TLS/解释器线程均随进程终止。
    remaining = deadline - monotonic()
    if remaining <= 0:
        return
    original_parent = os.getppid()
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
        return
    if os.getppid() != original_parent or original_parent == 1:
        return
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.setitimer(signal.ITIMER_REAL, remaining)
    from artifact_store.transport import BlobObjectNotFoundError, BlobReadLimitExceeded
    from connectors._deadline_process import MAX_PACKET, pack, unpack
    handler: Any
    if kind == "sent":
        from connectors.qq_mail.sent_worker import SentWorker
        handler = SentWorker(deadline)
    elif kind == "objects":
        from connectors.object_store.qq_preparation_worker import ObjectWorker
        handler = ObjectWorker(deadline)
    else:
        return
    with socket.socket(fileno=fd) as channel:
        def receive(size: int) -> bytes:
            result = bytearray()
            while len(result) < size:
                chunk = channel.recv(size - len(result))
                if not chunk:
                    raise EOFError()
                result.extend(chunk)
            return bytes(result)
        while monotonic() < deadline:
            try:
                size = int.from_bytes(receive(4), "big")
                if not 0 < size <= MAX_PACKET:
                    return
                operation, args = unpack(receive(size))
                value = handler.call(operation, *args)
                if monotonic() >= deadline:
                    return
                channel.sendall(pack({"ok": True, "value": value}))
                if operation == "close":
                    return
            except EOFError:
                return
            except Exception as error:  # noqa: BLE001 - 私有 IPC 也不返回供应商/凭证异常文本。
                code = "not_found" if isinstance(error, BlobObjectNotFoundError) else (
                    "limit" if isinstance(error, BlobReadLimitExceeded) else "unknown")
                channel.sendall(pack({"ok": False, "code": code}))
                return


if __name__ == "__main__":
    serve(int(sys.argv[1]), float(sys.argv[2]), sys.argv[3])
