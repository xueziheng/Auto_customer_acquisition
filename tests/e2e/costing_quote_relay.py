"""仅T10测试的固定HTTP传输：不读环境、不初始化数据库、不跟随重定向。"""

import base64
import binascii
import http.client
import json
import re
import signal
import sys
from urllib.parse import unquote, urlsplit

MAX_BYTES = 1048576
MAX_ENVELOPE = 1572864
TIMEOUT_SECONDS = 15
METHODS = frozenset({"GET", "HEAD", "POST", "OPTIONS"})
HOP_HEADERS = frozenset({"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
                         "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length"})


def clean_headers(headers):
    """移除hop-by-hop及Connection命名的头，长度/Host由固定目标重建。"""
    if not isinstance(headers, list) or len(headers) > 100:
        raise ValueError("invalid_headers")
    size = 0
    excluded = set(HOP_HEADERS)
    for pair in headers:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ValueError("invalid_headers")
        key, value = pair
        if (not isinstance(key, str) or not isinstance(value, str)
            or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key)
            or any(ord(c) < 32 or ord(c) > 126 for c in value)):
            raise ValueError("invalid_headers")
        size += len(key) + len(value)
        if key.lower() == "connection":
            excluded.update(part.strip().lower() for part in value.split(","))
    if size > 65536:
        raise ValueError("headers_too_large")
    return [(key, value) for key, value in headers if key.lower() not in excluded]


def valid_path(path):
    if not isinstance(path, str) or len(path) > 8192 or not path.startswith("/"):
        raise ValueError("invalid_path")
    parsed = urlsplit(path)
    decoded = unquote(path)
    if (parsed.scheme or parsed.netloc or parsed.fragment or decoded.startswith("//")
        or "\\" in decoded or any(ord(char) < 33 or ord(char) > 126 for char in decoded)
        or ".." in unquote(parsed.path).split("/")):
        raise ValueError("invalid_path")
    return path


def decode_body(value):
    if not isinstance(value, str) or len(value) > (MAX_BYTES + 2) // 3 * 4:
        raise ValueError("body_too_large")
    try:
        body = base64.b64decode(value, validate=True)
    except binascii.Error:
        raise ValueError("invalid_body") from None
    if len(body) > MAX_BYTES:
        raise ValueError("body_too_large")
    return body


def request_bytes(method, path, headers, body):
    if method not in METHODS:
        raise ValueError("invalid_method")
    valid_path(path)
    clean_headers(headers)
    if not isinstance(body, bytes) or len(body) > MAX_BYTES:
        raise ValueError("body_too_large")
    data = json.dumps({"method": method, "path": path, "headers": headers,
                       "body": base64.b64encode(body).decode("ascii")}).encode("ascii")
    if len(data) > MAX_ENVELOPE:
        raise ValueError("envelope_too_large")
    return data


def relay_request(data):
    """目标不可由输入控制，Host覆盖无效；真实非2xx状态也原样返回。"""
    if len(data) > MAX_ENVELOPE:
        raise ValueError("envelope_too_large")
    value = json.loads(data)
    if not isinstance(value, dict) or set(value) != {"method", "path", "headers", "body"}:
        raise ValueError("invalid_envelope")
    method, path = value["method"], valid_path(value["path"])
    if method not in METHODS:
        raise ValueError("invalid_method")
    headers, body = clean_headers(value["headers"]), decode_body(value["body"])
    connection = http.client.HTTPConnection("127.0.0.1", 8000, timeout=TIMEOUT_SECONDS)
    try:
        connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for key, item in headers:
            connection.putheader(key, item)
        connection.putheader("Host", "127.0.0.1:8000")
        connection.putheader("Connection", "close")
        connection.putheader("Content-Length", str(len(body)))
        connection.endheaders(body)
        response = connection.getresponse()
        content = response.read(MAX_BYTES + 1)
        if len(content) > MAX_BYTES:
            raise ValueError("body_too_large")
        return json.dumps({"status": response.status, "headers": clean_headers(response.getheaders()),
                           "body": base64.b64encode(content).decode("ascii")}).encode("ascii")
    finally:
        connection.close()


def relay_main():
    """stdin/stdout只承载受控传输，无日志；alarm同时约束阻塞stdin与HTTP。"""
    signal.alarm(TIMEOUT_SECONDS)
    try:
        data = sys.stdin.buffer.read(MAX_ENVELOPE + 1)
        result = relay_request(data)
        sys.stdout.buffer.write(result)
        sys.stdout.buffer.flush()
        return 0
    except Exception:  # noqa: BLE001 - 错误不输出请求/响应/环境，host明确映射502
        return 2
    finally:
        signal.alarm(0)
