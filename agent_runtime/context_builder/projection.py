"""模型可见投影的确定性扫描与预算；不接触任何原件或凭证。"""

from __future__ import annotations

import json
import re
from typing import Any

from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
from shared.errors import ValidationError

_TOOL = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+")
_PRIVATE = re.compile(
    r"[\w.+-]+@[\w.-]+\.[\w-]+|[a-z][a-z0-9+.-]*://|"
    r'(?:^|[\s"\'=:])(?:/|~[/\\]|[A-Za-z]:\\)|'
    r"\b(?:internal[ _]cost|estimated[ _]cost|estimated[ _]profit|object_key|object_locator)\b|"
    r"内部成本|采购成本|预计成本|[\w.-]+[/\\][\w.-]+",
    re.IGNORECASE,
)
_SAFE_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")


def check_candidate(value: object) -> None:
    """先凭证检查再隐私检查；对将被裁掉的候选也执行。"""
    if isinstance(value, str):
        CredentialMarkerGuard().check(subject=None, body=value)
        if _PRIVATE.search(value):
            raise ValidationError("上下文包含不允许的私密内容")
    elif isinstance(value, dict):
        for key, child in value.items():
            check_candidate(key)
            check_candidate(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            check_candidate(child)


def check_ref(value: str) -> None:
    """安全引用可留审计，但不可夹带路径、自由文本或任意长度。"""
    check_candidate(value)
    if _SAFE_REF.fullmatch(value) is None:
        raise ValidationError("上下文安全引用无效")


def check_tools(tools: tuple[str, ...]) -> None:
    """工具只允许精确标识符；通配授权与通配禁止都明确拒绝。"""
    if type(tools) is not tuple or any(
        type(tool) is not str or _TOOL.fullmatch(tool) is None for tool in tools
    ):
        raise ValidationError("上下文工具配置无效")
    check_candidate(tools)


def json_bytes(value: object) -> int:
    """完整可见 JSON 的 UTF-8 字节数；并非真实模型用量。"""
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )


def model_payload(
    sections: list[dict[str, Any]],
    allowed: tuple[str, ...],
    blocked: tuple[str, ...],
    scope: dict[str, Any],
) -> dict[str, Any]:
    """模型消费者只能序列化这些字段；估算和截断日志属于运行审计。"""
    return {
        "sections": sections,
        "allowed_tools": allowed,
        "blocked_tools": blocked,
        "data_scope": scope,
    }


def positive_limit(value: int) -> None:
    """所有资源上限必须显式为正整数，bool 不能代替数值。"""
    if type(value) is not int or value <= 0:
        raise ValidationError("上下文资源上限必须为正整数")
