"""Tool Gateway 安全调用类型与结构化错误契约。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from shared.errors import ValidationError
from shared.schemas.identifiers import IdempotencyKey, TenantId, UserId, new_id
from tool_gateway.errors import (
    DeliveryCertainty,
    ToolCallStatus,
    ToolErrorCategory,
    ToolGatewayError,
)
from tool_gateway.pipeline import (
    CheckRejection,
    PreparedToolCall,
    ToolCallContext,
    ToolCallResult,
)


def test_error_categories_and_delivery_certainty_cover_safe_recovery_paths() -> None:
    """删除待对账或确定未发送分类会迫使上层把不确定结果当普通重试。"""
    assert {item.value for item in ToolErrorCategory} == {
        "validation",
        "permission_denied",
        "suppressed",
        "approval_required",
        "idempotency_conflict",
        "in_progress",
        "rate_limited",
        "provider_auth_required",
        "provider_permanent",
        "provider_transient",
        "reconciliation_required",
        "unexpected",
    }
    assert {item.value for item in DeliveryCertainty} == {
        "sent",
        "definitely_not_sent",
        "unknown",
    }


def test_tool_gateway_error_keeps_only_typed_safe_metadata() -> None:
    """自由 context 或原始异常文本会把客户内容和凭证带进日志。"""
    error = ToolGatewayError(
        ToolErrorCategory.RATE_LIMITED,
        retry_after_seconds=30,
    )
    assert str(error) == "工具调用失败"
    assert error.category is ToolErrorCategory.RATE_LIMITED
    assert error.retry_after_seconds == 30
    assert error.is_retryable
    assert error.context == {}
    assert "30" not in str(error)


@pytest.mark.parametrize(
    ("category", "retryable"),
    [
        (ToolErrorCategory.PROVIDER_TRANSIENT, True),
        (ToolErrorCategory.RECONCILIATION_REQUIRED, True),
        (ToolErrorCategory.PROVIDER_AUTH_REQUIRED, False),
        (ToolErrorCategory.PROVIDER_PERMANENT, False),
        (ToolErrorCategory.PERMISSION_DENIED, False),
    ],
)
def test_tool_gateway_error_retryability_is_derived_from_category(
    category: ToolErrorCategory, retryable: bool
) -> None:
    """调用方不能通过构造参数把权限或永久错误伪装成可重试。"""
    assert ToolGatewayError(category).is_retryable is retryable


@pytest.mark.parametrize("retry_after", [0, -1, True, 86_401, "30"])
def test_tool_gateway_error_rejects_invalid_retry_after(retry_after: object) -> None:
    """无界或错类型 Retry-After 会导致客户端热循环或长期冻结。"""
    with pytest.raises(ValidationError):
        ToolGatewayError(
            ToolErrorCategory.RATE_LIMITED,
            retry_after_seconds=retry_after,  # type: ignore[arg-type]
        )


def test_prepared_call_defensively_copies_safe_projection_and_hides_payload() -> None:
    """调用方不能在 prepare 后篡改审计投影，payload 也不能出现在 repr。"""
    projection: dict[str, str | int | bool | None] = {
        "attempt_id": "mat_01K00000000000000000000000",
        "subject_bytes": 18,
        "has_unsubscribe": True,
    }
    payload = object()
    prepared = PreparedToolCall(
        request_fingerprint="a" * 64,
        fingerprint_version="fp-v1",
        audit_projection=projection,
        payload=payload,
    )
    projection["subject_bytes"] = 999
    assert prepared.audit_projection["subject_bytes"] == 18
    assert prepared.payload is payload
    assert "object at" not in repr(prepared)
    with pytest.raises(FrozenInstanceError):
        prepared.request_fingerprint = "b" * 64


@pytest.mark.parametrize(
    "projection",
    [
        {"recipient": "buyer@example.com"},
        {"subject": "secret subject"},
        {"body_bytes": {}},
        {"safe": ["nested"]},
        {"authorization_type": "bearer"},
        {"api_key": "opaque-value"},
        {"oauth_credential": "opaque-value"},
        {"has_url": True},
        {"note": "buyer@example.com"},
        {"note": "secret customer text"},
        {"safe\nkey": "value"},
    ],
)
def test_prepared_call_rejects_sensitive_keys_and_non_scalar_values(
    projection: dict[str, object]
) -> None:
    """敏感字段名或嵌套值不得穿过 durable audit projection 边界。"""
    with pytest.raises(ValidationError):
        PreparedToolCall(
            request_fingerprint="a" * 64,
            fingerprint_version="fp-v1",
            audit_projection=projection,  # type: ignore[arg-type]
            payload=object(),
        )


@pytest.mark.parametrize(
    ("fingerprint", "version"),
    [
        ("A" * 64, "fp-v1"),
        ("a" * 63, "fp-v1"),
        ("g" * 64, "fp-v1"),
        ("a" * 64, ""),
        ("a" * 64, " fp-v1"),
        ("a" * 64, "secret-key"),
    ],
)
def test_prepared_call_rejects_invalid_fingerprint_metadata(
    fingerprint: str, version: str
) -> None:
    """非 canonical digest 或 secret-like version 会破坏历史幂等比较。"""
    with pytest.raises(ValidationError):
        PreparedToolCall(
            request_fingerprint=fingerprint,
            fingerprint_version=version,
            audit_projection={},
            payload=object(),
        )


def test_tool_call_context_defensively_copies_ephemeral_params() -> None:
    """API 在构造调用后修改原 dict 不得改变本次外部副作用。"""
    params = {"subject": "original", "body": "body"}
    context = ToolCallContext(
        tenant_id=TenantId(new_id("tn")),
        user_id=UserId(new_id("usr")),
        tool_id="email.send",
        params=params,
        idempotency_key=IdempotencyKey("send-key-1"),
    )
    params["subject"] = "changed"
    assert context.params["subject"] == "original"


@pytest.mark.parametrize(
    "params",
    [
        {1: "value"},
        {"": "value"},
        {"bad\nkey": "value"},
    ],
)
def test_tool_call_context_rejects_malformed_param_keys(params: dict[object, object]) -> None:
    """错类型或控制字符 key 会让 schema/redaction 行为不确定。"""
    with pytest.raises(ValidationError):
        ToolCallContext(
            tenant_id=TenantId(new_id("tn")),
            user_id=UserId(new_id("usr")),
            tool_id="email.send",
            params=params,  # type: ignore[arg-type]
        )


def test_rejection_is_structured_frozen_and_safe() -> None:
    """自由异常不能替代 Agent 可处理且不会泄密的固定拒绝。"""
    rejection = CheckRejection(
        stage="approval",
        rule="approval:commercial_commitment",
        reason="邮件内容需要逐次审批",
        remediation="移除商业承诺后重试",
    )
    assert rejection.stage == "approval"
    with pytest.raises(FrozenInstanceError):
        rejection.rule = "changed"
    with pytest.raises(ValidationError):
        CheckRejection(
            stage="approval",
            rule="token:secret",
            reason="bad",
        )


def test_success_result_accepts_only_safe_output_and_is_defensively_copied() -> None:
    """handler 不能把任意 payload 借 output 写进审计或 HTTP。"""
    output: dict[str, str | int | bool | None] = {
        "provider_ref": "gmail_abc123",
        "already_existed": False,
    }
    result = ToolCallResult(
        tool_id="email.send",
        status=ToolCallStatus.SUCCEEDED,
        output=output,
        tool_call_id="tcl_01K00000000000000000000000",
    )
    output["provider_ref"] = "gmail_changed"
    assert result.output == {
        "provider_ref": "gmail_abc123",
        "already_existed": False,
    }


@pytest.mark.parametrize(
    "output",
    [
        {"body": "secret customer text"},
        {"recipient": "buyer@example.com"},
        {"safe": {"nested": True}},
        {"retry_after_seconds": True},
        {"already_existed": "false"},
        {"provider_ref": "buyer@example.com"},
        {"provider_ref": "Bearer_abc123"},
        {"attempt_id": "buyer@example.com"},
    ],
)
def test_tool_call_result_rejects_raw_or_wrong_typed_output(
    output: dict[str, object]
) -> None:
    """删除结果白名单或 bool/int 区分会重新开放任意内容输出。"""
    with pytest.raises(ValidationError):
        ToolCallResult(
            tool_id="email.send",
            status=ToolCallStatus.SUCCEEDED,
            output=output,  # type: ignore[arg-type]
            tool_call_id="tcl_01K00000000000000000000000",
        )


def test_result_state_fields_are_consistent() -> None:
    """拒绝、重复和成功证据混放会让调用方错误重试。"""
    rejection = CheckRejection("approval", "approval:required", "需要审批")
    with pytest.raises(ValidationError):
        ToolCallResult(
            tool_id="email.send",
            status=ToolCallStatus.SUCCEEDED,
            rejected=rejection,
        )
    with pytest.raises(ValidationError):
        ToolCallResult(tool_id="email.send", status=ToolCallStatus.REJECTED)
    with pytest.raises(ValidationError):
        ToolCallResult(tool_id="email.send", status=ToolCallStatus.DUPLICATE)
    duplicate = ToolCallResult(
        tool_id="email.send",
        status=ToolCallStatus.DUPLICATE,
        duplicate_of="tcl_01K00000000000000000000000",
        output={"provider_ref": "gmail_abc123", "already_existed": True},
    )
    assert duplicate.duplicate_of is not None


@pytest.mark.parametrize(
    "result",
    [
        {
            "status": ToolCallStatus.SUCCEEDED,
            "error_category": ToolErrorCategory.PROVIDER_PERMANENT,
        },
        {"status": ToolCallStatus.FAILED_TRANSIENT},
        {"status": ToolCallStatus.FAILED_PERMANENT},
        {
            "status": ToolCallStatus.RECEIVED,
            "retry_after_seconds": 30,
        },
        {
            "status": ToolCallStatus.FAILED_PERMANENT,
            "error_category": ToolErrorCategory.PROVIDER_PERMANENT,
            "retry_after_seconds": 30,
        },
    ],
)
def test_result_error_and_retry_metadata_match_terminal_status(
    result: dict[str, object]
) -> None:
    """漏校验会产生“成功但有永久错误”或无根因失败的矛盾证据。"""
    with pytest.raises(ValidationError):
        ToolCallResult(tool_id="email.send", **result)  # type: ignore[arg-type]


def test_transient_failure_accepts_typed_retry_metadata() -> None:
    """合法限流结果必须能携带有界重试时间供 API 映射。"""
    result = ToolCallResult(
        tool_id="email.send",
        status=ToolCallStatus.FAILED_TRANSIENT,
        error_category=ToolErrorCategory.RATE_LIMITED,
        retry_after_seconds=30,
    )
    assert result.retry_after_seconds == 30
