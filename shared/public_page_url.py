"""公开网页证据的纯 URL 形状规范化。

此处不解析 DNS；解析、防 DNS rebinding 与连接前复核属于 Gateway 的实际抓取边界。
共享纯函数只定义任何持久化公开网页证据都必须遵守的 canonical URL 形状。
"""

from __future__ import annotations

import ipaddress
from urllib.parse import SplitResult, urlsplit, urlunsplit

_IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def canonical_public_page_url(url: str) -> str:
    """返回公开 HTTP(S) 页面 URL 的唯一 canonical 形式。

    不安全 scheme、凭证、fragment、控制字符、非默认端口，以及私网、特殊用途或
    legacy-IP 字面量均拒绝。默认端口归一化为无端口，缺失 path 归一化为 ``/``。
    """

    if (
        not isinstance(url, str)
        or not 1 <= len(url) <= 2_048
        or url != url.strip()
        or _has_control(url)
    ):
        raise ValueError("invalid public page URL")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (ValueError, UnicodeError) as error:
        raise ValueError("invalid public page URL") from error
    hostname = parsed.hostname
    if (
        parsed.scheme not in {"http", "https"}
        or hostname is None
        or not hostname.isascii()
        or "%" in parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (parsed.scheme == "http" and port not in {None, 80})
        or (parsed.scheme == "https" and port not in {None, 443})
    ):
        raise ValueError("invalid public page URL")
    hostname = hostname.casefold()
    # DNS permits one terminal root label.  Persist the rootless form so it cannot
    # disguise an IPv4 literal (``127.0.0.1.``) as an ordinary DNS hostname.
    if hostname.endswith("."):
        hostname = hostname[:-1]
        if not hostname or hostname.endswith("."):
            raise ValueError("invalid public page URL")
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(
        ".local"
    ):
        raise ValueError("invalid public page URL")
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None and not is_public_unicast_ip(literal):
        raise ValueError("invalid public page URL")
    if literal is None and _is_legacy_ip_literal(hostname):
        raise ValueError("invalid public page URL")
    netloc = f"[{hostname}]" if ":" in hostname else hostname
    return urlunsplit(
        SplitResult(parsed.scheme, netloc, parsed.path or "/", parsed.query, "")
    )


def _is_legacy_ip_literal(hostname: str) -> bool:
    """拒绝 Python ``ipaddress`` 不接受但浏览器可能解释为 IP 的旧写法。"""

    labels = hostname.split(".")
    return 1 <= len(labels) <= 4 and all(
        _is_numeric_host_label(label) for label in labels
    )


def _is_numeric_host_label(label: str) -> bool:
    """识别浏览器/socket 仍可能按 IPv4 数字 component 解释的单段。"""

    folded = label.casefold()
    if folded.startswith("0x"):
        return len(folded) > 2 and all(
            character in "0123456789abcdef" for character in folded[2:]
        )
    return bool(folded) and folded.isdecimal()


def is_public_unicast_ip(value: str | _IPAddress) -> bool:
    """仅接受可公开路由的普通单播 IP；DNS 与实际对端复核复用此规则。"""

    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return bool(
        address.is_global
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_unspecified
        and not getattr(address, "is_site_local", False)
    )


def _has_control(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


__all__ = ("canonical_public_page_url", "is_public_unicast_ip")
