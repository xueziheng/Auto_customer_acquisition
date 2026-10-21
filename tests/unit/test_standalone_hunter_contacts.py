"""部署配置只读取显式元数据；密钥延迟到具名 resolver 调用。"""

import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def settings_module():
    try:
        return importlib.import_module("infra.standalone.hunter_settings")
    except ModuleNotFoundError:
        pytest.fail("Hunter 客户验证显式配置尚未实现")


def composition_module():
    try:
        return importlib.import_module("apps.api.composition.standalone_contacts")
    except ModuleNotFoundError:
        pytest.fail("Hunter 客户验证部署装配尚未实现")


def metadata(path: Path):
    return {
        "enabled": True,
        "business_tenant_id": "tn_fixture",
        "configuration_version": "fixture-v1",
        "api_key_version": "fixture-key-v1",
        "secret_file": str(path),
        "maximum_daily_verifications": 1,
    }


def platform_metadata(path: Path):
    return {
        "enabled": True,
        "business_tenant_id": "tn_fixture",
        "configuration_version": "hunter-platform-rev1",
        "api_key_version": "platform-rev1",
        "credential_source": "platform",
        "source_directory": str(path),
        "control_tenant_id": "tn_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "source_revision": 1,
        "maximum_daily_verifications": 1,
    }


def test_platform_metadata_loads_without_accessing_referenced_repository(tmp_path):
    module = settings_module()
    tmp_path.chmod(0o700)
    path = tmp_path / "settings.json"
    source = tmp_path / "not-created-data-sources"
    value = platform_metadata(source)
    value.pop("enabled")
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    settings = module.load_hunter_customer_settings(path)
    assert settings.credential_source == "platform" and settings.secret_file is None
    assert not settings.enabled and not source.exists()
    assert str(source) not in repr(settings)


@pytest.mark.parametrize(
    "patch",
    [
        {"secret_file": "/private/fallback-key"},
        {"credential_source": "private_file"},
        {"credential_source": "unknown"},
        {"source_directory": None},
        {"source_directory": "relative"},
        {"source_directory": "/private/../sources"},
        {"source_directory": "/"},
        {"control_tenant_id": None},
        {"control_tenant_id": "tn_fixture"},
        {"control_tenant_id": " tn_01ARZ3NDEKTSV4RRFFQ69G5FAV"},
        {"source_revision": None},
        {"source_revision": 0},
        {"source_revision": True},
        {"source_revision": "1"},
        {"configuration_version": "hunter-test-20261011-v1"},
        {"api_key_version": "test-20261011-v1"},
        {"source_revision": 2},
    ],
)
def test_platform_metadata_requires_exclusive_exact_revision_binding(tmp_path, patch):
    with pytest.raises(ValueError):
        settings_module().HunterCustomerSettings.model_validate(
            {**platform_metadata(tmp_path / "missing"), **patch}
        )


@pytest.mark.parametrize(
    "patch",
    [
        {"secret_file": None},
        {"source_directory": "/private/platform-data-sources"},
        {"control_tenant_id": "tn_01ARZ3NDEKTSV4RRFFQ69G5FAV"},
        {"source_revision": 1},
    ],
)
def test_private_file_metadata_rejects_missing_file_or_platform_fields(tmp_path, patch):
    with pytest.raises(ValueError):
        settings_module().HunterCustomerSettings.model_validate(
            {**metadata(tmp_path / "missing"), **patch}
        )


def test_platform_resolver_is_lazy_exact_and_rejects_rotation_without_fallback(
    tmp_path, monkeypatch
):
    from pydantic import SecretStr

    module = composition_module()
    source = tmp_path / "data-sources"
    settings = settings_module().HunterCustomerSettings.model_validate(
        platform_metadata(source)
    )
    reads = []
    revision = 1

    class Repository:
        def resolve_credential(self, tenant_id, source_id, field_name, *, revision):
            reads.append((tenant_id, source_id, field_name, revision))
            if revision != current_revision():
                raise ValueError("unsafe fixture exception")
            return SecretStr("synthetic-hunter-fixture")

    def current_revision():
        return revision

    def repository_factory(directory, tenant_id):
        assert directory == source
        assert tenant_id == "tn_01ARZ3NDEKTSV4RRFFQ69G5FAV"
        return Repository()

    def forbidden_file_read(*args, **kwargs):
        pytest.fail("平台来源不得读取或回退私有密钥文件")

    monkeypatch.setattr("infra.hunter_customer_credentials.private_read", forbidden_file_read)
    resolver = module.PlatformHunterCustomerSecretResolver(
        settings, repository_factory=repository_factory
    )
    assert reads == [] and str(source) not in repr(resolver)
    with pytest.raises(ValueError, match="Hunter 凭证引用无效"):
        resolver.resolve("WRONG_REFERENCE")
    assert reads == []
    assert resolver.resolve("HUNTER_CUSTOMER_KEY") == "synthetic-hunter-fixture"
    assert reads == [("tn_01ARZ3NDEKTSV4RRFFQ69G5FAV", "hunter", "api_key", 1)]
    revision = 2
    with pytest.raises(ValueError, match="^Hunter 凭证读取失败$"):
        resolver.resolve("HUNTER_CUSTOMER_KEY")
    assert reads[-1][-1] == 1


def test_metadata_load_never_reads_referenced_key_and_defaults_disabled(tmp_path):
    module = settings_module()
    tmp_path.chmod(0o700)
    path = tmp_path / "settings.json"
    key_path = tmp_path / "intentionally-absent-key"
    path.write_text(json.dumps(metadata(key_path)))
    path.chmod(0o600)
    value = module.load_hunter_customer_settings(path)
    assert value.enabled and value.maximum_daily_verifications == 1
    assert not key_path.exists()
    assert str(key_path) not in repr(value)
    assert (
        module.HunterCustomerSettings.model_validate(
            {**metadata(key_path), "enabled": False}
        ).enabled
        is False
    )
    assert (
        module.HunterCustomerSettings.model_validate(
            {k: v for k, v in metadata(key_path).items() if k != "enabled"}
        ).enabled
        is False
    )
    assert module.load_hunter_customer_settings(None) is None


@pytest.mark.parametrize(
    "patch",
    [
        {"enabled": "true"},
        {"maximum_daily_verifications": 0},
        {"maximum_daily_verifications": True},
        {"maximum_daily_verifications": 1.1},
        {"secret_file": "relative/key"},
        {"secret_file": "/private/../key"},
        {"configuration_version": "unsafe@version"},
        {"business_tenant_id": ""},
        {"base_url": "https://wrong.test"},
    ],
)
def test_metadata_rejects_invalid_boundaries_without_key_io(tmp_path, patch):
    with pytest.raises(ValueError):
        settings_module().HunterCustomerSettings.model_validate(
            {**metadata(tmp_path / "missing"), **patch}
        )


def test_loader_and_lazy_resolver_reject_unsafe_files_without_exposing_values(tmp_path):
    module, composition = settings_module(), composition_module()
    tmp_path.chmod(0o700)
    missing = tmp_path / "missing-key"
    resolver = composition.HunterCustomerSecretResolver(missing)
    assert str(missing) not in repr(resolver)
    with pytest.raises(ValueError, match="Hunter"):
        resolver.resolve("WRONG_REFERENCE")
    assert not missing.exists()
    secret = "fixture-only-never-production"
    missing.write_text(secret)
    missing.chmod(0o600)
    assert resolver.resolve("HUNTER_CUSTOMER_KEY") == secret
    missing.chmod(0o644)
    with pytest.raises(ValueError) as caught:
        resolver.resolve("HUNTER_CUSTOMER_KEY")
    assert secret not in str(caught.value) and str(missing) not in str(caught.value)
    path = tmp_path / "invalid.json"
    path.write_text(secret)
    path.chmod(0o600)
    with pytest.raises(ValueError) as caught:
        module.load_hunter_customer_settings(path)
    assert secret not in str(caught.value)


@pytest.mark.parametrize("credential_source", ["private_file", "platform"])
def test_real_employee_actor_builder_has_explicit_tenant_and_zero_io(
    tmp_path, monkeypatch, credential_source
):
    from domains.employees.permissions import Actor, EmployeeScope
    from shared.schemas.identifiers import TenantId, UserId
    from tool_gateway.fingerprint import HmacFingerprintProvider

    module = composition_module()

    def forbidden_io(*args, **kwargs):
        pytest.fail("构造不得访问凭证、数据库或网络")

    monkeypatch.setattr("infra.hunter_customer_credentials.private_read", forbidden_io)
    monkeypatch.setattr("infra.hunter_customer_credentials.FileDataSourceConfigurationRepository", forbidden_io)
    dependencies = SimpleNamespace(
        employee_lookup_actor=Actor(
            UserId("usr_fixture"), EmployeeScope.SYSTEM, "system"
        ),
        prospecting=object(),
        outreach=object(),
        demand_service=object(),
        compliance=object(),
    )
    settings = settings_module().HunterCustomerSettings.model_validate(
        platform_metadata(tmp_path / "absent-directory")
        if credential_source == "platform"
        else metadata(tmp_path / "absent-key")
    )
    app = module.build_standalone_customer_verification(
        forbidden_io,
        dependencies,
        tenant_id=TenantId("tn_fixture"),
        settings=settings,
        fingerprints=HmacFingerprintProvider("fixture-v1", b"f" * 32),
    )
    assert callable(app.verify) and callable(app.startup)
    with pytest.raises(Exception, match="企业配置不一致"):
        module.build_standalone_customer_verification(
            forbidden_io,
            dependencies,
            tenant_id=TenantId("tn_other"),
            settings=settings,
            fingerprints=HmacFingerprintProvider("fixture-v1", b"f" * 32),
        )
