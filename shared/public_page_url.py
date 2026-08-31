"""公开网页证据的纯 URL 形状规范化。

此处不解析 DNS；解析、防 DNS rebinding 与连接前复核属于 Gateway 的实际抓取边界。
共享纯函数只定义任何持久化公开网页证据都必须遵守的 canonical URL 形状。
"""

from __future__ import annotations

import ipaddress
from urllib.parse import SplitResult, urlsplit, urlunsplit


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
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (parsed.scheme == "http" and port not in {None, 80})
        or (parsed.scheme == "https" and port not in {None, 443})
    ):
        raise ValueError("invalid public page URL")
    hostname = hostname.casefold()
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(
        ".local"
    ):
        raise ValueError("invalid public page URL")
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise ValueError("invalid public page URL")
    if literal is None and _is_legacy_ip_literal(hostname):
        raise ValueError("invalid public page URL")
    netloc = f"[{hostname}]" if ":" in hostname else hostname
    return urlunsplit(
        SplitResult(parsed.scheme, netloc, parsed.path or "/", parsed.query, "")
    )


def _is_legacy_ip_literal(hostname: str) -> bool:
    """拒绝 Python ``ipaddress`` 不接受但浏览器可能解释为 IP 的旧写法。"""

    folded = hostname.casefold()
    if folded.startswith("0x") and len(folded) > 2:
        return all(character in "0123456789abcdef" for character in folded[2:])
    labels = hostname.split(".")
    return bool(labels) and all(label.isdecimal() for label in labels)


def _has_control(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


__all__ = ("canonical_public_page_url",)
