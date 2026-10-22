"""旧启动参数兼容，显式/相邻Hunter元数据沿同一入口传递，不读取Key。"""
import sys

import pytest

from apps.scheduler_worker import standalone
from tests.unit.test_pilot_profile import make_config
from tests.unit.test_standalone_model_settings import settings


@pytest.mark.parametrize("mode", ["absent", "adjacent", "explicit"])
def test_original_scheduler_arguments_keep_optional_hunter_metadata(tmp_path, monkeypatch, mode):
    profile_path = tmp_path / "profile.json"
    model_path = tmp_path / "model.json"
    expected = None
    args = ["scheduler", "--profile", str(profile_path), "--model-settings", str(model_path)]
    if mode != "absent":
        expected = tmp_path / ("hunter-customer-settings.json" if mode == "adjacent" else "chosen-hunter.json")
        expected.write_text("synthetic metadata untouched")
        if mode == "explicit": args += ["--hunter-customer-settings", str(expected)]
    profile = make_config(tmp_path)
    configured = standalone.StandaloneModelSettings.model_validate(settings())
    seen = []
    monkeypatch.setattr(sys, "argv", args)
    monkeypatch.setattr(standalone.PilotConfig, "read", lambda path: profile)
    monkeypatch.setattr(standalone, "load_model_settings", lambda path: configured)
    monkeypatch.setattr(standalone.logging, "disable", lambda *_: None)
    def factory(actual_profile, actual_settings, resolver, **kwargs):
        assert actual_profile is profile and actual_settings is configured
        seen.append(kwargs["hunter_customer_settings_file"])
        return "synthetic-runtime-factory"
    monkeypatch.setattr(standalone, "create_standalone_factory", factory)
    monkeypatch.setattr(standalone, "run_worker", lambda factory: 0 if factory == "synthetic-runtime-factory" else 2)
    assert standalone.main() == 0
    assert seen == [expected]
    if expected is not None: assert expected.read_text() == "synthetic metadata untouched"
    else: assert not (tmp_path / "hunter-customer-settings.json").exists()
