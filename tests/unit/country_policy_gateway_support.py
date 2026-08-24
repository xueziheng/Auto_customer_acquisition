"""国家政策检查的真实 ToolGateway 测试装配，只用于测试。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from shared.schemas.identifiers import TenantId
from tool_gateway.errors import ToolCallStatus
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.manifest import (
    CostClass,
    IdempotencyRequirement,
    RiskLevel,
    ToolManifest,
    ToolRegistry,
)
from tool_gateway.pipeline import (
    CheckStage,
    PreparedToolCall,
    ToolCallContext,
    ToolGateway,
)
from tool_gateway.repository import ClaimResult, ClaimStatus

NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)


class CounterTransport:
    def __init__(self) -> None:
        self.calls = 0

    async def call(self) -> str:
        self.calls += 1
        return "provider_ref_1"


class CounterHandler:
    def __init__(self, transport: CounterTransport) -> None:
        self.transport = transport
        self.prepare_calls = 0
        self.execute_calls = 0
        self._fingerprints = HmacFingerprintProvider("v1", b"x" * 32)

    async def prepare(
        self, ctx: ToolCallContext, preflight: object | None
    ) -> PreparedToolCall:
        del preflight
        self.prepare_calls += 1
        fingerprint, version = self._fingerprints.fingerprint(
            (str(ctx.tenant_id).encode(), ctx.tool_id.encode())
        )
        return PreparedToolCall(fingerprint, version, {}, object())

    async def execute(
        self, tenant_id: TenantId, prepared: PreparedToolCall
    ) -> dict[str, str]:
        del tenant_id, prepared
        self.execute_calls += 1
        return {"provider_ref": await self.transport.call()}


class PreflightStage:
    name = "playbook"

    def __init__(self, preflight: object) -> None:
        self._preflight = preflight

    async def check(self, ctx, state):
        del ctx
        state.preflight = self._preflight


class _Ledger:
    def __init__(self) -> None:
        self.records = {}

    async def create_received(self, record) -> None:
        self.records[record.tool_call_id] = record

    async def append_event(self, event) -> None:
        del event

    async def claim(self, tenant_id, tool_call_id, **values):
        del tenant_id
        canonical = replace(
            self.records[tool_call_id],
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
        del tenant_id, tool_call_id

    async def complete(self, tenant_id, tool_call_id, **values) -> None:
        del tenant_id, tool_call_id, values


class _UnitOfWork:
    def __init__(self, ledger: _Ledger) -> None:
        self.calls = ledger

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        del exc_type, exc, tb


def build_country_policy_gateway(
    manifest: ToolManifest,
    preflight: object,
    country_policy: CheckStage,
) -> tuple[ToolGateway, CounterHandler, CounterTransport]:
    transport = CounterTransport()
    handler = CounterHandler(transport)
    registry = ToolRegistry()
    registry.register(manifest, handler)
    ledger = _Ledger()
    gateway = ToolGateway(
        registry,
        {
            "playbook": PreflightStage(preflight),
            "country_policy": country_policy,
        },
        lambda tenant_id: _UnitOfWork(ledger),
        lease_duration=timedelta(seconds=30),
        lease_owner="country_policy_test",
        now=lambda: NOW,
        id_factory=_id_factory(),
    )
    return gateway, handler, transport


def country_policy_manifest(tool_id: str) -> ToolManifest:
    return ToolManifest(
        tool_id=tool_id,
        version="test-v1",
        description="测试国家政策短路的计数工具",
        risk_level=RiskLevel.MEDIUM,
        cost_class=CostClass.LOW,
        requires_approval=False,
        idempotency=IdempotencyRequirement.NONE,
        required_permissions=("test:country_policy",),
        checks=("playbook", "country_policy"),
    )


def _id_factory():
    sequence = 0

    def create(prefix: str) -> str:
        nonlocal sequence
        sequence += 1
        return f"{prefix}_01J00000000000000000000{sequence:03d}"

    return create


__all__ = (
    "build_country_policy_gateway",
    "country_policy_manifest",
)
