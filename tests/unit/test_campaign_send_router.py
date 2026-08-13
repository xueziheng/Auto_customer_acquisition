"""受保护的单封手工发送 HTTP 契约。"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from httpx import ASGITransport, AsyncClient

from apps.api.dependencies import get_api_dependencies, get_request_identity
from apps.api.identity import RequestIdentity
from apps.api.main import create_app
from apps.api.middleware import ApiSettings
from domains.employees.permissions import Actor as EmployeeActor
from domains.employees.permissions import EmployeeScope
from domains.employees.schemas import EmployeeView
from domains.opportunities.permissions import Actor as OpportunityActor
from domains.opportunities.permissions import OpportunityScope, ScopeLevel
from shared.schemas.identifiers import EmployeeId, TenantId, new_id
from tool_gateway.errors import ToolCallStatus, ToolErrorCategory
from tool_gateway.pipeline import CheckRejection, ToolCallContext, ToolCallResult

_TENANT = TenantId(new_id("tn"))
_EMPLOYEE = EmployeeId(new_id("emp"))
_HEADERS = {
    "X-Tenant-Id": str(_TENANT),
    "X-Employee-Id": str(_EMPLOYEE),
}


class _Gateway:
    def __init__(self, result: ToolCallResult) -> None:
        self.result = result
        self.contexts: list[ToolCallContext] = []

    async def invoke(self, ctx: ToolCallContext) -> ToolCallResult:
        self.contexts.append(ctx)
        return self.result


@dataclass(frozen=True)
class _Dependencies:
    tool_gateway: _Gateway


def _identity(*, role: str = "sales") -> RequestIdentity:
    employee = EmployeeView(
        employee_id=_EMPLOYEE,
        tenant_id=_TENANT,
        name="测试员工",
        role=role,
        manager_id=None,
        is_active=True,
    )
    return RequestIdentity(
        tenant_id=_TENANT,
        employee=employee,
        employee_actor=EmployeeActor(str(_EMPLOYEE), EmployeeScope.SELF, role),
        opportunity_actor=OpportunityActor(
            str(_EMPLOYEE),
            OpportunityScope(
                level=ScopeLevel.SELF,
                allowed_owners=frozenset({_EMPLOYEE}),
            ),
            role,
        ),
    )


def _success_result(*, duplicate: bool = False) -> ToolCallResult:
    tool_call_id = new_id("tcl")
    if duplicate:
        return ToolCallResult(
            tool_id="email.send",
            status=ToolCallStatus.DUPLICATE,
            output={"provider_ref": "gmail_ref_1", "duplicate": True},
            duplicate_of=tool_call_id,
            tool_call_id=tool_call_id,
        )
    return ToolCallResult(
        tool_id="email.send",
        status=ToolCallStatus.SUCCEEDED,
        output={"provider_ref": "gmail_ref_1", "already_existed": False},
        tool_call_id=tool_call_id,
    )


def _error_result(category: ToolErrorCategory) -> ToolCallResult:
    call_id = new_id("tcl")
    rejected_categories = {
        ToolErrorCategory.VALIDATION,
        ToolErrorCategory.PERMISSION_DENIED,
        ToolErrorCategory.SUPPRESSED,
        ToolErrorCategory.APPROVAL_REQUIRED,
        ToolErrorCategory.IDEMPOTENCY_CONFLICT,
    }
    if category in rejected_categories:
        return ToolCallResult(
            tool_id="email.send",
            status=ToolCallStatus.REJECTED,
            rejected=CheckRejection(
                "runtime",
                f"runtime:{category.value}",
                "工具调用当前不可继续",
            ),
            tool_call_id=call_id,
            error_category=category,
        )
    transient_categories = {
        ToolErrorCategory.IN_PROGRESS,
        ToolErrorCategory.RATE_LIMITED,
        ToolErrorCategory.PROVIDER_TRANSIENT,
        ToolErrorCategory.RECONCILIATION_REQUIRED,
    }
    return ToolCallResult(
        tool_id="email.send",
        status=(
            ToolCallStatus.FAILED_TRANSIENT
            if category in transient_categories
            else ToolCallStatus.FAILED_PERMANENT
        ),
        tool_call_id=call_id,
        error_category=category,
        retry_after_seconds=(17 if category is ToolErrorCategory.RATE_LIMITED else None),
    )


def _app_with(result: ToolCallResult):
    gateway = _Gateway(result)
    app = create_app(
        settings=ApiSettings(
            tenant_id=str(_TENANT),
            dev_mode=True,
            retry_after_seconds=23,
        )
    )
    app.dependency_overrides[get_api_dependencies] = lambda: _Dependencies(gateway)
    app.dependency_overrides[get_request_identity] = _identity
    return app, gateway


async def _post(app, attempt_id: str, payload: dict[str, object]):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post(
            f"/crm/message-attempts/{attempt_id}/send",
            headers=_HEADERS,
            json=payload,
        )


async def test_manual_send_derives_every_resource_binding_server_side() -> None:
    app, gateway = _app_with(_success_result())
    attempt_id = new_id("mat")
    subject = "Subject-marker-that-must-not-be-returned"
    body = "Body-marker-that-must-not-be-returned"

    response = await _post(
        app,
        attempt_id,
        {"subject": subject, "body": body},
    )

    assert response.status_code == 200
    assert response.json() == {
        "tool_call_id": gateway.result.tool_call_id,
        "status": "succeeded",
        "duplicate": False,
        "provider_ref": "gmail_ref_1",
        "error_category": None,
    }
    assert subject not in response.text
    assert body not in response.text
    assert len(gateway.contexts) == 1
    context = gateway.contexts[0]
    assert context.tenant_id == _TENANT
    assert context.user_id == str(_EMPLOYEE)
    assert context.tool_id == "email.send"
    assert dict(context.params) == {
        "attempt_id": attempt_id,
        "subject": subject,
        "body": body,
    }
    assert context.idempotency_key is None
    assert context.campaign_ref is None
    assert context.approval_ref is None


async def test_manual_send_duplicate_is_a_safe_success() -> None:
    app, _gateway = _app_with(_success_result(duplicate=True))

    response = await _post(
        app,
        new_id("mat"),
        {"subject": "Hello", "body": "Need discussion"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "tool_call_id": _gateway.result.tool_call_id,
        "status": "duplicate",
        "duplicate": True,
        "provider_ref": "gmail_ref_1",
        "error_category": None,
    }


@pytest.mark.parametrize(
    "extra_field",
    [
        "tenant_id",
        "recipient_address",
        "from_address",
        "sending_identity_id",
        "tool_id",
        "idempotency_key",
        "approval_ref",
        "provider_ref",
        "unsubscribe_url",
    ],
)
async def test_manual_send_body_rejects_every_server_owned_field(
    extra_field: str,
) -> None:
    marker = f"forbidden-{extra_field}-marker"
    app, gateway = _app_with(_success_result())

    response = await _post(
        app,
        new_id("mat"),
        {"subject": "Hello", "body": "Need discussion", extra_field: marker},
    )

    assert response.status_code == 400
    assert response.json() == {
        "code": "validation_error",
        "message": "请求参数无效",
    }
    assert marker not in response.text
    assert gateway.contexts == []


async def test_manual_send_rejects_noncanonical_attempt_before_gateway() -> None:
    marker = "mat-invalid-attempt-marker"
    app, gateway = _app_with(_success_result())

    response = await _post(
        app,
        marker,
        {"subject": "Hello", "body": "Need discussion"},
    )

    assert response.status_code == 400
    assert response.json() == {
        "code": "validation_error",
        "message": "请求参数无效",
    }
    assert marker not in response.text
    assert gateway.contexts == []


@pytest.mark.parametrize(
    ("category", "status_code", "code", "message", "retry_after"),
    [
        (ToolErrorCategory.VALIDATION, 400, "validation_error", "发送请求无效", None),
        (
            ToolErrorCategory.APPROVAL_REQUIRED,
            400,
            "approval_required",
            "客户内容需要人工审批",
            None,
        ),
        (ToolErrorCategory.PERMISSION_DENIED, 403, "forbidden", "没有权限", None),
        (
            ToolErrorCategory.SUPPRESSED,
            403,
            "suppressed",
            "当前事实不允许发送",
            None,
        ),
        (
            ToolErrorCategory.IDEMPOTENCY_CONFLICT,
            409,
            "idempotency_conflict",
            "发送请求与既有记录冲突",
            None,
        ),
        (
            ToolErrorCategory.IN_PROGRESS,
            409,
            "in_progress",
            "发送正在处理中",
            None,
        ),
        (
            ToolErrorCategory.RECONCILIATION_REQUIRED,
            409,
            "reconciliation_required",
            "发送结果需要人工对账",
            None,
        ),
        (
            ToolErrorCategory.RATE_LIMITED,
            429,
            "rate_limited",
            "发送额度暂不可用",
            "17",
        ),
        (
            ToolErrorCategory.PROVIDER_AUTH_REQUIRED,
            503,
            "service_unavailable",
            "发送服务暂不可用",
            "23",
        ),
        (
            ToolErrorCategory.PROVIDER_TRANSIENT,
            503,
            "service_unavailable",
            "发送服务暂不可用",
            "23",
        ),
        (
            ToolErrorCategory.UNEXPECTED,
            503,
            "service_unavailable",
            "发送服务暂不可用",
            "23",
        ),
        (
            ToolErrorCategory.PROVIDER_PERMANENT,
            400,
            "request_rejected",
            "发送请求被服务拒绝",
            None,
        ),
    ],
)
async def test_manual_send_maps_gateway_categories_to_fixed_safe_http_errors(
    category: ToolErrorCategory,
    status_code: int,
    code: str,
    message: str,
    retry_after: str | None,
) -> None:
    raw_markers = ("buyer@example.com", "subject-secret", "body-secret")
    app, gateway = _app_with(_error_result(category))

    response = await _post(
        app,
        new_id("mat"),
        {"subject": raw_markers[1], "body": raw_markers[2]},
    )

    assert response.status_code == status_code
    assert response.json() == {"code": code, "message": message}
    assert response.headers.get("Retry-After") == retry_after
    assert all(marker not in response.text for marker in raw_markers)
    assert len(gateway.contexts) == 1
