"""受控Python进程的纵深防线；不是操作系统沙箱。"""

from __future__ import annotations

import ipaddress
import socket
import sys

from .config import ControlledError


def resolve_external_destinations(
    hosts: frozenset[tuple[str, int]],
) -> frozenset[tuple[str, int]]:
    """在安装审计钩子前解析固定host；返回规范IPv4/IPv6端点。"""
    result: set[tuple[str, int]] = set()
    for host, port in hosts:
        if (
            not isinstance(host, str)
            or not host
            or host != host.strip().casefold()
            or type(port) is not int
            or not 1 <= port <= 65535
        ):
            raise ControlledError("external_address_rejected")
        try:
            records = socket.getaddrinfo(
                host, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM
            )
        except OSError:
            raise ControlledError("external_address_rejected") from None
        for family, socktype, protocol, canonical, address in records:
            del socktype, protocol, canonical
            if family not in {socket.AF_INET, socket.AF_INET6}:
                continue
            try:
                resolved = str(ipaddress.ip_address(address[0]))
            except ValueError:
                raise ControlledError("external_address_rejected") from None
            result.add((resolved, port))
    if hosts and not result:
        raise ControlledError("external_address_rejected")
    return frozenset(result)


def install_network_boundary(
    *,
    destinations: frozenset[int],
    listeners: frozenset[int],
    external_hosts: frozenset[tuple[str, int]] = frozenset(),
    external_destinations: frozenset[tuple[str, int]] = frozenset(),
) -> None:
    """只接受本次loopback端口和启动前解析的精确外部host/IP端点。"""

    def audit(event: str, arguments: tuple[object, ...]) -> None:
        if event == "socket.getaddrinfo":
            requested = (arguments[0], arguments[1])
            loopback = (
                arguments[0] == "127.0.0.1"
                and arguments[1] in destinations | listeners
            )
            if not loopback and requested not in external_hosts:
                raise ControlledError("external_address_rejected")
        elif event in {"socket.connect", "socket.bind", "socket.sendto"}:
            sock, address = arguments[0], arguments[-1]
            if isinstance(sock, socket.socket) and sock.family == socket.AF_UNIX:
                raise ControlledError("external_address_rejected")
            allowed = listeners if event == "socket.bind" else destinations
            loopback = (
                isinstance(address, tuple)
                and address[:1] == ("127.0.0.1",)
                and address[1] in allowed
            )
            external = (
                event != "socket.bind"
                and isinstance(address, tuple)
                and len(address) >= 2
                and address[:2] in external_destinations
            )
            if not loopback and not external:
                raise ControlledError("external_address_rejected")

    sys.addaudithook(audit)
