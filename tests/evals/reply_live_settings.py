"""真实回复验收的显式前置条件；不推导额度，不创建 Provider。"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from infra.secrets import EnvironmentSecretResolver
from infra.standalone.settings import StandaloneModelSettings, load_model_settings
from shared.errors import ValidationError

REQUIRED_CALLS = 163


@dataclass(frozen=True)
class LiveReplySettings:
    settings: StandaloneModelSettings
    max_calls: int
    resolver: EnvironmentSecretResolver = field(repr=False)


def load_live_settings(environ: Mapping[str, str]) -> LiveReplySettings:
    """先拒绝未启用/缺配置/缺密钥/预算不足，再允许创建 owned 测试环境。"""
    if environ.get("TRADEOS_REPLY_LIVE") != "1":
        pytest.skip("真实回复模型验收未启用；未调用模型")
    raw_path = environ.get("TRADEOS_REPLY_MODEL_SETTINGS_PATH", "")
    if not raw_path or not Path(raw_path).is_absolute():
        pytest.fail("真实验收需要显式模型配置绝对路径", pytrace=False)
    try:
        maximum = int(environ.get("TRADEOS_REPLY_LIVE_MAX_CALLS", ""))
        config = load_model_settings(Path(raw_path))
    except (ValueError, OSError):
        pytest.fail("真实验收配置或显式调用预算无效", pytrace=False)
    if not config.reply_enabled or not config.model_data_export_enabled:
        pytest.fail("真实验收需要启用回复模型和模型数据外发许可", pytrace=False)
    if (
        min(maximum, config.limits.tenant_calls, config.limits.employee_calls)
        < REQUIRED_CALLS
    ):
        pytest.fail(
            "真实验收至少需要163次调用预算；未调用模型，未修改限额", pytrace=False
        )
    resolver = EnvironmentSecretResolver(environ)
    try:
        if not resolver.resolve(config.secret_ref).strip():
            raise ValidationError("模型凭证未配置")
    except ValidationError:
        pytest.fail("可信环境解析器无法解析模型凭证", pytrace=False)
    return LiveReplySettings(config, maximum, resolver)
