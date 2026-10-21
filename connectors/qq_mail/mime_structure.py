"""有界 IMAP BODYSTRUCTURE 与原 MIME 一致性；仅允许单体文本或双文本 alternative。"""

import base64
import binascii
import quopri
import re
from dataclasses import dataclass
from email import policy
from email.parser import BytesHeaderParser

from shared.schemas.email_inbound import HEADER_BYTES
from shared.schemas.qq_trial import QQTrialStopped

type WireValue = list[WireValue] | bytes | str | int | None


def _invalid() -> QQTrialStopped:
    return QQTrialStopped("mime_structure_invalid")


class _Wire:
    """仅解析一个完整 FETCH；字符串和 literal 不作为协议 token 再解释。"""

    def __init__(self, raw: bytes, literal_limit: int = HEADER_BYTES):
        self.raw, self.pos, self.nodes = raw, 0, 0
        self.literal_limit = literal_limit

    def space(self) -> None:
        while self.pos < len(self.raw) and self.raw[self.pos] == 32:
            self.pos += 1

    def value(self, depth: int = 0) -> WireValue:
        self.nodes += 1
        if depth > 8 or self.nodes > 256:
            raise QQTrialStopped("mime_structure_limit")
        self.space()
        if self.pos >= len(self.raw):
            raise _invalid()
        char = self.raw[self.pos]
        self.pos += 1
        if char == 40:
            values: list[WireValue] = []
            while True:
                self.space()
                if self.pos >= len(self.raw):
                    raise _invalid()
                if self.raw[self.pos] == 41:
                    self.pos += 1
                    return values
                values.append(self.value(depth + 1))
                if (self.pos < len(self.raw) and self.raw[self.pos] not in (32, 41)
                        and not (isinstance(values[-1], list) and self.raw[self.pos] == 40)):
                    raise _invalid()
        if char == 34:
            quoted = bytearray()
            while self.pos < len(self.raw):
                char = self.raw[self.pos]
                self.pos += 1
                if char == 34:
                    return bytes(quoted)
                if char == 92:
                    if self.pos >= len(self.raw) or self.raw[self.pos] not in (34, 92):
                        raise _invalid()
                    char = self.raw[self.pos]
                    self.pos += 1
                if not 32 <= char <= 126:
                    raise _invalid()
                quoted.append(char)
            raise _invalid()
        if char == 123:
            match = re.match(rb"(0|[1-9][0-9]{0,5})}\r\n", self.raw[self.pos:])
            if match is None:
                raise _invalid()
            size = int(match[1])
            self.pos += match.end()
            if size > self.literal_limit or self.pos + size > len(self.raw):
                raise QQTrialStopped("mime_structure_limit")
            value = self.raw[self.pos:self.pos + size]
            self.pos += size
            return value
        self.pos -= 1
        # BODY[HEADER.FIELDS (...)] 是一个 FETCH 属性，不是 BODYSTRUCTURE 列表。
        if self.raw[self.pos:self.pos + 5].upper() == b"BODY[":
            end = self.raw.find(b"]", self.pos)
            if end < 0:
                raise _invalid()
            value = self.raw[self.pos:end + 1]
            self.pos = end + 1
            if any(c < 32 or c > 126 for c in value):
                raise _invalid()
            return value.decode("ascii").upper()
        match = re.match(rb"[^ (){}\r\n\x00-\x1f\x7f-\xff]+", self.raw[self.pos:])
        if match is None:
            raise _invalid()
        self.pos += match.end()
        atom = match[0]
        if re.fullmatch(rb"0|[1-9][0-9]{0,9}", atom):
            return int(atom)
        return None if atom.upper() == b"NIL" else atom.decode("ascii").upper()


def _fetch_attributes(result: object, limit: int) -> tuple[dict[str, WireValue], int]:
    """重组 imaplib 的 literal 片段；拒绝多响应、重复属性和未消费尾部。"""
    if not isinstance(result, list) or not 1 <= len(result) <= 32:
        raise _invalid()
    chunks: list[bytes] = []
    size = 0
    for item in result:
        if isinstance(item, tuple) and len(item) == 2 and all(isinstance(v, bytes) for v in item):
            chunk = item[0] + b"\r\n" + item[1]
        elif isinstance(item, bytes):
            chunk = item
        else:
            raise _invalid()
        size += len(chunk)
        if size > limit + HEADER_BYTES:
            raise QQTrialStopped("mime_structure_limit")
        chunks.append(chunk)
    wire = _Wire(b"".join(chunks), limit)
    seq = wire.value()
    if wire.pos >= len(wire.raw) or wire.raw[wire.pos] != 32:
        raise _invalid()
    attrs = wire.value()
    wire.space()
    if (type(seq) is not int or seq <= 0 or not isinstance(attrs, list)
            or len(attrs) % 2 or wire.pos != len(wire.raw)):
        raise _invalid()
    values: dict[str, WireValue] = {}
    for key, value in zip(attrs[::2], attrs[1::2], strict=True):
        if not isinstance(key, str) or key in values:
            raise _invalid()
        values[key] = value
    return values, size


def parse_header_fetch(result: object, uid: int, fields: str) -> tuple[int, bytes, WireValue]:
    values, size = _fetch_attributes(result, HEADER_BYTES)
    label = f"BODY[HEADER.FIELDS ({fields})]"
    if set(values) != {"UID", "RFC822.SIZE", "BODYSTRUCTURE", label}:
        raise _invalid()
    raw, total = values[label], values["RFC822.SIZE"]
    if (type(values["UID"]) is not int or values["UID"] != uid
            or type(total) is not int or not 0 < total <= 262144
            or not isinstance(raw, bytes) or not 0 < len(raw) <= HEADER_BYTES
            or size - len(raw) > HEADER_BYTES or not isinstance(values["BODYSTRUCTURE"], list)):
        raise _invalid()
    return total, raw, values["BODYSTRUCTURE"]


def parse_body_fetch(result: object, uid: int, size: int) -> bytes:
    """整封 literal 也必须带完整闭合响应、精确 UID 和大小。"""
    try:
        values, received = _fetch_attributes(result, size)
        raw = values.get("BODY[]")
        if (set(values) not in ({"UID", "BODY[]"}, {"UID", "BODY[]", "RFC822.SIZE"})
                or type(values.get("UID")) is not int or values["UID"] != uid
                or ("RFC822.SIZE" in values and values["RFC822.SIZE"] != size)
                or not isinstance(raw, bytes) or len(raw) != size or received - size > HEADER_BYTES):
            raise _invalid()
        return raw
    except QQTrialStopped:
        raise QQTrialStopped("mime_body_mismatch") from None


@dataclass(frozen=True, repr=False)
class MimePlan:
    kind: str
    parameters: tuple[tuple[str, str], ...]
    disposition: str | None
    encoding: str | None = None
    octets: int = 0
    lines: int = 0
    children: tuple["MimePlan", ...] = ()


def _text(value: WireValue) -> str:
    if not isinstance(value, bytes) or not value or len(value) > 998 or any(c < 32 or c > 126 for c in value):
        raise _invalid()
    return value.decode("ascii")


def _parameters(value: WireValue, *, multipart: bool) -> tuple[tuple[str, str], ...]:
    if value is None:
        value = []
    if not isinstance(value, list) or len(value) > 6 or len(value) % 2:
        raise _invalid()
    params: dict[str, str] = {}
    for name, item in zip(value[::2], value[1::2], strict=True):
        key, text = _text(name).lower(), _text(item)
        if key in params or key not in ({"boundary"} if multipart else {"charset"}):
            raise _invalid()
        params[key] = text if key == "boundary" else text.lower()
    if multipart:
        if set(params) != {"boundary"} or not re.fullmatch(r"[0-9A-Za-z'()+_,./:=? -]{0,69}[0-9A-Za-z'()+_,./:=?-]", params["boundary"]):
            raise _invalid()
    elif params.get("charset", "us-ascii") not in {"utf-8", "us-ascii", "gb18030", "gb2312", "gbk", "iso-8859-1"}:
        raise _invalid()
    return tuple(sorted(params.items()))


def _disposition(value: WireValue) -> str | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) != 2:
        raise _invalid()
    kind = _text(value[0]).lower()
    if kind == "attachment" or (kind == "inline" and value[1] is not None):
        raise QQTrialStopped("attachments_not_allowed")
    if kind != "inline":
        raise _invalid()
    return "inline"


def validate_structure(value: WireValue, total: int, depth: int = 0) -> MimePlan:
    """最多两层、根加两个叶子；不接受 mixed、嵌套 message 或未知扩展。"""
    if type(total) is not int or not 0 < total <= 262144:
        raise _invalid()
    if depth > 1:
        raise QQTrialStopped("mime_structure_limit")
    if not isinstance(value, list) or not value:
        raise _invalid()
    if isinstance(value[0], list):
        if (depth != 0 or not 4 <= len(value) <= 7 or not isinstance(value[1], list)
                or _text(value[2]).lower() != "alternative"):
            raise _invalid()
        children = tuple(validate_structure(child, total, depth + 1) for child in value[:2])
        if [p.kind for p in children] != ["text/plain", "text/html"] or sum(p.octets for p in children) > total:
            raise _invalid()
        if any(v is not None for v in value[5:]):
            raise _invalid()
        return MimePlan("multipart/alternative", _parameters(value[3], multipart=True),
                        _disposition(value[4] if len(value) > 4 else None), children=children)
    if (not 8 <= len(value) <= 12 or _text(value[0]).lower() != "text"
            or _text(value[1]).lower() not in {"plain", "html"}
            or value[3] is not None or value[4] is not None):
        raise _invalid()
    encoding = _text(value[5]).lower()
    octets, lines = value[6:8]
    if (encoding not in {"7bit", "8bit", "base64", "quoted-printable"}
            or type(octets) is not int or not 0 <= octets <= total
            or type(lines) is not int or not 0 <= lines <= octets
            or (len(value) > 8 and value[8] is not None)
            or any(v is not None for v in value[10:])):
        raise _invalid()
    return MimePlan("text/" + _text(value[1]).lower(), _parameters(value[2], multipart=False),
                    _disposition(value[9] if len(value) > 9 else None), encoding, octets, lines)


def match_headers(raw: bytes, plan: MimePlan) -> None:
    """与原头有效字段逐项核对；不靠重新序列化 MIME 掩盖差异。"""
    end = raw.find(b"\r\n\r\n")
    if end < 0 or end + 4 > HEADER_BYTES:
        raise _invalid()
    h = BytesHeaderParser(policy=policy.default).parsebytes(raw[:end + 4])
    names = ("Content-Type", "Content-Disposition", "Content-Transfer-Encoding", "MIME-Version")
    if (h.defects or any(len(h.get_all(n, [])) > 1 for n in names)
            or any(getattr(h[n], "defects", ()) for n in names if n in h)
            or any(h.get_all(n) for n in ("Bcc", "Content-ID", "Content-Description", "Content-MD5", "Content-Language", "Content-Location"))
            or str(h.get("MIME-Version", "1.0")) != "1.0"
            or re.search(r";\s*[^;= ]+\*", str(h.get("Content-Type", "")))):
        raise _invalid()
    params = h.get_params([], header="Content-Type")[1:]
    normalized = []
    for key, value in params:
        if not isinstance(value, str):
            raise _invalid()
        normalized.append((key.lower(), value if key.lower() == "boundary" else value.lower()))
    encoding = str(h.get("Content-Transfer-Encoding", "7bit")).lower()
    if (h.get_content_type() != plan.kind or tuple(sorted(normalized)) != plan.parameters
            or h.get_content_disposition() != plan.disposition or h.get_filename() is not None
            or (plan.children and encoding not in {"7bit", "8bit"})
            or (not plan.children and encoding != plan.encoding)):
        raise _invalid()


def verify_mime(raw: bytes, plan: MimePlan, total: int) -> None:
    """获准的一次 MIME 到达后核验原字节结构、编码、叶子字节/行数；不处理附件。"""
    try:
        if len(raw) != total:
            raise _invalid()
        match_headers(raw, plan)
        body = raw.split(b"\r\n\r\n", 1)[1]
        if plan.children:
            boundary = dict(plan.parameters)["boundary"].encode("ascii")
            matches = list(re.finditer(rb"(?:^|\r\n)--" + re.escape(boundary) + rb"(--)?\r\n", body))
            if (len(matches) != 3 or body[:matches[0].start()] not in {
                    b"", b"This is a multi-part message in MIME format.\r\n"}
                    or any(m[1] is not None for m in matches[:2]) or matches[2][1] != b"--"
                    or matches[-1].end() != len(body)):
                raise _invalid()
            for i, child in enumerate(plan.children):
                start, end = matches[i].end(), matches[i + 1].start()
                part = body[start:end]
                split = part.split(b"\r\n\r\n", 1)
                if len(split) != 2:
                    raise _invalid()
                # QQ may count the physical boundary CRLF in both octets and lines.
                # Use only those original bytes; recursive verification checks both.
                if child.octets == len(split[1]) + 2 and body[end:end + 2] == b"\r\n":
                    part = body[start:end + 2]
                verify_mime(part, child, len(part))
            return
        if len(body) != plan.octets or re.search(rb"(?<!\r)\n|\r(?!\n)", body):
            raise _invalid()
        lines = body.count(b"\r\n") + int(bool(body) and not body.endswith(b"\r\n"))
        if lines != plan.lines:
            raise _invalid()
        decoded = body
        if plan.encoding == "base64":
            decoded = base64.b64decode(body.replace(b"\r\n", b""), validate=True)
        elif plan.encoding == "quoted-printable":
            if re.search(rb"=(?![0-9A-Fa-f]{2}|\r\n)", body):
                raise _invalid()
            decoded = quopri.decodestring(body)
        elif plan.encoding == "7bit" and any(c > 127 for c in body):
            raise _invalid()
        decoded.decode(dict(plan.parameters).get("charset", "us-ascii"), errors="strict")
    except (QQTrialStopped, ValueError, UnicodeError, binascii.Error):
        raise QQTrialStopped("mime_body_mismatch") from None
