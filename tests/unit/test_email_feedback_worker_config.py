"""邮件反馈 worker 配置的严格解析与脱敏契约。"""

from __future__ import annotations

from dataclasses import fields

import pytest
from pydantic import SecretStr

from shared.errors import ValidationError
from shared.schemas.identifiers import new_id


def _environment() -> dict[str, str]:
    return {
        "DATABASE_URL": "postgresql+asyncpg://worker-secret@db.example/tradeos",
        "GMAIL_OAUTH_TOKEN_REF": "GMAIL_WORKER_OAUTH_TOKEN",
        "TOOL_CALL_FINGERPRINT_KEY_REF": "TOOL_WORKER_FINGERPRINT_KEY",
        "TOOL_CALL_FINGERPRINT_KEY_VERSION": "feedback-v1",
        "TRADEOS_DEV_MODE": "false",
        "TRADEOS_TENANT_ID": new_id("tn"),
        "TRADEOS_EMAIL_FEEDBACK_GMAIL_BASE_URL": "https://gmail.googleapis.com",
        "TRADEOS_EMAIL_FEEDBACK_MAILBOX_ALIAS": "delivery-feedback",
        "TRADEOS_EMAIL_FEEDBACK_SENDING_IDENTITY_ID": new_id("sid"),
        "TRADEOS_EMAIL_FEEDBACK_ROUTE_ID": "feedback-route-v1",
        "TRADEOS_EMAIL_FEEDBACK_ENABLED": "true",
        "TRADEOS_EMAIL_FEEDBACK_HEALTH_PORT": "8092",
        "TRADEOS_EMAIL_FEEDBACK_POLL_INTERVAL_SECONDS": "30",
        "TRADEOS_EMAIL_FEEDBACK_PAGE_LIMIT": "100",
    }


def test_worker_settings_parse_strict_safe_environment() -> None:
    config = __import__(
        "apps.email_feedback_worker.config", fromlist=["EmailFeedbackWorkerSettings"]
    )
    settings = config.EmailFeedbackWorkerSettings.from_environ(_environment())

    assert isinstance(settings.database_url, SecretStr)
    assert settings.database_url.get_secret_value().startswith("postgresql+asyncpg://")
    assert settings.gmail_oauth_token_ref == "GMAIL_WORKER_OAUTH_TOKEN"
    assert settings.tool_call_fingerprint_key_ref == "TOOL_WORKER_FINGERPRINT_KEY"
    assert settings.tool_call_fingerprint_key_version == "feedback-v1"
    assert settings.dev_mode is False
    assert settings.gmail_base_url == "https://gmail.googleapis.com"
    assert settings.config.mailbox_alias == "delivery-feedback"
    assert settings.config.enabled is True
    assert settings.config.health_port == 8092
    assert settings.config.poll_interval_seconds == 30
    assert settings.config.page_limit == 100
    assert config.BOOTSTRAP_DAYS == 30
    assert "DATABASE_URL" not in repr(settings)
    assert "worker-secret" not in repr(settings)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DATABASE_URL", ""),
        ("DATABASE_URL", " postgresql+asyncpg://db/test"),
        ("GMAIL_OAUTH_TOKEN_REF", "gmail-token"),
        ("GMAIL_OAUTH_TOKEN_REF", "GMAIL_TOKEN\x00"),
        ("TOOL_CALL_FINGERPRINT_KEY_REF", "tool-secret"),
        ("TOOL_CALL_FINGERPRINT_KEY_VERSION", " bearer-v1"),
        ("TOOL_CALL_FINGERPRINT_KEY_VERSION", "token-secret"),
        ("TRADEOS_DEV_MODE", "TRUE"),
        ("TRADEOS_TENANT_ID", "tenant-one"),
        ("TRADEOS_EMAIL_FEEDBACK_GMAIL_BASE_URL", "http://127.0.0.1:8111"),
        ("TRADEOS_EMAIL_FEEDBACK_GMAIL_BASE_URL", "https://gmail.googleapis.com/"),
        ("TRADEOS_EMAIL_FEEDBACK_SENDING_IDENTITY_ID", "sid-not-ulid"),
        ("TRADEOS_EMAIL_FEEDBACK_MAILBOX_ALIAS", "Delivery-Feedback"),
        ("TRADEOS_EMAIL_FEEDBACK_MAILBOX_ALIAS", "token-mailbox"),
        ("TRADEOS_EMAIL_FEEDBACK_MAILBOX_ALIAS", "delivery\nfeedback"),
        ("TRADEOS_EMAIL_FEEDBACK_ROUTE_ID", "secret-route"),
        ("TRADEOS_EMAIL_FEEDBACK_ROUTE_ID", "route_underscore"),
        ("TRADEOS_EMAIL_FEEDBACK_ENABLED", "1"),
        ("TRADEOS_EMAIL_FEEDBACK_ENABLED", "TRUE"),
        ("TRADEOS_EMAIL_FEEDBACK_HEALTH_PORT", "true"),
        ("TRADEOS_EMAIL_FEEDBACK_HEALTH_PORT", "0"),
        ("TRADEOS_EMAIL_FEEDBACK_HEALTH_PORT", "65536"),
        ("TRADEOS_EMAIL_FEEDBACK_POLL_INTERVAL_SECONDS", "4"),
        ("TRADEOS_EMAIL_FEEDBACK_POLL_INTERVAL_SECONDS", "3601"),
        ("TRADEOS_EMAIL_FEEDBACK_PAGE_LIMIT", "0"),
        ("TRADEOS_EMAIL_FEEDBACK_PAGE_LIMIT", "101"),
    ],
)
def test_worker_settings_reject_invalid_field(name: str, value: str) -> None:
    config = __import__(
        "apps.email_feedback_worker.config", fromlist=["EmailFeedbackWorkerSettings"]
    )
    environment = _environment()
    environment[name] = value

    with pytest.raises(config.WorkerConfigurationError) as captured:
        config.EmailFeedbackWorkerSettings.from_environ(environment)

    assert captured.value.field_name == name
    if value:
        assert value not in str(captured.value)


def test_worker_settings_require_each_explicit_field() -> None:
    config = __import__(
        "apps.email_feedback_worker.config", fromlist=["EmailFeedbackWorkerSettings"]
    )
    for name in tuple(_environment()):
        environment = _environment()
        del environment[name]
        with pytest.raises(config.WorkerConfigurationError) as captured:
            config.EmailFeedbackWorkerSettings.from_environ(environment)
        assert captured.value.field_name == name


@pytest.mark.parametrize(
    "base_url",
    [
        "http://127.0.0.1:8111",
        "http://localhost:8111",
        "http://[::1]:8111",
    ],
)
def test_worker_settings_allow_canonical_loopback_only_in_explicit_dev_mode(
    base_url: str,
) -> None:
    config = __import__(
        "apps.email_feedback_worker.config", fromlist=["EmailFeedbackWorkerSettings"]
    )
    environment = _environment()
    environment["TRADEOS_DEV_MODE"] = "true"
    environment["TRADEOS_EMAIL_FEEDBACK_GMAIL_BASE_URL"] = base_url

    settings = config.EmailFeedbackWorkerSettings.from_environ(environment)

    assert settings.dev_mode is True
    assert settings.gmail_base_url == base_url


@pytest.mark.parametrize(
    "base_url",
    [
        "http://127.0.0.1",
        "http://127.0.0.1:80/path",
        "http://127.0.0.1:8111?query=1",
        "http://127.0.0.1:8111#fragment",
        "http://user@127.0.0.1:8111",
        "http://LOCALHOST:8111",
        "http://127.0.0.1:08111",
        "http://0.0.0.0:8111",
        "https://example.test",
    ],
)
def test_worker_settings_reject_noncanonical_or_non_gmail_base_url(
    base_url: str,
) -> None:
    config = __import__(
        "apps.email_feedback_worker.config", fromlist=["EmailFeedbackWorkerSettings"]
    )
    environment = _environment()
    environment["TRADEOS_DEV_MODE"] = "true"
    environment["TRADEOS_EMAIL_FEEDBACK_GMAIL_BASE_URL"] = base_url

    with pytest.raises(config.WorkerConfigurationError) as captured:
        config.EmailFeedbackWorkerSettings.from_environ(environment)

    assert captured.value.field_name == "TRADEOS_EMAIL_FEEDBACK_GMAIL_BASE_URL"
    assert base_url not in str(captured.value)


def test_worker_config_rejects_bool_as_integer_at_runtime() -> None:
    config = __import__(
        "apps.email_feedback_worker.config", fromlist=["EmailFeedbackWorkerConfig"]
    )
    base = {
        "tenant_id": new_id("tn"),
        "mailbox_alias": "feedback",
        "sending_identity_id": new_id("sid"),
        "feedback_route_id": "feedback-v1",
    }
    for name in ("poll_interval_seconds", "page_limit", "health_port"):
        with pytest.raises(ValidationError):
            config.EmailFeedbackWorkerConfig(**base, **{name: True})


def test_worker_does_not_model_bootstrap_or_unsubscribe_hmac_configuration() -> None:
    config = __import__(
        "apps.email_feedback_worker.config", fromlist=["EmailFeedbackWorkerSettings"]
    )
    environment = _environment()
    environment.update(
        {
            "TRADEOS_EMAIL_FEEDBACK_BOOTSTRAP_DAYS": "999",
            "TRADEOS_UNSUBSCRIBE_ACTIVE_KEY_ID": "credential-marker",
            "TRADEOS_UNSUBSCRIBE_KEY_REFS_JSON": "customer-secret-marker",
        }
    )

    settings = config.EmailFeedbackWorkerSettings.from_environ(environment)

    names = {item.name for item in fields(settings)} | {
        item.name for item in fields(settings.config)
    }
    assert "bootstrap_days" not in names
    assert not any("unsubscribe" in name or "hmac" in name for name in names)
    rendered = repr(settings)
    assert "999" not in rendered
    assert "customer-secret-marker" not in rendered
