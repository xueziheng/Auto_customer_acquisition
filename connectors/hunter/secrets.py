"""Hunter 固定逻辑凭证引用到部署引用的最窄绑定。"""

from __future__ import annotations

import re

from connectors.hunter.client import HUNTER_API_KEY_REF, HunterSecretResolver
from shared.errors import ValidationError

_DEPLOYMENT_REFERENCE = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")


class BoundHunterSecretResolver:
    """只把 connector 的固定逻辑引用映射到一个受信部署引用。"""

    def __init__(
        self,
        resolver: HunterSecretResolver,
        configured_ref: str,
    ) -> None:
        if (
            not isinstance(resolver, HunterSecretResolver)
            or not isinstance(configured_ref, str)
            or _DEPLOYMENT_REFERENCE.fullmatch(configured_ref) is None
        ):
            raise ValidationError("Hunter 密钥引用配置无效")
        self._resolver = resolver
        self._configured_ref = configured_ref

    def __repr__(self) -> str:
        return "BoundHunterSecretResolver()"

    def resolve(self, secret_ref: str) -> str:
        if secret_ref != HUNTER_API_KEY_REF:
            raise ValidationError("Hunter 凭证引用无效")
        resolution_failed = False
        resolved: str | None = None
        try:
            resolved = self._resolver.resolve(self._configured_ref)
        except Exception:  # noqa: BLE001 - 凭证边界不得穿透实现异常。
            resolution_failed = True
        if resolution_failed:
            raise ValidationError("Hunter 凭证解析失败") from None
        if not isinstance(resolved, str):
            raise ValidationError("Hunter 凭证解析失败")
        return resolved


__all__ = ("BoundHunterSecretResolver",)
