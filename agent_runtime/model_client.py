"""统一结构化模型端口；Provider SDK 与凭证实现必须留在 connectors。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable


@runtime_checkable
class StructuredJsonModelClient(Protocol):
    """调用模型并返回 JSON 对象文本，不向业务层暴露 Provider SDK。"""

    async def complete_json(
        self,
        *,
        model: str,
        system_prompt: str,
        payload: Mapping[str, object],
        max_output_tokens: int,
    ) -> str: ...


__all__ = ("StructuredJsonModelClient",)
