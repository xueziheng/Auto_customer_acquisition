from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from connectors.object_store.config import S3ObjectStoreSettings
from shared.errors import PolicyViolation


def _production() -> dict[str, str]:
    return {
        "TRADEOS_DEV_MODE": "false",
        "S3_ENDPOINT": "https://objects.example.invalid",
        "S3_BUCKET_ARTIFACTS": "tradeos-artifacts",
        "S3_ACCESS_KEY_REF": "ARTIFACT_S3_ACCESS_KEY",
        "S3_SECRET_KEY_REF": "ARTIFACT_S3_SECRET_KEY",
        "S3_REGION": "us-east-1",
        "RAW_ARTIFACT_MAX_BYTES": "10485760",
        "GENERATED_ARTIFACT_MAX_BYTES": "1048576",
    }


def _assert_invalid(values: dict[str, object], marker: object | None = None) -> None:
    with pytest.raises(PolicyViolation, match="^Artifact Store 配置无效$") as exc:
        S3ObjectStoreSettings.from_environ(values)  # type: ignore[arg-type]
    if isinstance(marker, str) and marker:
        assert marker not in str(exc.value)
        assert marker not in repr(exc.value)


def test_production_config_is_explicit_frozen_and_repr_safe() -> None:
    settings = S3ObjectStoreSettings.from_environ(_production())
    assert settings.dev_mode is False
    assert settings.endpoint == "https://objects.example.invalid"
    assert settings.bucket == "tradeos-artifacts"
    assert settings.region == "us-east-1"
    assert settings.access_key_ref == "ARTIFACT_S3_ACCESS_KEY"
    assert settings.secret_key_ref == "ARTIFACT_S3_SECRET_KEY"
    assert settings.raw_max_bytes == 10_485_760
    assert settings.generated_max_bytes == 1_048_576
    assert repr(settings) == "S3ObjectStoreSettings(dev_mode=False)"
    with pytest.raises((AttributeError, FrozenInstanceError)):
        settings.region = "eu-west-1"  # type: ignore[misc]


def test_dev_mode_allows_only_loopback_http_with_explicit_port() -> None:
    for endpoint in (
        "http://127.0.0.1:19000",
        "http://localhost:19000",
        "http://[::1]:19000",
    ):
        values = _production() | {
            "TRADEOS_DEV_MODE": "true",
            "S3_ENDPOINT": endpoint,
        }
        settings = S3ObjectStoreSettings.from_environ(values)
        assert settings.dev_mode is True
        assert settings.endpoint == endpoint
        assert repr(settings) == "S3ObjectStoreSettings(dev_mode=True)"


@pytest.mark.parametrize("dev_mode", ["TRUE", "False", "1", "yes", "", True, 1])
def test_dev_mode_requires_canonical_lowercase_boolean(dev_mode: object) -> None:
    values: dict[str, object] = _production() | {"TRADEOS_DEV_MODE": dev_mode}
    _assert_invalid(values, dev_mode)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://objects.example.invalid",
        "http://objects.example.invalid:9000",
        "ftp://objects.example.invalid",
        "https://user:password@objects.example.invalid",
        "https://objects.example.invalid/path",
        "https://objects.example.invalid/",
        "https://objects.example.invalid?token=secret",
        "https://objects.example.invalid#fragment",
        "https://objects.example.invalid:0",
        "https://objects.example.invalid:65536",
        "https://objects.example.invalid:09000",
        "https://OBJECTS.example.invalid",
        " https://objects.example.invalid",
        "https://objects.example.invalid ",
    ],
)
def test_production_endpoint_rejects_noncanonical_or_sensitive_shapes(
    endpoint: str,
) -> None:
    _assert_invalid(_production() | {"S3_ENDPOINT": endpoint}, endpoint)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://10.0.0.1:9000",
        "http://objects.example.invalid:9000",
        "http://127.0.0.1",
        "http://localhost",
        "http://[::1]",
        "http://127.0.0.1:09000",
        "http://127.0.0.1:0",
        "http://127.0.0.1:65536",
    ],
)
def test_dev_http_endpoint_rejects_remote_missing_or_bad_port(endpoint: str) -> None:
    _assert_invalid(
        _production()
        | {"TRADEOS_DEV_MODE": "true", "S3_ENDPOINT": endpoint},
        endpoint,
    )


@pytest.mark.parametrize(
    "bucket",
    [
        "ab",
        "A-bucket",
        "bucket_name",
        "-bucket",
        "bucket-",
        ".bucket",
        "bucket.",
        "bucket..name",
        "192.168.0.1",
        "a" * 64,
        " bucket",
    ],
)
def test_bucket_requires_canonical_dns_safe_name(bucket: str) -> None:
    _assert_invalid(_production() | {"S3_BUCKET_ARTIFACTS": bucket}, bucket)


@pytest.mark.parametrize(
    "region",
    ["US-EAST-1", "us_east_1", "us-east", "-us-east-1", "us-east-1-", " us-east-1"],
)
def test_region_requires_canonical_aws_shape(region: str) -> None:
    _assert_invalid(_production() | {"S3_REGION": region}, region)


@pytest.mark.parametrize("name", ["S3_ACCESS_KEY_REF", "S3_SECRET_KEY_REF"])
@pytest.mark.parametrize(
    "secret_ref",
    [
        "artifact_secret",
        "1_ARTIFACT_SECRET",
        "ARTIFACT-SECRET",
        "${ARTIFACT_SECRET}",
        "/run/secrets/artifact",
        "A" * 129,
        " ARTIFACT_SECRET",
    ],
)
def test_secret_refs_must_be_environment_variable_names(
    name: str, secret_ref: str
) -> None:
    _assert_invalid(_production() | {name: secret_ref}, secret_ref)


@pytest.mark.parametrize(
    "size",
    ["0", "-1", "+1", "01", "1.0", "true", "NaN", "Infinity", "9223372036854775808", 1, True],
)
@pytest.mark.parametrize(
    "name", ["RAW_ARTIFACT_MAX_BYTES", "GENERATED_ARTIFACT_MAX_BYTES"]
)
def test_byte_limits_require_positive_canonical_int64(
    name: str, size: object
) -> None:
    values: dict[str, object] = _production() | {name: size}
    _assert_invalid(values, size)


@pytest.mark.parametrize("name", tuple(_production()))
def test_each_setting_is_required_and_nonblank(name: str) -> None:
    missing = _production()
    marker = missing.pop(name)
    _assert_invalid(missing, marker)
    _assert_invalid(_production() | {name: ""})
    _assert_invalid(_production() | {name: " "})


@pytest.mark.parametrize("name", tuple(_production()))
def test_each_setting_rejects_non_string_objects(name: str) -> None:
    for value in (None, object(), [], {}):
        values: dict[str, object] = _production() | {name: value}
        _assert_invalid(values)


def test_settings_copy_inputs_and_ignore_unrelated_environment_values() -> None:
    values = _production() | {"UNRELATED_CREDENTIAL_MARKER": "raw-secret-marker"}
    settings = S3ObjectStoreSettings.from_environ(values)
    values["S3_ENDPOINT"] = "https://mutated.example.invalid"
    values["S3_BUCKET_ARTIFACTS"] = "mutated-bucket"
    rendered = repr(settings)
    assert settings.endpoint == "https://objects.example.invalid"
    assert settings.bucket == "tradeos-artifacts"
    assert rendered == "S3ObjectStoreSettings(dev_mode=False)"
    assert "raw-secret-marker" not in rendered
    assert "objects.example.invalid" not in rendered
    assert "tradeos-artifacts" not in rendered
