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
