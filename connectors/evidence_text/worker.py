"""一次性parse协议入口；资源限制前仅导入标准库。"""

from __future__ import annotations

import importlib.metadata
import json
import platform
import struct
import sys
from typing import BinaryIO

PROTOCOL = "evidence-worker-v1"
CONTROL_MAXIMUM = 16384
LIMIT_FIELDS = frozenset(
    {
        "maximum_input_bytes",
        "maximum_pages",
        "maximum_text_bytes",
        "maximum_excerpt_bytes",
        "cpu_seconds",
        "address_space_bytes",
        "wall_timeout_ms",
        "maximum_result_bytes",
        "maximum_concurrency",
        "queue_timeout_ms",
        "termination_grace_ms",
    }
)


def read_exact(stream: BinaryIO, size: int) -> bytes:
    """有限读取；短读拒绝，不等待任意EOF。"""
    content = bytearray()
    while len(content) < size:
        piece = stream.read(min(65536, size - len(content)))
        if not piece:
            raise ValueError("协议短读")
        content.extend(piece)
    return bytes(content)


def strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """JSON重复字段不允许覆盖受限控制参数。"""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("协议重复字段")
        result[key] = value
    return result


def read_control() -> dict:
    """长度头的上限是固定协议尺寸，不是部署资源默认值。"""
    size = struct.unpack("!I", read_exact(sys.stdin.buffer, 4))[0]
    if not 0 < size <= CONTROL_MAXIMUM:
        raise ValueError("协议控制头超限")
    control = json.loads(
        read_exact(sys.stdin.buffer, size).decode("utf-8", errors="strict"),
        object_pairs_hook=strict_object,
    )
    if type(control) is not dict:
        raise ValueError("协议控制头不合法")
    return control


def set_resources(cpu_seconds: int, address_space_bytes: int) -> None:
    """normal/probe共用实际限额设置；soft/hard均有限并禁止core。"""
    import resource

    if (
        type(cpu_seconds) is not int
        or type(address_space_bytes) is not int
        or min(cpu_seconds, address_space_bytes) <= 0
    ):
        raise ValueError("资源限额不合法")
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
    resource.setrlimit(resource.RLIMIT_AS, (address_space_bytes, address_space_bytes))
    if resource.getrlimit(resource.RLIMIT_CPU) != (
        cpu_seconds,
        cpu_seconds + 1,
    ) or resource.getrlimit(resource.RLIMIT_AS) != (
        address_space_bytes,
        address_space_bytes,
    ):
        raise ValueError("资源设置失败")


def runtime_matches() -> bool:
    """精确patch与pypdf版本；在处理内容前调用。"""
    try:
        return (
            platform.system() == "Linux"
            and platform.python_implementation() == "CPython"
            and sys.version_info[:3] == (3, 12, 14)
            and importlib.metadata.version("pypdf") == "6.16.2"
        )
    except importlib.metadata.PackageNotFoundError:
        return False


def emit(payload: dict, maximum_bytes: int) -> None:
    """stdout只写一个有限JSON帧，编码后真实byte数才是上限。"""
    data = json.dumps(
        payload, ensure_ascii=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    if len(data) > maximum_bytes:
        data = b'{"error":"parse_limit_exceeded"}'
        if len(data) > maximum_bytes:
            raise ValueError("结果预算不足")
    sys.stdout.buffer.write(struct.pack("!I", len(data)))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def main() -> int:
    """只接受parse，不接受probe/fault/program或自由路径。"""
    try:
        control = read_control()
        if (
            set(control)
            != {"protocol", "operation", "profile", "page", "input_bytes", "limits"}
            or control["protocol"] != PROTOCOL
            or control["operation"] != "parse"
        ):
            return 70
        limits = control["limits"]
        if (
            type(limits) is not dict
            or set(limits) != LIMIT_FIELDS
            or any(type(v) is not int or v <= 0 for v in limits.values())
            or limits["maximum_excerpt_bytes"] > limits["maximum_text_bytes"]
        ):
            return 70
        size = control["input_bytes"]
        if type(size) is not int or not 0 < size <= limits["maximum_input_bytes"]:
            return 70
        profile, page = control["profile"], control["page"]
        if not (
            (profile == "pdf-text-v1" and type(page) is int and page >= 1)
            or (profile == "rfc822-plain-v1" and page is None)
        ):
            return 70
        set_resources(limits["cpu_seconds"], limits["address_space_bytes"])
        if not runtime_matches():
            emit({"error": "parse_unavailable"}, limits["maximum_result_bytes"])
            return 0
        content = read_exact(sys.stdin.buffer, size)
        from connectors.evidence_text.profiles import parse_profile
        from shared.schemas.evidence_read import EvidenceParseLimits, QuoteEvidenceError

        try:
            parsed = parse_profile(
                content,
                profile=profile,
                page=page,
                limits=EvidenceParseLimits(**limits),
            )
            emit(
                {
                    "protocol": PROTOCOL,
                    "profile": parsed.profile,
                    "page": parsed.page,
                    "text": parsed.text,
                },
                limits["maximum_result_bytes"],
            )
        except QuoteEvidenceError as error:
            emit({"error": error.code}, limits["maximum_result_bytes"])
        except MemoryError:
            emit({"error": "parse_limit_exceeded"}, limits["maximum_result_bytes"])
        return 0
    except MemoryError:
        return 71
    except Exception:  # noqa: BLE001 - 协议/资源错误只有固定退出码，无异常原文
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
