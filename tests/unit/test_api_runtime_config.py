"""Phase 1 API runtime 的显式、严格、脱敏环境配置。"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

try:
    from apps.api.runtime_config import (
        Phase1RuntimeSettings,
        RuntimeConfigurationError,
    )
except ModuleNotFoundError:
    class RuntimeConfigurationError(RuntimeError):
        pass

    class Phase1RuntimeSettings:
        @classmethod
        def from_environ(cls, _environ: object) -> object:
            pytest.fail("RED：apps.api.runtime_config 尚未实现")


_VALID_ENV = {
    "DATABASE_URL": (
        "postgresql+asyncpg://"
        + "runtime-user"
        + ":"
        + "runtime-secret"
        + "@db.invalid/tradeos"
    ),
    "TRADEOS_TENANT_ID": "tenant-runtime",
    "TRADEOS_DEV_MODE": "true",
    "TRADEOS_CORS_ALLOWED_ORIGINS": '["http://127.0.0.1:4173"]',
    "TRADEOS_API_RETRY_AFTER_SECONDS": "30",
    "TRADEOS_HANDOFF_POLICY": (
        '{"sla_seconds":300,"backlog_threshold":20,'
        '"t1_seconds":120,"t2_seconds":180}'
    ),
    "TRADEOS_SCORING_POLICY": (
        '{"version":"phase1-v1","currency":"USD",'
        '"value_band_boundaries":["1000","5000"],'
        '"bucket_map":{"1":"low","2":"low","3":"mid","4":"mid",'
        '"5":"high","6":"high","7":"high"}}'
    ),
    "TRADEOS_OUTBOX_MAX_ATTEMPTS": "3",
    "GMAIL_OAUTH_TOKEN_REF": "gmail-oauth-phase1",
    "TOOL_CALL_FINGERPRINT_KEY_REF": "tool-fingerprint-phase1",
    "TOOL_CALL_FINGERPRINT_KEY_VERSION": "v1",
    "TRADEOS_UNSUBSCRIBE_BASE_URL": "https://unsubscribe.example.test",
    "TRADEOS_EMAIL_FEEDBACK_ROUTE_ID": "feedback-route-v1",
    "TRADEOS_UNSUBSCRIBE_ACTIVE_KEY_ID": "2026-v1",
    "TRADEOS_UNSUBSCRIBE_KEY_REFS_JSON": (
        '{"2025-v1":"UNSUBSCRIBE_HMAC_2025",'
        '"2026-v1":"UNSUBSCRIBE_HMAC_2026"}'
    ),
    "TRADEOS_TOOL_LEASE_SECONDS": "120",
}


def test_runtime_settings_parse_exact_configuration_without_exposing_dsn() -> None:
    settings = Phase1RuntimeSettings.from_environ(_VALID_ENV)
    assert settings.tenant_id == "tenant-runtime"
    assert settings.scoring_policy.value_band_boundaries[0].amount == Decimal(1000)
    assert settings.t1.total_seconds() == 120
    assert settings.gmail_oauth_token_ref == "gmail-oauth-phase1"
    assert settings.tool_call_fingerprint_key_ref == "tool-fingerprint-phase1"
    assert settings.tool_call_fingerprint_key_version == "v1"
    assert settings.unsubscribe_base_url == "https://unsubscribe.example.test"
    assert settings.email_feedback_route_id == "feedback-route-v1"
    assert settings.unsubscribe_active_key_id == "2026-v1"
    assert len(settings.unsubscribe_key_refs) == 2
    assert settings.tool_lease.total_seconds() == 120
    assert "runtime-secret" not in repr(settings)


@pytest.mark.parametrize("name", sorted(_VALID_ENV))
def test_every_runtime_variable_is_required_and_error_is_sanitized(name: str) -> None:
    env = dict(_VALID_ENV)
    value = env.pop(name)
    with pytest.raises(RuntimeConfigurationError, match="API runtime 配置无效") as exc:
        Phase1RuntimeSettings.from_environ(env)
    if value.strip():
        assert value not in str(exc.value)
    assert exc.value.field_name == name


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DATABASE_URL", ""),
        ("DATABASE_URL", " postgresql+asyncpg://hidden.invalid/db"),
        ("TRADEOS_TENANT_ID", " "),
        ("TRADEOS_TENANT_ID", "tenant-runtime "),
        ("TRADEOS_DEV_MODE", "True"),
        ("TRADEOS_DEV_MODE", "false"),
        ("GMAIL_OAUTH_TOKEN_REF", ""),
        ("GMAIL_OAUTH_TOKEN_REF", " gmail-oauth-phase1"),
        ("GMAIL_OAUTH_TOKEN_REF", "gmail\noauth"),
        ("TOOL_CALL_FINGERPRINT_KEY_REF", "tool-fingerprint-phase1 "),
        ("TOOL_CALL_FINGERPRINT_KEY_REF", "secret\x7fref"),
        ("TOOL_CALL_FINGERPRINT_KEY_VERSION", ""),
        ("TOOL_CALL_FINGERPRINT_KEY_VERSION", "v1 "),
        ("TOOL_CALL_FINGERPRINT_KEY_VERSION", "v1\x00hidden"),
    ],
)
def test_exact_scalar_values_reject_blank_boundary_space_or_implicit_mode(
    name: str, value: str
) -> None:
    with pytest.raises(RuntimeConfigurationError) as exc:
        Phase1RuntimeSettings.from_environ({**_VALID_ENV, name: value})
    assert exc.value.field_name == name
    if value.strip():
        assert value not in str(exc.value)


@pytest.mark.parametrize(
    "value",
    ["0", "-1", "+1", "01", " 1", "1 ", "1.0", "true"],
)
@pytest.mark.parametrize(
    "name",
    [
        "TRADEOS_API_RETRY_AFTER_SECONDS",
        "TRADEOS_OUTBOX_MAX_ATTEMPTS",
        "TRADEOS_TOOL_LEASE_SECONDS",
    ],
)
def test_positive_integer_configuration_is_exact(name: str, value: str) -> None:
    with pytest.raises(RuntimeConfigurationError) as exc:
        Phase1RuntimeSettings.from_environ({**_VALID_ENV, name: value})
    assert exc.value.field_name == name


@pytest.mark.parametrize(
    ("origin_json", "valid"),
    [
        ('["http://127.0.0.1:4173"]', True),
        ('["*"]', False),
        ('["http://127.0.0.1:4173/path"]', False),
        ('["http://user@127.0.0.1:4173"]', False),
        ('["http://127.0.0.1:4173","http://127.0.0.1:4173"]', False),
        ('["HTTP://127.0.0.1:4173"]', False),
        ('["http://127.0.0.1:80"]', False),
        ('["http://LOCALHOST:4173"]', False),
        ("[]", False),
    ],
)
def test_cors_origin_is_canonical_and_exact(origin_json: str, valid: bool) -> None:
    env = {**_VALID_ENV, "TRADEOS_CORS_ALLOWED_ORIGINS": origin_json}
    if valid:
        assert Phase1RuntimeSettings.from_environ(env).cors_allowed_origins
    else:
        with pytest.raises(RuntimeConfigurationError):
            Phase1RuntimeSettings.from_environ(env)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "http://unsubscribe.example.test",
        "https://user@unsubscribe.example.test",
        "https://unsubscribe.example.test/path",
        "https://unsubscribe.example.test?tenant=hidden",
        "https://unsubscribe.example.test#fragment",
        "https://UNSUBSCRIBE.example.test",
        "https://unsubscribe.example.test/",
        " https://unsubscribe.example.test",
    ],
)
def test_unsubscribe_base_url_is_canonical_https_origin(value: str) -> None:
    marker = "credential-marker"
    candidate = value.replace("user", marker)
    with pytest.raises(RuntimeConfigurationError) as exc:
        Phase1RuntimeSettings.from_environ(
            {**_VALID_ENV, "TRADEOS_UNSUBSCRIBE_BASE_URL": candidate}
        )
    assert exc.value.field_name == "TRADEOS_UNSUBSCRIBE_BASE_URL"
    assert marker not in str(exc.value)


@pytest.mark.parametrize(
    "value",
    ("http://127.0.0.1:8000", "http://localhost:8000", "http://[::1]:8000"),
)
def test_dev_runtime_accepts_canonical_loopback_unsubscribe_origin(
    value: str,
) -> None:
    settings = Phase1RuntimeSettings.from_environ(
        {**_VALID_ENV, "TRADEOS_UNSUBSCRIBE_BASE_URL": value}
    )
    assert settings.unsubscribe_base_url == value


@pytest.mark.parametrize(
    ("name", "value"),
    [
        (
            "TRADEOS_HANDOFF_POLICY",
            (
                '{"sla_seconds":"300","backlog_threshold":20,'
                '"t1_seconds":120,"t2_seconds":180}'
            ),
        ),
        (
            "TRADEOS_HANDOFF_POLICY",
            (
                '{"sla_seconds":300,"backlog_threshold":20,'
                '"t1_seconds":120,"t2_seconds":180,"extra":1}'
            ),
        ),
        (
            "TRADEOS_HANDOFF_POLICY",
            (
                '{"sla_seconds":0,"backlog_threshold":20,'
                '"t1_seconds":120,"t2_seconds":180}'
            ),
        ),
        (
            "TRADEOS_SCORING_POLICY",
            (
                '{"version":"phase1-v1","currency":"USD",'
                '"value_band_boundaries":[1000,"5000"],'
                '"bucket_map":{"1":"low","2":"low","3":"mid","4":"mid",'
                '"5":"high","6":"high","7":"high"}}'
            ),
        ),
        (
            "TRADEOS_SCORING_POLICY",
            (
                '{"version":"phase1-v1","currency":"usd",'
                '"value_band_boundaries":["1000","5000"],'
                '"bucket_map":{"1":"low","2":"low","3":"mid","4":"mid",'
                '"5":"high","6":"high","7":"high"}}'
            ),
        ),
        (
            "TRADEOS_SCORING_POLICY",
            (
                '{"version":"phase1-v1","currency":"USD",'
                '"value_band_boundaries":["5000","1000"],'
                '"bucket_map":{"1":"low","2":"low","3":"mid","4":"mid",'
                '"5":"high","6":"high","7":"high"},"extra":true}'
            ),
        ),
    ],
)
def test_nested_configuration_rejects_wrong_types_numbers_and_extra_fields(
    name: str, value: str
) -> None:
    with pytest.raises(RuntimeConfigurationError) as exc:
        Phase1RuntimeSettings.from_environ({**_VALID_ENV, name: value})
    assert exc.value.field_name == name
    assert "runtime-secret" not in str(exc.value)


def test_env_example_process_settings_are_non_runnable_placeholders() -> None:
    """样例复制后不得无意获得可直接启动的 host、port、间隔或并发默认值。"""
    env_path = Path(__file__).resolve().parents[2] / "infra" / ".env.example"
    values = {
        name: raw_value.split("#", 1)[0].strip()
        for line in env_path.read_text().splitlines()
        if line and not line.startswith("#") and "=" in line
        for name, raw_value in [line.split("=", 1)]
    }

    for name in (
        "API_HOST",
        "API_PORT",
        "SCHEDULER_INTERVAL_SECONDS",
        "AGENT_WORKER_CONCURRENCY",
    ):
        assert values[name].startswith("<")
        assert values[name].endswith(">")
    for name in (
        "API_PORT",
        "SCHEDULER_INTERVAL_SECONDS",
        "AGENT_WORKER_CONCURRENCY",
    ):
        with pytest.raises(ValueError):
            int(values[name])
