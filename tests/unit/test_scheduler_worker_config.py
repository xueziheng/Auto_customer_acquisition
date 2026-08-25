"""Hunter 部署元数据必须严格解析，且不解析密钥值。"""

from __future__ import annotations

import pytest

from apps.scheduler_worker.config import HunterContactsSettings
from shared.errors import ValidationError
from tool_gateway.provider_readiness import ProviderConfiguration


def test_disabled_hunter_configuration_contains_no_secret_metadata() -> None:
    settings = HunterContactsSettings.from_environ(
        {"TRADEOS_HUNTER_CONTACTS_ENABLED": "false"}
    )

    assert settings.enabled is False
    assert settings.configuration is None
    assert settings.secret_ref is None


def test_enabled_hunter_configuration_builds_safe_hash_without_resolving_secret() -> None:
    environ = {
        "TRADEOS_HUNTER_CONTACTS_ENABLED": "true",
        "TRADEOS_HUNTER_CONFIGURATION_VERSION": "deploy-v1",
        "TRADEOS_HUNTER_API_KEY_SECRET_REF": "HUNTER_API_KEY_PROD",
        "TRADEOS_HUNTER_API_KEY_VERSION": "key-v1",
    }

    settings = HunterContactsSettings.from_environ(environ)

    assert settings.enabled is True
    assert settings.configuration == ProviderConfiguration.hunter_contacts(
        "deploy-v1", "key-v1"
    )
    assert settings.secret_ref == "HUNTER_API_KEY_PROD"
    assert "HUNTER_API_KEY_PROD" not in repr(settings)


@pytest.mark.parametrize(
    "environ",
    [
        {},
        {"TRADEOS_HUNTER_CONTACTS_ENABLED": "True"},
        {"TRADEOS_HUNTER_CONTACTS_ENABLED": "FALSE"},
        {"TRADEOS_HUNTER_CONTACTS_ENABLED": "1"},
    ],
)
def test_hunter_enable_flag_requires_lowercase_boolean(
    environ: dict[str, str],
) -> None:
    with pytest.raises(ValidationError):
        HunterContactsSettings.from_environ(environ)


@pytest.mark.parametrize(
    "missing",
    [
        "TRADEOS_HUNTER_CONFIGURATION_VERSION",
        "TRADEOS_HUNTER_API_KEY_SECRET_REF",
        "TRADEOS_HUNTER_API_KEY_VERSION",
    ],
)
def test_enabled_hunter_configuration_requires_all_metadata(missing: str) -> None:
    environ = {
        "TRADEOS_HUNTER_CONTACTS_ENABLED": "true",
        "TRADEOS_HUNTER_CONFIGURATION_VERSION": "deploy-v1",
        "TRADEOS_HUNTER_API_KEY_SECRET_REF": "HUNTER_API_KEY_PROD",
        "TRADEOS_HUNTER_API_KEY_VERSION": "key-v1",
    }
    environ.pop(missing)

    with pytest.raises(ValidationError):
        HunterContactsSettings.from_environ(environ)


@pytest.mark.parametrize(
    "name,value",
    [
        ("TRADEOS_HUNTER_CONFIGURATION_VERSION", "deploy-v1\n"),
        ("TRADEOS_HUNTER_API_KEY_SECRET_REF", "hunter-key"),
        ("TRADEOS_HUNTER_API_KEY_SECRET_REF", "HUNTER\tKEY"),
        ("TRADEOS_HUNTER_API_KEY_VERSION", "key-v1\x7f"),
        ("TRADEOS_HUNTER_CONFIGURATION_VERSION", "secret-v1"),
        ("TRADEOS_HUNTER_API_KEY_VERSION", "token-v1"),
    ],
)
def test_enabled_hunter_configuration_rejects_unsafe_metadata(
    name: str, value: str
) -> None:
    environ = {
        "TRADEOS_HUNTER_CONTACTS_ENABLED": "true",
        "TRADEOS_HUNTER_CONFIGURATION_VERSION": "deploy-v1",
        "TRADEOS_HUNTER_API_KEY_SECRET_REF": "HUNTER_API_KEY_PROD",
        "TRADEOS_HUNTER_API_KEY_VERSION": "key-v1",
        name: value,
    }

    with pytest.raises(ValidationError):
        HunterContactsSettings.from_environ(environ)


@pytest.mark.parametrize(
    "name",
    [
        "TRADEOS_HUNTER_CONFIGURATION_VERSION",
        "TRADEOS_HUNTER_API_KEY_SECRET_REF",
        "TRADEOS_HUNTER_API_KEY_VERSION",
    ],
)
def test_disabled_hunter_configuration_rejects_nonempty_companion_metadata(
    name: str,
) -> None:
    with pytest.raises(ValidationError):
        HunterContactsSettings.from_environ(
            {
                "TRADEOS_HUNTER_CONTACTS_ENABLED": "false",
                name: "unused",
            }
        )
