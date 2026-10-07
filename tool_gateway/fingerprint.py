"""Tool call HMAC-SHA-256 指纹。"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass, field

from shared.errors import ValidationError

_DOMAIN = b"tradeos.tool-call-fingerprint\x00v1\x00"
_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")
_SECRET_MARKERS = ("secret", "token", "password", "authorization", "bearer")
_MAX_PARTS = 32
_MAX_PART_BYTES = 1_048_576
_MAX_TOTAL_BYTES = 4_194_304


@dataclass(frozen=True)
class HmacFingerprintProvider:
    """用稳定 key/version 对无歧义字节序列生成不可逆指纹。"""

    key_version: str
    _key: bytes = field(repr=False)

    def __post_init__(self) -> None:
        version = self.key_version
        if (
            not isinstance(version, str)
            or _VERSION_RE.fullmatch(version) is None
            or any(marker in version.casefold() for marker in _SECRET_MARKERS)
        ):
            raise ValidationError("指纹版本无效")
        key = self._key
        if not isinstance(key, bytes) or not 32 <= len(key) <= 64:
            raise ValidationError("指纹密钥无效")
        object.__setattr__(self, "_key", bytes(key))

    def fingerprint(self, parts: tuple[bytes, ...]) -> tuple[str, str]:
        """返回 lower-hex digest 与公开 key version。"""
        if not isinstance(parts, tuple) or not 1 <= len(parts) <= _MAX_PARTS:
            raise ValidationError("指纹输入无效")
        total = 0
        encoded = bytearray(_DOMAIN)
        version = self.key_version.encode("ascii")
        encoded.extend(len(version).to_bytes(4, "big"))
        encoded.extend(version)
        for part in parts:
            if not isinstance(part, bytes) or len(part) > _MAX_PART_BYTES:
                raise ValidationError("指纹输入无效")
            total += len(part)
            if total > _MAX_TOTAL_BYTES:
                raise ValidationError("指纹输入无效")
            encoded.extend(len(part).to_bytes(4, "big"))
            encoded.extend(part)
        digest = hmac.new(self._key, bytes(encoded), hashlib.sha256).hexdigest()
        return digest, self.key_version
