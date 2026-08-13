"""六阶段 Gateway 的顺序、短路与副作用边界。"""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from domains.outreach.schemas import MessageSendPreflight
from domains.sending_identity.errors import (
    AuthenticationNotVerifiedError,
    IdentityRetiredError,
    WarmupLimitExceededError,
)
from shared.errors import TransientError
from shared.schemas.identifiers import IdempotencyKey, TenantId, UserId, new_id
from tool_gateway.checks.approval import ApprovalCheck
from tool_gateway.checks.rate_limit import RateLimitCheck
from tool_gateway.checks.suppression import SuppressionCheck
from tool_gateway.errors import ToolCallStatus, ToolErrorCategory, ToolGatewayError
from tool_gateway.manifest import (
    CostClass,
    IdempotencyRequirement,
    RiskLevel,
    ToolManifest,
    ToolRegistry,
)
from tool_gateway.pipeline import (
    CheckRejection,
    PreparedToolCall,
    ToolCallContext,
    ToolGateway,
    ToolInvocationState,
)
from tool_gateway.repository import ClaimResult, ClaimStatus

NOW = datetime(2026, 8, 11, 12, tzinfo=UTC)


class _Ledger:
    def __init__(self, trace: list[str]) -> None:
        self.trace = trace
        self.records = {}

    async def create_received(self, record) -> None:
        self.trace.append("ledger.received")
        self.records[record.tool_call_id] = record

    async def append_event(self, event) -> None:
        del event

    async def claim(self, tenant_id, tool_call_id, **values):
        del tenant_id
        self.trace.append("idempotency.claim")
        record = self.records[tool_call_id]
        canonical = replace(
            record,
            idempotency_key=values["idempotency_key"],
            request_fingerprint=values["request_fingerprint"],
            fingerprint_version=values["fingerprint_version"],
            status=ToolCallStatus.CLAIMED,
            lease_owner=values["lease_owner"],
            lease_expires_at=values["lease_expires_at"],
            attempt_count=1,
        )
        self.records[tool_call_id] = canonical
        return ClaimResult(ClaimStatus.CLAIMED, canonical)

    async def mark_executing(self, tenant_id, tool_call_id) -> None:
        del tenant_id
        self.trace.append("ledger.executing")
        self.records[tool_call_id] = replace(
            self.records[tool_call_id], status=ToolCallStatus.EXECUTING
        )

    async def complete(
        self,
        tenant_id,
        tool_call_id,
        *,
        status,
        provider_ref,
        error_category,
        retry_after_at,
    ) -> None:
        del tenant_id, retry_after_at
        label = {
            ToolCallStatus.SUCCEEDED: "ledger.succeeded",
            ToolCallStatus.REJECTED: "ledger.rejected",
            ToolCallStatus.FAILED_TRANSIENT: "ledger.failed_transient",
            ToolCallStatus.FAILED_PERMANENT: "ledger.failed_permanent",
        }[status]
        self.trace.append(label)
        self.records[tool_call_id] = replace(
            self.records[tool_call_id],
            status=status,
            provider_ref=provider_ref,
            error_category=error_category,
            lease_owner=(self.records[tool_call_id].lease_owner if status is ToolCallStatus.FAILED_TRANSIENT else None),
            lease_expires_at=(self.records[tool_call_id].lease_expires_at if status is ToolCallStatus.FAILED_TRANSIENT else None),
            completed_at=(None if status is ToolCallStatus.FAILED_TRANSIENT else NOW),
        )


class _Uow:
    def __init__(
        self,
        ledger: _Ledger,
        *,
        fail_executing_commit: bool = False,
        fail_succeeded_commit: bool = False,
    ) -> None:
        self.calls = ledger
        self._ledger = ledger
        self._fail_executing_commit = fail_executing_commit
        self._fail_succeeded_commit = fail_succeeded_commit

    async def __aenter__(self):
        self._snapshot = copy.deepcopy(self._ledger.records)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        del exc_type, exc, tb
        if (
            self._fail_executing_commit
            and self._ledger.trace
            and self._ledger.trace[-1] == "ledger.executing"
        ):
            self._ledger.records = self._snapshot
            raise RuntimeError("commit-marker")
        if (
            self._fail_succeeded_commit
            and self._ledger.trace
            and self._ledger.trace[-1] == "ledger.succeeded"
        ):
            self._ledger.records = self._snapshot
            raise RuntimeError("success-commit-marker")


class _Stage:
    def __init__(self, name: str, trace: list[str], reject: bool = False) -> None:
        self.name = name
        self.trace = trace
        self.reject = reject

    async def check(self, ctx, state):
        del ctx
        self.trace.append(self.name)
        if self.name == "suppression.preflight":
            state.preflight = "current-preflight"
        if self.reject:
            return CheckRejection(self.name, f"{self.name}:denied", "调用被固定规则拒绝")
        return None


class _RateStage(_Stage):
    async def check(self, ctx, state):
        del ctx, state
        self.trace.extend(("outreach.claim", "rate_limit.reserve"))

    async def record_sent(self, ctx, state, provider_ref):
        del ctx, state, provider_ref
        self.trace.append("outreach.record_sent")


class _IdempotencyStage(_Stage):
    async def check(self, ctx, state):
        del ctx, state


class _Handler:
    def __init__(self, trace: list[str]) -> None:
        self.trace = trace

    async def prepare(self, ctx, preflight):
        del ctx
        self.trace.append("handler.prepare")
        self.trace.append(f"handler.preflight:{preflight}")
        return PreparedToolCall("a" * 64, "fp-v1", {"attempt": "bound"}, object())

    async def execute(self, tenant_id, prepared):
        del tenant_id, prepared
        self.trace.append("handler.execute")
        return {"provider_ref": "gmail_ref_1", "already_existed": False}


def _tool_manifest() -> ToolManifest:
    return ToolManifest(
            tool_id="email.send",
            version="v1",
            description="发送已批准 Campaign 邮件",
            risk_level=RiskLevel.HIGH,
            cost_class=CostClass.LOW,
            requires_approval=False,
            idempotency=IdempotencyRequirement.REQUIRED,
            required_permissions=("outreach:message_send",),
            checks=("tenant", "permission", "suppression", "approval", "idempotency", "rate_limit"),
    )


def _gateway(
    *,
    reject: str | None = None,
    fail_executing_commit: bool = False,
    fail_succeeded_commit: bool = False,
):
    trace: list[str] = []
    registry = ToolRegistry()
    registry.register(_tool_manifest(), _Handler(trace))
    ledger = _Ledger(trace)
    checks = {
        name: (
            _RateStage(name, trace)
            if name == "rate_limit"
            else _IdempotencyStage(name, trace)
            if name == "idempotency"
            else _Stage(
                "suppression.preflight" if name == "suppression" else name,
                trace,
                reject=name == reject,
            )
        )
        for name in ("tenant", "permission", "suppression", "approval", "idempotency", "rate_limit")
    }
    gateway = ToolGateway(
        registry,
        checks,
        lambda tenant: _Uow(
            ledger,
            fail_executing_commit=fail_executing_commit,
            fail_succeeded_commit=fail_succeeded_commit,
        ),
        lease_duration=timedelta(minutes=5),
        lease_owner="worker-1",
        now=lambda: NOW,
        id_factory=new_id,
    )
    return gateway, trace


def _context() -> ToolCallContext:
    return ToolCallContext(
        tenant_id=TenantId(new_id("tn")),
        user_id=UserId(new_id("usr")),
        tool_id="email.send",
        params={"attempt_id": new_id("mat"), "subject": "Hello", "body": "Need discussion"},
        idempotency_key=IdempotencyKey("send-key-1"),
        campaign_ref=new_id("cmp"),
    )


async def test_gateway_success_has_exact_stage_and_side_effect_order() -> None:
    gateway, trace = _gateway()
    result = await gateway.invoke(_context())
    assert result.status is ToolCallStatus.SUCCEEDED
    assert trace == [
        "ledger.received",
        "tenant",
        "permission",
        "suppression.preflight",
        "approval",
        "handler.prepare",
        "handler.preflight:current-preflight",
        "idempotency.claim",
        "outreach.claim",
        "rate_limit.reserve",
        "ledger.executing",
        "handler.execute",
        "outreach.record_sent",
        "ledger.succeeded",
    ]


@pytest.mark.parametrize(
    ("rejected", "absent"),
    [
        ("tenant", "permission"),
        ("permission", "suppression.preflight"),
        ("suppression", "approval"),
        ("approval", "handler.prepare"),
    ],
)
async def test_rejection_short_circuits_every_later_side_effect(
    rejected: str, absent: str
) -> None:
    gateway, trace = _gateway(reject=rejected)
    result = await gateway.invoke(_context())
    assert result.status is ToolCallStatus.REJECTED
    assert absent not in trace
    assert "idempotency.claim" not in trace
    assert "rate_limit.reserve" not in trace
    assert "handler.execute" not in trace


async def test_executing_commit_failure_never_calls_connector() -> None:
    gateway, trace = _gateway(fail_executing_commit=True)
    with pytest.raises(RuntimeError, match="commit-marker"):
        await gateway.invoke(_context())
    assert "ledger.executing" in trace
    assert "handler.execute" not in trace
    assert "outreach.record_sent" not in trace


async def test_success_ledger_commit_failure_returns_reconciliation_required() -> None:
    gateway, trace = _gateway(fail_succeeded_commit=True)
    result = await gateway.invoke(_context())
    assert result.status is ToolCallStatus.FAILED_TRANSIENT
    assert result.error_category is ToolErrorCategory.RECONCILIATION_REQUIRED
    assert trace.count("handler.execute") == 1
    assert trace.count("outreach.record_sent") == 1
    assert trace[-1] == "ledger.failed_transient"


class _OutreachStageService:
    def __init__(self, preflight: MessageSendPreflight) -> None:
        self.preflight = preflight
        self.calls: list[tuple[str, object]] = []

    async def preflight_message_send(self, tenant_id, attempt_id, *, actor):
        del tenant_id, actor
        self.calls.append(("preflight", attempt_id))
        return self.preflight

    async def claim_message_send(self, tenant_id, attempt_id, *, actor):
        del tenant_id, actor
        self.calls.append(("claim", attempt_id))

    async def record_send_failure(self, tenant_id, attempt_id, category, *, actor):
        del tenant_id, actor
        self.calls.append(("failure", (attempt_id, category.value)))

    async def record_sent(self, tenant_id, attempt_id, provider_ref, *, actor):
        del tenant_id, actor
        self.calls.append(("sent", (attempt_id, provider_ref)))


class _SendingStageService:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[object, ...]] = []

    async def reserve_send_slot(
        self, tenant_id, identity_id, key, for_cold_outreach, *, actor
    ):
        del tenant_id, actor
        self.calls.append((identity_id, key, for_cold_outreach))
        if self.error is not None:
            raise self.error


def _preflight(ctx: ToolCallContext) -> MessageSendPreflight:
    return MessageSendPreflight(
        ctx.tenant_id,
        ctx.params["attempt_id"],
        ctx.campaign_ref,
        new_id("enr"),
        new_id("acc"),
        new_id("cp"),
        new_id("sid"),
        1,
        1,
        ctx.idempotency_key,
    )


def _state(ctx: ToolCallContext) -> ToolInvocationState:
    return ToolInvocationState(manifest=_tool_manifest(), tool_call_id=new_id("tcl"))


async def test_suppression_delegates_preflight_and_approval_blocks_commitment() -> None:
    ctx = _context()
    preflight = _preflight(ctx)
    outreach = _OutreachStageService(preflight)
    state = _state(ctx)
    suppression = SuppressionCheck(outreach, lambda _ctx: object())
    assert await suppression.check(ctx, state) is None
    assert state.preflight == preflight
    assert outreach.calls == [("preflight", preflight.attempt_id)]

    approval = ApprovalCheck()
    safe = await approval.check(ctx, state)
    assert safe is None
    unsafe = replace(
        ctx,
        params={
            "attempt_id": ctx.params["attempt_id"],
            "subject": "正式报价",
            "body": "保证七天交货",
        },
    )
    rejected = await approval.check(unsafe, state)
    assert rejected is not None
    assert rejected.rule == "approval:commercial_commitment"


@pytest.mark.parametrize(
    ("error", "category", "attempt_failure"),
    [
        (WarmupLimitExceededError("fixed"), ToolErrorCategory.RATE_LIMITED, "rate_limited"),
        (
            AuthenticationNotVerifiedError("fixed"),
            ToolErrorCategory.PROVIDER_AUTH_REQUIRED,
            "provider_auth_required",
        ),
        (
            IdentityRetiredError("fixed"),
            ToolErrorCategory.PROVIDER_PERMANENT,
            "identity_unavailable",
        ),
        (
            TransientError("fixed"),
            ToolErrorCategory.PROVIDER_TRANSIENT,
            "provider_transient",
        ),
    ],
)
async def test_rate_stage_claims_before_slot_and_records_exact_typed_failure(
    error: Exception, category: ToolErrorCategory, attempt_failure: str
) -> None:
    ctx = _context()
    preflight = _preflight(ctx)
    outreach = _OutreachStageService(preflight)
    sending = _SendingStageService(error)
    state = _state(ctx)
    state.preflight = preflight
    stage = RateLimitCheck(
        outreach,
        sending,
        lambda _ctx: object(),
        lambda _ctx, _preflight: object(),
    )
    with pytest.raises(ToolGatewayError) as caught:
        await stage.check(ctx, state)
    assert caught.value.category is category
    assert outreach.calls == [
        ("claim", preflight.attempt_id),
        ("failure", (preflight.attempt_id, attempt_failure)),
    ]
    assert sending.calls == [
        (preflight.sending_identity_id, preflight.idempotency_key, True)
    ]
