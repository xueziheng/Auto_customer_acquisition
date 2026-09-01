"""Sourcing V2 scheduler 的严格可选配置与安全装配契约。"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from apps.scheduler_worker.config import SchedulerWorkerConfig
from shared.errors import ValidationError
from shared.events.catalog import (
    DomainEvent,
    OpportunityQualified,
    SourcingCandidatesReady,
    SourcingCaseOpened,
)
from shared.schemas.identifiers import (
    SourcingCaseId,
    SourcingSupplyOptionId,
    SupplierCandidateId,
    TenantId,
)


def _base_environ() -> dict[str, str]:
    return {
        "DATABASE_URL": "postgresql+asyncpg://user:password@example.invalid/db",
        "TRADEOS_TENANT_ID": "tn_01K2C5R6J7ABCDEFGHJKMNPQRS",
        "TRADEOS_SCHEDULER_INTERVAL_SECONDS": "5",
        "TRADEOS_SCHEDULER_BATCH_LIMIT": "20",
        "TRADEOS_SCHEDULER_LOCK_KEY": "3110001",
        "TRADEOS_SCHEDULER_OUTBOX_MAX_ATTEMPTS": "3",
        "TRADEOS_HANDOFF_T1_SECONDS": "3600",
        "TRADEOS_HANDOFF_T2_SECONDS": "7200",
        "TRADEOS_DKIM_SELECTOR": "s1",
        "TRADEOS_SCHEDULER_HEALTH_PORT": "8094",
        "TRADEOS_TOOL_LEASE_SECONDS": "120",
        "TOOL_CALL_FINGERPRINT_KEY_REF": "SCHEDULER_FINGERPRINT_KEY",
        "TOOL_CALL_FINGERPRINT_KEY_VERSION": "v1",
        "TRADEOS_CAMPAIGN_RETRY_INTERVAL_SECONDS": "30",
        "GMAIL_OAUTH_TOKEN_REF": "GMAIL_OAUTH_TOKEN_REF",
        "TRADEOS_EMAIL_FEEDBACK_ROUTE_ID": "route-scheduler",
        "TRADEOS_UNSUBSCRIBE_BASE_URL": "https://unsub.example",
        "TRADEOS_UNSUBSCRIBE_ACTIVE_KEY_ID": "k1",
        "TRADEOS_UNSUBSCRIBE_KEY_REFS_JSON": '{"k1": "UNSUBSCRIBE_HMAC_CURRENT"}',
        "TRADEOS_HUNTER_CONTACTS_ENABLED": "false",
    }


def test_sourcing_runtime_is_disabled_without_explicit_settings() -> None:
    """删掉 sourcing 配置时必须回到旧 scheduler 的完全禁用状态。"""

    config = SchedulerWorkerConfig.from_environ(_base_environ())

    assert config.sourcing is None


def test_exact_disabled_sourcing_settings_reject_companion_fields() -> None:
    environ = _base_environ()
    environ["TRADEOS_SOURCING_SETTINGS_JSON"] = '{"enabled": false}'

    assert SchedulerWorkerConfig.from_environ(environ).sourcing is None

    for extra in (
        {"model_identifier": "gpt-sourcing-v1"},
        {"target_country": "US"},
        {"research_budget": 3},
    ):
        environ["TRADEOS_SOURCING_SETTINGS_JSON"] = json.dumps(
            {"enabled": False, **extra}
        )
        with pytest.raises(
            ValidationError, match="scheduler worker \u914d\u7f6e\u65e0\u6548"
        ):
            SchedulerWorkerConfig.from_environ(environ)


def test_enabled_sourcing_settings_accept_only_fixed_safe_deployment_fields() -> None:
    environ = _base_environ()
    environ["TRADEOS_SOURCING_SETTINGS_JSON"] = json.dumps(
        {
            "enabled": True,
            "model_identifier": "gpt-sourcing-v1",
            "tavily_secret_ref": "TAVILY_API_KEY_PROD",
            "max_search_queries_per_plan": 8,
            "max_pages_per_plan": 24,
            "system_actor_id": "system:sourcing-v2",
        }
    )

    settings = SchedulerWorkerConfig.from_environ(environ).sourcing

    assert settings is not None
    assert settings.model_identifier == "gpt-sourcing-v1"
    assert settings.tavily_secret_ref == "TAVILY_API_KEY_PROD"
    assert settings.max_search_queries_per_plan == 8
    assert settings.max_pages_per_plan == 24
    assert settings.system_actor_id == "system:sourcing-v2"
    assert "TAVILY_API_KEY_PROD" not in repr(settings)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"enabled": True},
        {
            "enabled": True,
            "model_identifier": "gpt-sourcing-v1",
            "tavily_secret_ref": "",
            "max_search_queries_per_plan": 8,
            "max_pages_per_plan": 24,
            "system_actor_id": "system:sourcing-v2",
        },
        {
            "enabled": True,
            "model_identifier": " ",
            "tavily_secret_ref": "TAVILY_API_KEY_PROD",
            "max_search_queries_per_plan": 8,
            "max_pages_per_plan": 24,
            "system_actor_id": "system:sourcing-v2",
        },
        {
            "enabled": True,
            "model_identifier": "gpt-sourcing-v1",
            "tavily_secret_ref": "TAVILY_API_KEY_PROD",
            "max_search_queries_per_plan": 0,
            "max_pages_per_plan": 24,
            "system_actor_id": "system:sourcing-v2",
        },
        {
            "enabled": True,
            "model_identifier": "gpt-sourcing-v1",
            "tavily_secret_ref": "TAVILY_API_KEY_PROD",
            "max_search_queries_per_plan": 21,
            "max_pages_per_plan": 24,
            "system_actor_id": "system:sourcing-v2",
        },
        {
            "enabled": True,
            "model_identifier": "gpt-sourcing-v1",
            "tavily_secret_ref": "TAVILY_API_KEY_PROD",
            "max_search_queries_per_plan": 8,
            "max_pages_per_plan": 51,
            "system_actor_id": "system:sourcing-v2",
        },
        {
            "enabled": True,
            "model_identifier": "gpt-sourcing-v1",
            "tavily_secret_ref": "TAVILY_API_KEY_PROD",
            "max_search_queries_per_plan": 8,
            "max_pages_per_plan": 24,
            "system_actor_id": "system:sourcing-v2",
            "target_countries": ["US"],
        },
        {
            "enabled": True,
            "model_identifier": "gpt-sourcing-v1",
            "tavily_secret_ref": "TAVILY_API_KEY_PROD",
            "max_search_queries_per_plan": 8,
            "max_pages_per_plan": 24,
            "system_actor_id": "system:sourcing-v2",
            "budget": 5,
        },
    ],
)
def test_sourcing_settings_fail_closed_on_missing_unknown_or_unsafe_values(
    payload: dict[str, object],
) -> None:
    environ = _base_environ()
    environ["TRADEOS_SOURCING_SETTINGS_JSON"] = json.dumps(payload)

    with pytest.raises(
        ValidationError, match="scheduler worker \u914d\u7f6e\u65e0\u6548"
    ):
        SchedulerWorkerConfig.from_environ(environ)


def test_sourcing_settings_reject_duplicate_json_keys() -> None:
    environ = _base_environ()
    environ["TRADEOS_SOURCING_SETTINGS_JSON"] = (
        '{"enabled":true,"enabled":true,"model_identifier":"gpt-sourcing-v1",'
        '"tavily_secret_ref":"TAVILY_API_KEY_PROD",'
        '"max_search_queries_per_plan":8,"max_pages_per_plan":24,'
        '"system_actor_id":"system:sourcing-v2"}'
    )

    with pytest.raises(
        ValidationError, match="scheduler worker \u914d\u7f6e\u65e0\u6548"
    ):
        SchedulerWorkerConfig.from_environ(environ)


def test_ready_acknowledgement_is_type_and_tenant_only() -> None:
    from apps.scheduler_worker.sourcing_runtime import (
        SourcingCandidatesReadyAuditAcknowledgement,
    )

    tenant = TenantId("tn_01K2C5R6J7ABCDEFGHJKMNPQRS")
    ack = SourcingCandidatesReadyAuditAcknowledgement(tenant)
    event = SourcingCandidatesReady(
        tenant_id=tenant,
        occurred_at=datetime(2026, 8, 31, tzinfo=UTC),
        case_id=SourcingCaseId("src_01K2C5R6J7ABCDEFGHJKMNPQRS"),
        option_ids=(SourcingSupplyOptionId("sop_01K2C5R6J7ABCDEFGHJKMNPQRS"),),
        candidate_ids=(SupplierCandidateId("spc_01K2C5R6J7ABCDEFGHJKMNPQRS"),),
    )

    import asyncio

    async def consume() -> None:
        await ack.handle(event)
        with pytest.raises(ValidationError, match="SourcingCandidatesReady"):
            await ack.handle(object())

    asyncio.run(consume())


@pytest.mark.parametrize(
    "event_type",
    [
        SourcingCaseOpened,
        OpportunityQualified,
    ],
)
def test_lifecycle_audit_acknowledgement_is_explicit_type_and_tenant_only(
    event_type: type[DomainEvent],
) -> None:
    """阶段确认只接收已注册的事实，不能成为全局 no-handler 旁路。"""

    from apps.scheduler_worker.sourcing_runtime import (
        SourcingLifecycleAuditAcknowledgement,
    )

    tenant = TenantId("tn_01K2C5R6J7ABCDEFGHJKMNPQRS")
    ack = SourcingLifecycleAuditAcknowledgement(tenant, event_type)

    import asyncio

    async def consume() -> None:
        await ack.handle(
            event_type(
                tenant_id=tenant,
                occurred_at=datetime(2026, 8, 31, tzinfo=UTC),
            )
        )
        with pytest.raises(ValidationError, match=event_type.__name__):
            await ack.handle(
                SourcingCandidatesReady(
                    tenant_id=tenant,
                    occurred_at=datetime(2026, 8, 31, tzinfo=UTC),
                )
            )

    asyncio.run(consume())


def test_production_quote_evidence_reader_is_explicitly_fail_closed() -> None:
    from apps.scheduler_worker.sourcing_runtime import (
        FailClosedSupplierQuoteEvidenceReader,
    )

    reader = FailClosedSupplierQuoteEvidenceReader()

    async def read() -> None:
        with pytest.raises(ValidationError) as captured:
            await reader.read_verified(
                TenantId("tn_01K2C5R6J7ABCDEFGHJKMNPQRS"),
                "art_01K2C5R6J7ABCDEFGHJKMNPQRS",  # type: ignore[arg-type]
            )
        assert str(captured.value) == "直接供应商报价证据存储尚未装配"
        assert captured.value.__cause__ is None

    import asyncio

    asyncio.run(read())


@pytest.mark.parametrize(
    ("url", "accepted"),
    [
        ("https://supplier.example/products/hinge", True),
        ("https://supplier.example:443/products/hinge", True),
        ("http://supplier.example:80/products/hinge", True),
        ("https://supplier.example:8443/products/hinge", False),
        ("https://user@supplier.example/products/hinge", False),
        ("https://supplier.example/products/hinge#contact", False),
        ("https://supplier.example/products/\x01hinge", False),
        ("http://127.0.0.1/private", False),
        ("http://localhost/private", False),
        ("http://supplier.local/private", False),
        ("http://127.1/private", False),
        ("http://2130706433/private", False),
        ("http://0x7f000001/private", False),
        ("http://0177.0.0.1/private", False),
        ("http://[::1]/private", False),
        ("file:///etc/passwd", False),
    ],
)
def test_candidate_artifact_url_shape_matches_gateway_public_boundary(
    url: str, accepted: bool
) -> None:
    """Reader-local URL rules must not invert Gateway DNS and port semantics."""

    from apps.scheduler_worker.sourcing_runtime import _safe_public_url

    assert _safe_public_url(url) is accepted
