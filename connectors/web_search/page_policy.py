"""有界公开页面限制判定；robots允许绝不等价于法律访问许可。"""

from __future__ import annotations

import html
import re
from urllib.parse import urlsplit

_ROBOT_AGENT = "tradeos-agent"
_MAX_ROBOTS_BYTES = 524_288


def _normalized_path(value: str) -> str | None:
    """RFC9309：只解码unreserved字节，保留%2F/%2A等与字面分隔符的区别。"""
    result: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character == "%":
            token = value[index + 1 : index + 3]
            if len(token) != 2 or re.fullmatch(r"[0-9A-Fa-f]{2}", token) is None:
                return None
            decoded = chr(int(token, 16))
            result.append(
                decoded
                if re.fullmatch(r"[A-Za-z0-9._~-]", decoded)
                else "%" + token.upper()
            )
            index += 3
            continue
        if ord(character) <= 32 or ord(character) == 127:
            return None
        if ord(character) > 127:
            try:
                result.extend(f"%{byte:02X}" for byte in character.encode("utf-8"))
            except UnicodeEncodeError:
                return None
        else:
            result.append(character)
        index += 1
    return "".join(result)


def is_restricted_page(body: bytes) -> bool:
    """只识别拦截标题/明确禁用文案，不因普通登录导航或表单CAPTCHA误拒。"""
    text = body.decode("utf-8", errors="replace")
    forms = re.findall(r"<form\b[^>]*>.*?</form>", text, re.IGNORECASE | re.DOTALL)
    if any(
        re.search(r"<input\b[^>]*\btype\s*=\s*['\"]?password\b", form, re.IGNORECASE)
        for form in forms
    ):
        outside = re.sub(
            r"<(head|title|script|style|nav|footer|form)\b[^>]*>.*?</\1>",
            " ",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        visible = html.unescape(re.sub(r"<[^>]*>", " ", outside))
        # 少量导航/标题并不构成可读的公开内容；不把有正文的嵌入登录框当整页墙。
        if len(visible.split()) < 12 and len(visible.strip()) < 80:
            return True
    headings = re.findall(
        r"<(?:title|h1)\b[^>]*>(.*?)</(?:title|h1)>", text, re.IGNORECASE | re.DOTALL
    )
    for heading in headings:
        visible = re.sub(r"<[^>]*>", " ", heading).strip()
        if re.search(
            r"^(?:access denied|forbidden|verify (?:that )?you are human|"
            r"security (?:check|verification)|just a moment|"
            r"(?:sign in|log in|login|authentication) required|"
            r"请先登录|访问被拒绝|请完成人机验证)[.!… ]*$",
            visible,
            re.IGNORECASE,
        ):
            return True
    return (
        re.search(
            r"(?:automated access|automated scraping|web scraping) is (?:prohibited|not permitted)"
            r"|(?:sign in|log in) to (?:view|access) (?:this|the) (?:page|content)"
            r"|禁止自动(?:抓取|访问)",
            re.sub(r"<[^>]*>", " ", text),
            re.IGNORECASE,
        )
        is not None
    )


def robots_allows(body: bytes, url: str) -> bool:
    """匹配本产品/通配分组、最长路径与allow同长优先；畸形/未支持限制关闭。"""
    if len(body) > _MAX_ROBOTS_BYTES:
        return False
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False
    if "<html" in text.casefold() or is_restricted_page(body):
        return False
    groups: list[tuple[list[str], list[tuple[str, str]]]] = []
    agents: list[str] = []
    rules: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        key, sep, value = line.partition(":")
        if not sep:
            return False
        key, value = key.strip().casefold(), value.strip()
        if key == "user-agent":
            if re.fullmatch(r"[A-Za-z_-]+|\*", value) is None:
                return False
            if rules:
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(value.casefold())
        elif key in {"allow", "disallow", "crawl-delay", "request-rate"}:
            if not agents:
                return False
            rules.append((key, value))
    if agents:
        groups.append((agents, rules))
    specific = [
        rules
        for agents, rules in groups
        if any(a != "*" and a in _ROBOT_AGENT for a in agents)
    ]
    selected = specific or [rules for agents, rules in groups if "*" in agents]
    parsed = urlsplit(url)
    path = _normalized_path(
        (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
    )
    if path is None:
        return False
    matches: list[tuple[int, bool]] = []
    for group in selected:
        for key, value in group:
            if key in {"crawl-delay", "request-rate"}:
                # 未实现跨Run站点速率状态时，不能假装已满足额外限制。
                return False
            if not value:
                continue
            pattern = _normalized_path(value)
            if pattern is None or not pattern.startswith("/"):
                return False
            end = pattern.endswith("$")
            if end:
                pattern = pattern[:-1]
            expression = "^" + ".*".join(re.escape(part) for part in pattern.split("*"))
            if re.search(expression + ("$" if end else ""), path):
                matches.append((len(pattern.replace("*", "").encode()), key == "allow"))
    return not matches or max(matches)[1]
