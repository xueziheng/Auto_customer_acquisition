"""运行时环境密钥引用的最窄解析能力。"""

from __future__ import annotations

import re
from collections.abc import Mapping

from shared.errors import ValidationError

_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,127}")


def validate_environment_secret_reference(value: object) -> str:
    """只接受显式 canonical 环境变量名，不解释路径或模板。"""
    if not isinstance(value, str) or _ENV_NAME.fullmatch(value) is None:
        raise ValidationError("环境密钥引用无效")
    return value


class EnvironmentSecretResolver:
    """按调用方给出的安全变量名读取一个值；不枚举、不缓存、不展示。"""

    def __init__(self, environ: Mapping[str, str]) -> None:
        self._environ = environ

    def __repr__(self) -> str:
        return "EnvironmentSecretResolver()"

    def resolve(self, secret_ref: str) -> str:
        name = validate_environment_secret_reference(secret_ref)
        try:
            value = self._environ[name]
        except KeyError:
            raise ValidationError("环境密钥未配置") from None
        if not isinstance(value, str):
            raise ValidationError("环境密钥无效")
        return value
