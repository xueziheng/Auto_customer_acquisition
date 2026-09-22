"""受控Python进程的纵深防线；不是操作系统沙箱。"""

from __future__ import annotations

import socket
import sys

from .config import ControlledError


def install_network_boundary(
    *, destinations: frozenset[int], listeners: frozenset[int]
) -> None:
    """只接受本次loopback端口；DNS解析与所有未知连接在socket前失败。"""

    def audit(event: str, arguments: tuple[object, ...]) -> None:
        if event == "socket.getaddrinfo":
            if (
                arguments[0] != "127.0.0.1"
                or arguments[1] not in destinations | listeners
            ):
                raise ControlledError("external_address_rejected")
        elif event in {"socket.connect", "socket.bind", "socket.sendto"}:
            sock, address = arguments[0], arguments[-1]
            if isinstance(sock, socket.socket) and sock.family == socket.AF_UNIX:
                raise ControlledError("external_address_rejected")
            allowed = listeners if event == "socket.bind" else destinations
            if (
                not isinstance(address, tuple)
                or address[:1] != ("127.0.0.1",)
                or address[1] not in allowed
            ):
                raise ControlledError("external_address_rejected")

    sys.addaudithook(audit)
