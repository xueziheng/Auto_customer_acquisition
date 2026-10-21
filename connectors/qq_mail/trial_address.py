"""仅受控 QQ trial 的已核验地址语法；显示名始终不作身份或授权依据。"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from email.errors import InvalidHeaderDefect
from email.headerregistry import AddressHeader, BaseHeader
from email.message import Message
from typing import cast

from shared.schemas.email_inbound import HEADER_BYTES

KNOWN_DEFECTS = (
    "encoded word inside quoted string",
    "missing trailing whitespace after encoded-word",
)
# 32 是显示字段的技术上限；不扩展至其他字符集、编码、空格或 Unicode 显示名。
_QUOTED_WORD = re.compile(
    rb'"=\?utf-8\?B\?([A-Za-z0-9+/]{2,44}={0,2})\?=" <([0-9]{5,12}@qq\.com)>'
)


@dataclass(frozen=True, repr=False)
class AddressEvidence:
    field: str
    raw_field: bytes
    defects: tuple[str, ...]


def raw_address_field(raw: bytes, name: str) -> bytes | None:
    """保留原字段大小写、空白、折行；正文不会进入比较材料。"""
    prefix, separator, _ = raw[:HEADER_BYTES + 4].partition(b"\r\n\r\n")
    if not separator:
        return None
    fields: list[bytes] = []
    for line in prefix.split(b"\r\n"):
        if line.startswith((b" ", b"\t")) and fields:
            fields[-1] += b"\r\n" + line
        else:
            fields.append(line)
    values = [line for line in fields if line.partition(b":")[0].lower() == name.encode("ascii").lower()]
    return values[0] if len(values) == 1 else None


def trial_address(headers: Message, raw: bytes, name: str, expected: str) -> AddressEvidence | None:
    """标准解析与严格原字节解析须一致；只把明确的两个 defect 作为受限例外。"""
    values = headers.get_all(name, [])
    if len(values) != 1 or not isinstance(values[0], AddressHeader):
        return None
    header = values[0]
    defects = cast(BaseHeader, header).defects
    original = raw_address_field(raw, name)
    if (original is None or len(header.addresses) != 1
            or len(header.groups) != 1 or header.groups[0].display_name is not None
            or not re.fullmatch(r"[0-9]{5,12}@qq\.com", expected)
            or header.addresses[0].addr_spec != expected):
        return None
    if not defects:
        # 未来标准库若不再报告此已知错误，也不能静默扩大本次例外。
        if re.search(rb'"[^"\r\n]*=\?', original):
            return None
        return AddressEvidence(name.lower(), original, ())
    if (len(defects) != 2
            or any(type(d) is not InvalidHeaderDefect for d in defects)
            or tuple(str(d) for d in defects) != KNOWN_DEFECTS):
        return None
    # 冒号后也仅准一个空格。原始折行/控制字符不经过 unfolding 或解码修正。
    value = original.partition(b":")[2]
    if not value.startswith(b" "):
        return None
    matched = _QUOTED_WORD.fullmatch(value[1:])
    if matched is None:
        return None
    encoded, mailbox = matched.groups()
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        return None
    if (base64.b64encode(decoded) != encoded
            or not re.fullmatch(rb"[A-Za-z0-9]{1,32}", decoded)
            or mailbox.decode("ascii") != expected
            or header.addresses[0].display_name != decoded.decode("ascii")):
        return None
    return AddressEvidence(name.lower(), original, KNOWN_DEFECTS)
