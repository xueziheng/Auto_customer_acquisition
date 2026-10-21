"""标准库解析固定合成 MIME，模拟 IMAP 的结构返回；不复用被测解析器。"""

from email import policy
from email.parser import BytesParser


def structure_for(raw):
    def params(items):
        return [s.encode() for pair in items for s in pair] or None

    def walk(p):
        disposition = p.get_content_disposition()
        disp = ([disposition.upper().encode(), params(p.get_params([], header="content-disposition")[1:])]
                if disposition else None)
        parameters = params(p.get_params([], header="content-type")[1:])
        if p.is_multipart():
            return [*(walk(part) for part in p.iter_parts()), p.get_content_subtype().upper().encode(),
                    parameters, disp, None, None]
        body = p.get_payload().encode("ascii", "surrogateescape")
        result = [p.get_content_maintype().upper().encode(), p.get_content_subtype().upper().encode(),
                  parameters, None, None, str(p.get("Content-Transfer-Encoding", "7bit")).upper().encode(), len(body)]
        if p.get_content_maintype() == "text":
            result += [body.count(b"\r\n") + int(bool(body) and not body.endswith(b"\r\n"))]
        return [*result, None, disp, None, None]

    return walk(BytesParser(policy=policy.default).parsebytes(raw))


def wire_value(value):
    if value is None:
        return b"NIL"
    if isinstance(value, list):
        return b"(" + b" ".join(wire_value(v) for v in value) + b")"
    if isinstance(value, int):
        return str(value).encode()
    return b'"' + value.replace(b"\\", b"\\\\").replace(b'"', b'\\"') + b'"'
