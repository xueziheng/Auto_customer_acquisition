"""API runtime factory 的纯净性、中间件、health 与脱敏失败。"""

from __future__ import annotations

import importlib
import logging
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apps.api.dependencies import UnconfiguredApiDependencies
from apps.api.main import create_app
from apps.api.middleware import ApiSettings
from apps.api.runtime_config import Phase1RuntimeSettings


class _ManualFacts:
    async def get_contact_eligibility(self, *args: object) -> object:
        del args
        return object()

    async def get_sending_identity_eligibility(self, *args: object) -> object:
        del args
        return object()

    async def get_campaign_approval(self, *args: object) -> object:
        del args
        return object()

    async def get_reply_status(self, *args: object) -> object:
        del args
        return object()


class _ManualMaterials:
    async def resolve(self, *args: object) -> object:
        del args
        return object()


class _ManualLinks:
    async def build(self, *args: object) -> str:
        del args
        return "https://unsubscribe.example.test/u/ref"


class _ManualSecrets:
    def __init__(self) -> None:
        self.refs: list[str] = []

    def resolve(self, secret_ref: str) -> str:
        self.refs.append(secret_ref)
        return "k" * 32


class _ManualTransport:
    async def search(self, **kwargs: object) -> None:
        del kwargs

    async def send(self, **kwargs: object) -> str:
        del kwargs
        return "gmail_ref_1"


class _Probe:
    def __init__(self, ready: bool) -> None:
        self.ready = ready

    async def is_ready(self) -> bool:
        return self.ready


def _unexpected_call(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("import/zero-arg create_app 不得创建 engine")


def test_import_and_zero_arg_app_do_not_read_environment_or_create_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "secret-marker")
    monkeypatch.setattr("sqlalchemy.ext.asyncio.create_async_engine", _unexpected_call)
    module = importlib.reload(importlib.import_module("apps.api.runtime"))
    app = create_app()
    assert module.create_runtime_app
    assert isinstance(app.state.dependencies, UnconfiguredApiDependencies)


def test_explicit_manual_send_composition_registers_real_gateway() -> None:
    module = importlib.import_module("apps.api.composition.runtime")
    settings = Phase1RuntimeSettings.from_environ(
        {
            "DATABASE_URL": "postgresql+asyncpg://db.invalid/tradeos",
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
                '"bucket_map":{"1":"low","2":"low","3":"mid",'
                '"4":"mid","5":"high","6":"high","7":"high"}}'
            ),
            "TRADEOS_OUTBOX_MAX_ATTEMPTS": "3",
            "GMAIL_OAUTH_TOKEN_REF": "gmail-oauth-phase1",
            "TOOL_CALL_FINGERPRINT_KEY_REF": "tool-fingerprint-phase1",
            "TOOL_CALL_FINGERPRINT_KEY_VERSION": "v1",
            "TRADEOS_UNSUBSCRIBE_BASE_URL": "https://unsubscribe.example.test",
            "TRADEOS_TOOL_LEASE_SECONDS": "120",
        }
    )
    facts = _ManualFacts()
    materials = _ManualMaterials()
    links = _ManualLinks()
    secrets = _ManualSecrets()
    manual = module.ManualSendComposition(
        contact_eligibility=facts,
        sending_identity_eligibility=facts,
        campaign_approvals=facts,
        reply_status=facts,
        delivery_materials=materials,
        unsubscribe_links=links,
        secret_resolver=secrets,
        gmail_transport=_ManualTransport(),
    )

    dependencies = module.build_phase1_dependencies(
        settings,
        object(),
        now=lambda: datetime.now(UTC),
        manual_send=manual,
    )

    assert dependencies.delivery_materials is materials
    assert dependencies.unsubscribe_links is links
    assert type(dependencies.tool_gateway).__name__ == "ResolvedManualSendGateway"
    assert secrets.refs == ["tool-fingerprint-phase1"]

    with pytest.raises(TypeError, match="API 手工发送依赖未完整配置"):
        module.build_phase1_dependencies(
            settings,
            object(),
            now=lambda: datetime.now(UTC),
            manual_send=replace(manual, delivery_materials=object()),
        )


@pytest.fixture
def runtime_app() -> FastAPI:
    return create_app(
        settings=ApiSettings(
            tenant_id="tenant-runtime",
            dev_mode=True,
            retry_after_seconds=30,
        ),
        cors_allowed_origins=("http://127.0.0.1:4173",),
        readiness_probe=_Probe(True),
    )


async def test_runtime_cors_allows_only_configured_origin(runtime_app: FastAPI) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=runtime_app), base_url="http://test"
    ) as client:
        allowed = await client.options(
            "/health/live",
            headers={
                "Origin": "http://127.0.0.1:4173",
                "Access-Control-Request-Method": "GET",
                "X-Tenant-Id": "tenant-runtime",
            },
        )
        denied = await client.options(
            "/health/live",
            headers={
                "Origin": "http://evil.invalid",
                "Access-Control-Request-Method": "GET",
                "X-Tenant-Id": "tenant-runtime",
            },
        )
    assert allowed.headers["access-control-allow-origin"] == (
        "http://127.0.0.1:4173"
    )
    assert "access-control-allow-origin" not in denied.headers


async def test_health_is_fixed_and_readiness_failure_is_503() -> None:
    app = create_app(
        settings=ApiSettings(
            tenant_id="tenant-runtime", dev_mode=True, retry_after_seconds=30
        ),
        readiness_probe=_Probe(False),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        live = await client.get(
            "/health/live", headers={"X-Tenant-Id": "tenant-runtime"}
        )
        ready = await client.get(
            "/health/ready", headers={"X-Tenant-Id": "tenant-runtime"}
        )
    assert live.status_code == 200
    assert live.json() == {"status": "live"}
    assert ready.status_code == 503
    assert ready.json() == {
        "code": "service_unavailable",
        "message": "服务暂时不可用",
    }


def test_runtime_user_middleware_order_keeps_safe_boundary_outermost(
    runtime_app: FastAPI,
) -> None:
    assert [middleware.cls.__name__ for middleware in runtime_app.user_middleware] == [
        "SafeUnhandledExceptionMiddleware",
        "CORSMiddleware",
        "TenantAssertionMiddleware",
    ]


def test_invalid_database_url_failure_and_log_are_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    module = importlib.import_module("apps.api.runtime")
    marker = "invalid-url-secret-marker"
    valid = {
        "DATABASE_URL": marker,
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
        "TRADEOS_TOOL_LEASE_SECONDS": "120",
    }
    monkeypatch.setattr(module.os, "environ", valid)
    with caplog.at_level(logging.ERROR), pytest.raises(
        module.RuntimeStartupError, match="API runtime 启动检查失败"
    ) as exc:
        module.create_runtime_app()
    assert marker not in str(exc.value)
    assert marker not in caplog.text
    assert all(marker not in str(record.__dict__) for record in caplog.records)


async def test_local_alembic_metadata_failure_is_mapped_without_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("apps.api.runtime")
    marker = "local-alembic-metadata-secret-marker"

    class _BrokenScriptDirectory:
        @classmethod
        def from_config(cls, _config: object) -> object:
            del cls
            raise ValueError(marker)

    monkeypatch.setattr(module, "ScriptDirectory", _BrokenScriptDirectory)

    with pytest.raises(
        module.RuntimeStartupError,
        match="API runtime 启动检查失败",
    ) as exc:
        await module.assert_database_schema_current(object())
    assert marker not in str(exc.value)
    assert exc.value.__cause__ is None
