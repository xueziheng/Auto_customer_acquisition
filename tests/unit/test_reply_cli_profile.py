"""固定任务说明由受信身份选择；正文不能改变 CLI 配置。"""

from pathlib import Path

import pytest

from apps.composition_support.model import configured_codex_provider
from infra.standalone.settings import StandaloneModelSettings
from shared.schemas.model_invocation import InvocationIdentity
from tests.integration.test_model_usage import limits


def profile_settings(root):
    return StandaloneModelSettings(
        provider="deepseek", model="deepseek-flash", secret_ref="SYNTHETIC_ONLY",
        configuration_version="synthetic-v1", model_data_export_enabled=True,
        api_key_file=root / "absent-key", api_control_tenant="tn_synthetic",
        codex_job_root=root, limits=limits(timeout_seconds=60, max_input_bytes=32768,
                                        max_output_tokens=512),
    )


def profile_identity(capability="reply_qualification"):
    return InvocationIdentity(
        tenant_id="tn_synthetic", user_id="usr_synthetic", employee_id="emp_synthetic",
        run_id="run_synthetic", capability=capability,
        configuration_version="synthetic-v1", sequence=0,
    )


@pytest.mark.parametrize("capability,bounded,expected", [
    ("reply_qualification", True, True),
    ("reply_qualification", False, False),
    ("research", True, False),
    ("research", False, False),
])
def test_compact_profile_only_for_bounded_reply_identity(
    monkeypatch, tmp_path, capability, bounded, expected,
):
    captured = {}

    def provider(transport, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("connectors.codex_agent.client.codex_agent_provider", provider)
    configured_codex_provider(profile_settings(tmp_path), profile_identity(capability),
                              round_budget=object() if bounded else None)
    assert captured["settings"].reply_classification is expected
    assert not (tmp_path / "absent-key").exists()
    assert captured["settings"].max_input_bytes == 32768


def test_default_runner_profile_is_unchanged():
    from connectors.codex.client import CodexRunnerSettings

    settings = CodexRunnerSettings(*(Path("/synthetic") for _ in range(4)), 60, 32768, 4096)
    assert settings.reply_classification is False
