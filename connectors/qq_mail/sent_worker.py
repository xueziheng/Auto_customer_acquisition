"""Sent 单会话的同步协议；只能由有截止时间的受信 IO 子进程运行。"""
from time import monotonic

from connectors.qq_mail.sent_collect import _SentIMAP


class SentWorker:
    def __init__(self, deadline: float) -> None:
        self.deadline = deadline
        self.connection: _SentIMAP | None = None

    def call(self, operation: str, *args):
        if monotonic() >= self.deadline:
            raise TimeoutError()
        if operation == "open" and self.connection is None:
            self.connection = _SentIMAP(self.deadline - monotonic())
            return None
        connection = self.connection
        if connection is None:
            raise ValueError("missing Sent connection")
        if operation == "limits":
            connection.budget, connection.literal_limit = args
            return None
        if operation == "close":
            connection.shutdown()
            return None
        if operation not in {"login", "select", "status", "uid"}:
            raise ValueError("invalid Sent operation")
        connection.sock.settimeout(min(15, self.deadline - monotonic()))
        if operation == "select":
            if len(args) != 1:
                raise ValueError("invalid Sent folder")
            return connection.select(args[0], readonly=True)
        return getattr(connection, operation)(*args)
