"""发件身份 DTO、仓储和公开服务签名契约。"""

from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from domains.sending_identity import errors, models, schemas, service
from shared.schemas.identifiers import IdempotencyKey, SendingIdentityId, TenantId


def test_authentication_result_requires_exactly_one_failure_for_each_failed_check() -> None:
    """漏失失败项会把未认证身份误判为已认证。"""
    failure = schemas.AuthenticationFailure(
        check=models.AuthCheck.SPF,
        category=models.AuthenticationFailureCategory.RECORD_MISSING,
        instruction=models.AuthenticationFixInstruction.CONFIGURE_SPF,
    )
    result = schemas.AuthenticationResult(
        checked_at=datetime(2026, 8, 10, tzinfo=UTC),
        spf_passed=False,
        dkim_passed=True,
        dmarc_passed=True,
        failures=(failure,),
        check_ref="auth-check-1",
    )
    assert not result.all_passed
    with pytest.raises(errors.InvalidAuthenticationResultError):
        schemas.AuthenticationResult(
            checked_at=datetime(2026, 8, 10, tzinfo=UTC),
            spf_passed=False,
            dkim_passed=True,
            dmarc_passed=True,
            failures=(),
            check_ref="auth-check-1",
        )
    with pytest.raises(errors.InvalidAuthenticationResultError):
        schemas.AuthenticationResult(
            checked_at=datetime(2026, 8, 10, tzinfo=UTC),
            spf_passed=True,
            dkim_passed=True,
            dmarc_passed=True,
            failures=(failure,),
            check_ref="auth-check-1",
        )


def test_dtos_require_utc_and_real_booleans() -> None:
    """天真时间或 0/1 布尔值会让认证状态可被伪造。"""
    with pytest.raises(errors.InvalidAuthenticationResultError):
        schemas.AuthenticationResult(
            checked_at=datetime(2026, 8, 10),  # noqa: DTZ001 - 验证拒绝 naive datetime
            spf_passed=True,
            dkim_passed=True,
            dmarc_passed=True,
            failures=(),
            check_ref="auth-check-1",
        )
    with pytest.raises(errors.InvalidAuthenticationResultError):
        schemas.AuthenticationResult(
            checked_at=datetime(2026, 8, 10, tzinfo=UTC),
            spf_passed=1,  # type: ignore[arg-type]
            dkim_passed=True,
            dmarc_passed=True,
            failures=(),
            check_ref="auth-check-1",
        )


def test_delivery_event_requires_typed_event_and_safe_utc_metadata() -> None:
    """自由字符串事件或非 UTC 时间会污染滚动信誉窗口。"""
    event = schemas.DeliveryEventRecord(
        tenant_id=TenantId("tenant-1"),
        identity_id=SendingIdentityId("sid-1"),
        event_type=models.DeliveryEventType.DELIVERED,
        occurred_at=datetime(2026, 8, 10, tzinfo=UTC),
        dedup_key=IdempotencyKey("provider:message-1"),
        source_ref="gmail-event-1",
    )
    assert event.event_type is models.DeliveryEventType.DELIVERED
    with pytest.raises(errors.InvalidDeliveryEventError):
        schemas.DeliveryEventRecord(
            tenant_id=TenantId("tenant-1"),
            identity_id=SendingIdentityId("sid-1"),
            event_type="delivered",  # type: ignore[arg-type]
            occurred_at=datetime(2026, 8, 10, tzinfo=UTC),
            dedup_key=IdempotencyKey("provider:message-1"),
            source_ref="gmail-event-1",
        )


def test_requests_and_records_are_frozen_typed_contracts() -> None:
    """可变 DTO 或自由 role 会让已判权的内容在执行前被替换。"""
    request = schemas.IdentityRegisterRequest(
        address="sales@example.com",
        domain="example.com",
        role=models.DomainRole.COLD_OUTREACH,
        connector_ref="gmail-ref-1",
    )
    with pytest.raises(FrozenInstanceError):
        request.domain = "other.example"  # type: ignore[misc]
    with pytest.raises(TypeError):
        schemas.IdentityRegisterRequest(  # type: ignore[arg-type]
            address="sales@example.com",
            domain="example.com",
            role="cold_outreach",
        )


def test_reputation_view_rejects_float_rate() -> None:
    """API 视图接受 float 会把确定性信誉阈值重新变成二进制近似。"""
    with pytest.raises(TypeError):
        schemas.ReputationView(
            window_days=7,
            sent_attempts=100,
            delivered=99,
            hard_bounce_rate=0.01,  # type: ignore[arg-type]
            complaint_rate=Decimal(".001"),
            delivery_rate=Decimal(".99"),
            computed_at=datetime(2026, 8, 10, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    ("method", "parameters"),
    [
        ("register", ("self", "tenant_id", "request", "actor")),
        ("begin_authentication", ("self", "tenant_id", "identity_id", "actor")),
        (
            "record_authentication_result",
            ("self", "tenant_id", "identity_id", "result", "actor"),
        ),
        ("start_warmup", ("self", "tenant_id", "identity_id", "target_daily_volume", "actor")),
        ("advance_warmup", ("self", "tenant_id", "identity_id", "actor")),
        ("check_send_permission", ("self", "tenant_id", "identity_id", "for_cold_outreach", "actor")),
        (
            "reserve_send_slot",
            ("self", "tenant_id", "identity_id", "reservation_key", "for_cold_outreach", "actor"),
        ),
        ("record_delivery_event", ("self", "tenant_id", "identity_id", "event", "actor")),
        ("evaluate_reputation", ("self", "tenant_id", "identity_id", "actor")),
        ("resume_from_throttle", ("self", "tenant_id", "identity_id", "actor")),
        (
            "resume_from_suspension",
            ("self", "tenant_id", "identity_id", "investigation_note", "actor"),
        ),
        ("retire", ("self", "tenant_id", "identity_id", "reason", "actor")),
        ("get", ("self", "tenant_id", "identity_id", "actor")),
        ("list_available_for_campaign", ("self", "tenant_id", "limit", "actor")),
        ("get_domain_reputation", ("self", "tenant_id", "domain", "actor")),
        ("get_warmup_progress", ("self", "tenant_id", "identity_id", "actor")),
    ],
)
def test_public_service_signature_is_typed_and_has_no_client_time_bypass(
    method: str, parameters: tuple[str, ...]
) -> None:
    """客户端日期或审批旁路参数会绕过预热和人工恢复门禁。"""
    signature = inspect.signature(getattr(service.SendingIdentityService, method))
    assert tuple(signature.parameters) == parameters
    assert not {"started_on", "on_day", "computed_at", "skip_warmup", "approved_by"} & set(
        signature.parameters
    )


def test_public_service_reexports_only_public_contract_types() -> None:
    """其他域需要经 service 消费 typed DTO，而不是碰内部模型。"""
    assert service.AuthenticationResult is schemas.AuthenticationResult
    assert service.DeliveryEventRecord is schemas.DeliveryEventRecord
    assert service.SendReservation is schemas.SendReservation
    assert service.Actor.__module__ == "domains.sending_identity.permissions"
