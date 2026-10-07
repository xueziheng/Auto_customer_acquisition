"""真实验收必须显式授权，缺配置或预算时在模型与基础设施启动前拒绝。"""

import importlib
import json

import pytest

from tests.unit.test_standalone_model_settings import settings


def loader():
    return importlib.import_module("tests.evals.reply_live_settings").load_live_settings


def configured(tmp_path):
    data = {
        **settings(),
        "reply_enabled": True,
        "limits": {
            **settings()["limits"],
            "tenant_calls": 163,
            "employee_calls": 163,
            "max_output_tokens": 2048,
        },
    }
    tmp_path.chmod(0o700)
    path = tmp_path / "model.json"
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    environ = {
        "TRADEOS_REPLY_LIVE": "1",
        "TRADEOS_REPLY_MODEL_SETTINGS_PATH": str(path),
        "TRADEOS_REPLY_LIVE_MAX_CALLS": "163",
        "DEEPSEEK_API_KEY": "synthetic-unit-only",
    }
    return data, path, environ


def test_live_disabled_does_not_require_or_read_settings():
    with pytest.raises(pytest.skip.Exception):
        loader()({})


@pytest.mark.parametrize(
    "case", ["path", "budget", "insufficient", "quota", "disabled", "export", "secret"]
)
def test_live_preflight_rejects_missing_authority(tmp_path, case):
    data, path, environ = configured(tmp_path)
    if case == "path":
        environ.pop("TRADEOS_REPLY_MODEL_SETTINGS_PATH")
    elif case == "budget":
        environ.pop("TRADEOS_REPLY_LIVE_MAX_CALLS")
    elif case == "insufficient":
        environ["TRADEOS_REPLY_LIVE_MAX_CALLS"] = "162"
    elif case == "quota":
        data["limits"]["employee_calls"] = 162
    elif case == "disabled":
        data["reply_enabled"] = False
    elif case == "export":
        data["model_data_export_enabled"] = False
    else:
        environ.pop("DEEPSEEK_API_KEY")
    path.write_text(json.dumps(data))
    with pytest.raises(pytest.fail.Exception):
        loader()(environ)


def test_live_explicit_settings_remain_unchanged_and_secret_not_in_receipt(tmp_path):
    data, path, environ = configured(tmp_path)
    before = path.read_bytes()
    config = loader()(environ)
    assert config.settings.model == data["model"]
    assert config.settings.limits.employee_calls == 163
    assert config.max_calls == 163
    assert config.resolver.resolve(config.settings.secret_ref) == "synthetic-unit-only"
    assert "synthetic-unit-only" not in repr(config)
    assert path.read_bytes() == before
