"""没有隐式付费额度，也不允许通过配置改向任意服务商。"""

import pytest
from pydantic import ValidationError

from infra.standalone.settings import StandaloneModelSettings, load_model_settings
from tests.integration.test_model_usage import limits


def settings():
    return {
        "provider": "deepseek",
        "model": "explicit-test-model",
        "secret_ref": "DEEPSEEK_API_KEY",
        "configuration_version": "explicit-v1",
        "limits": limits().model_dump(),
        "model_data_export_enabled": True,
    }


def test_no_implicit_paid_defaults():
    with pytest.raises(ValidationError):
        StandaloneModelSettings.model_validate(
            {"provider": "deepseek", "model": "test"}
        )


@pytest.mark.parametrize(
    "patch",
    [
        {"base_url": "https://example.invalid"},
        {"provider": "openai"},
        {"model_data_export_enabled": "true"},
        {"model": " https://example.invalid"},
    ],
)
def test_provider_endpoint_and_strict_explicit_settings(patch):
    with pytest.raises(ValidationError):
        StandaloneModelSettings.model_validate({**settings(), **patch})


def test_loader_rejects_symlink_and_redacts_secret_ref(tmp_path):
    import json

    tmp_path.chmod(0o700)
    path = tmp_path / "model.json"
    path.write_text(json.dumps(settings()))
    path.chmod(0o600)
    value = load_model_settings(path)
    assert (
        "DEEPSEEK_API_KEY" not in repr(value) and "secret_ref" not in value.model_dump()
    )
    link = tmp_path / "linked.json"
    link.symlink_to(path)
    with pytest.raises(ValueError):
        load_model_settings(link)


def test_reply_switch_requires_explicit_valid_configuration():
    assert StandaloneModelSettings.model_validate(settings()).reply_enabled is False
    for enabled in ("true", "false", 1, 0, None):
        with pytest.raises(ValidationError):
            StandaloneModelSettings.model_validate(
                {**settings(), "reply_enabled": enabled}
            )


@pytest.mark.parametrize(
    "tokens,valid", [(127, False), (128, True), (8192, True), (8193, False)]
)
def test_reply_output_limits_match_existing_classifier(tokens, valid):
    config = {
        **settings(),
        "reply_enabled": True,
        "limits": {**settings()["limits"], "max_output_tokens": tokens},
    }
    if valid:
        assert StandaloneModelSettings.model_validate(config).reply_enabled is True
    else:
        with pytest.raises(ValidationError):
            StandaloneModelSettings.model_validate(config)


def test_disabled_reply_preserves_other_capability_limits():
    config = {
        **settings(),
        "reply_enabled": False,
        "limits": {**settings()["limits"], "max_output_tokens": 64},
    }
    assert StandaloneModelSettings.model_validate(config).limits.max_output_tokens == 64
