"""DNS 认证只读工具；持久层只保存安全 typed capsule。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from shared.errors import TransientError, ValidationError
from shared.schemas.dns_auth import (
    DnsAuthenticationFacts,
    DnsAuthenticationFailure,
    DnsAuthenticationFailureCategory,
    DnsAuthenticationRequest,
)
from shared.schemas.identifiers import (
    AuthenticationCheckRequestId,
    IdempotencyKey,
    RunId,
    SendingIdentityId,
    TenantId,
    UserId,
)
from tool_gateway.errors import ToolCallStatus, ToolErrorCategory, ToolGatewayError
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.manifest import (
    CostClass,
    IdempotencyRequirement,
    RiskLevel,
    ToolManifest,
)
from tool_gateway.pipeline import PreparedToolCall, ToolCallContext, ToolCallResult

_ULID = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"
_REQUEST_ID = re.compile(rf"acr_{_ULID}\Z")
_IDENTITY_ID = re.compile(rf"sid_{_ULID}\Z")
_CHECK_CODE = {"spf": "s", "dkim": "k", "dmarc": "m"}
_CHECK_FROM_CODE = {value: key for key, value in _CHECK_CODE.items()}
_CATEGORY_CODE = {
    DnsAuthenticationFailureCategory.MISSING: "0",
    DnsAuthenticationFailureCategory.MALFORMED: "1",
    DnsAuthenticationFailureCategory.POLICY_UNSAFE: "2",
}
_CATEGORY_FROM_CODE = {value: key for key, value in _CATEGORY_CODE.items()}
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

MANIFEST = ToolManifest(
    tool_id="dns.auth.check",
    version="v1",
    description="读取公开 DNS 并验证 SPF、DKIM 与 DMARC",
    risk_level=RiskLevel.LOW,
    cost_class=CostClass.FREE,
    requires_approval=False,
    idempotency=IdempotencyRequirement.REQUIRED,
    required_permissions=("sending_identity:auth_check",),
    checks=("tenant", "permission", "idempotency", "rate_limit"),
    input_schema={
        "type": "object",
        "required": (
            "request_id",
            "sending_identity_id",
            "domain",
            "dkim_selector",
        ),
        "properties": {
            "request_id": {"type": "string"},
            "sending_identity_id": {"type": "string"},
            "domain": {"type": "string"},
            "dkim_selector": {"type": "string"},
        },
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "required": ("provider_ref",),
        "properties": {"provider_ref": {"type": "string"}},
        "additionalProperties": False,
    },
)


@runtime_checkable
class DnsAuthenticationChecker(Protocol):
    async def check(
        self, request: DnsAuthenticationRequest
    ) -> DnsAuthenticationFacts: ...


@runtime_checkable
class _Gateway(Protocol):
    async def invoke(self, ctx: ToolCallContext) -> ToolCallResult: ...


@dataclass(frozen=True, repr=False)
class _Payload:
    tenant_id: TenantId
    request: DnsAuthenticationRequest


def _encode_facts(facts: DnsAuthenticationFacts) -> str:
    bits = "".join(
        "1" if value else "0"
        for value in (facts.spf_passed, facts.dkim_passed, facts.dmarc_passed)
    )
    failures = (
        ",".join(
            f"{_CHECK_CODE[item.check]}{_CATEGORY_CODE[item.category]}"
            for item in facts.failures
        )
        or "-"
    )
    elapsed = facts.checked_at - _EPOCH
    epoch_microseconds = (
        (elapsed.days * 86_400 + elapsed.seconds) * 1_000_000
        + elapsed.microseconds
    )
    return f"dns2.{epoch_microseconds}.{facts.check_ref}.{bits}.{failures}"


def _decode_facts(value: object) -> DnsAuthenticationFacts:
    try:
        prefix, epoch, check_ref, bits, encoded = str(value).split(".", 4)
        if prefix != "dns2" or len(bits) != 3 or set(bits) - {"0", "1"}:
            raise ValueError
        epoch_microseconds = int(epoch)
        if epoch_microseconds < 0:
            raise ValueError
        seconds, microseconds = divmod(epoch_microseconds, 1_000_000)
        failures: list[DnsAuthenticationFailure] = []
        if encoded != "-":
            for code in encoded.split(","):
                check = _CHECK_FROM_CODE[code[0]]
                category = _CATEGORY_FROM_CODE[code[1]]
                failures.append(
                    DnsAuthenticationFailure(check, category, f"configure_{check}")
                )
        return DnsAuthenticationFacts(
            _EPOCH + timedelta(seconds=seconds, microseconds=microseconds),
            bits[0] == "1",
            bits[1] == "1",
            bits[2] == "1",
            tuple(failures),
            check_ref,
        )
    except (KeyError, TypeError, ValueError, IndexError, OverflowError):
        raise ValidationError("DNS 工具结果无效") from None


class DnsAuthenticationCheckHandler:
    """严格准备安全输入，执行后仅返回 typed facts capsule。"""

    def __init__(
        self,
        connector: DnsAuthenticationChecker,
        fingerprints: HmacFingerprintProvider,
    ) -> None:
        if not isinstance(connector, DnsAuthenticationChecker) or not isinstance(
            fingerprints, HmacFingerprintProvider
        ):
            raise ValidationError("DNS handler 依赖无效")
        self._connector = connector
        self._fingerprints = fingerprints

    async def prepare(
        self, ctx: ToolCallContext, preflight: object | None
    ) -> PreparedToolCall:
        del preflight
        if set(ctx.params) != {
            "request_id",
            "sending_identity_id",
            "domain",
            "dkim_selector",
        }:
            raise ValidationError("DNS 工具参数无效")
        request_id = ctx.params.get("request_id")
        identity_id = ctx.params.get("sending_identity_id")
        if (
            not isinstance(request_id, str)
            or _REQUEST_ID.fullmatch(request_id) is None
            or not isinstance(identity_id, str)
            or _IDENTITY_ID.fullmatch(identity_id) is None
        ):
            raise ValidationError("DNS 工具参数无效")
        request = DnsAuthenticationRequest(
            ctx.params.get("domain"),  # type: ignore[arg-type]
            ctx.params.get("dkim_selector"),  # type: ignore[arg-type]
        )
        fingerprint, version = self._fingerprints.fingerprint(
            (
                b"dns-auth-v1",
                str(ctx.tenant_id).encode(),
                request_id.encode(),
                identity_id.encode(),
                request.domain.encode(),
                request.dkim_selector.encode(),
            )
        )
        return PreparedToolCall(
            fingerprint,
            version,
            {
                "request_id": request_id,
                "sending_identity_id": identity_id,
                "domain": request.domain,
                "dkim_selector": request.dkim_selector,
            },
            _Payload(ctx.tenant_id, request),
        )

    async def execute(
        self, tenant_id: TenantId, prepared: PreparedToolCall
    ) -> dict[str, str]:
        payload = prepared.payload
        if not isinstance(payload, _Payload) or payload.tenant_id != tenant_id:
            raise ValidationError("DNS 工具 payload 无效")
        facts = await self._connector.check(payload.request)
        if not isinstance(facts, DnsAuthenticationFacts):
            raise ValidationError("DNS Connector 结果无效")
        return {"provider_ref": _encode_facts(facts)}

    async def reconcile(
        self, tenant_id: TenantId, prepared: PreparedToolCall
    ) -> dict[str, str]:
        """DNS 是只读有界查询；账本模糊时安全重查并生成新的安全 capsule。"""
        return await self.execute(tenant_id, prepared)


class ToolGatewayDnsAuthenticationChecker:
    """workflow-facing adapter；所有外部 DNS 动作仍经通用 Gateway。"""

    def __init__(self, gateway: _Gateway, user_id: UserId) -> None:
        if (
            not isinstance(gateway, _Gateway)
            or not isinstance(user_id, str)
            or not user_id
        ):
            raise ValidationError("DNS 工具 checker 依赖无效")
        self._gateway = gateway
        self._user_id = user_id

    async def check(
        self,
        *,
        tenant_id: TenantId,
        run_id: RunId,
        request_id: AuthenticationCheckRequestId,
        sending_identity_id: SendingIdentityId,
        domain: str,
        dkim_selector: str,
    ) -> DnsAuthenticationFacts:
        result = await self._gateway.invoke(
            ToolCallContext(
                tenant_id,
                self._user_id,
                MANIFEST.tool_id,
                {
                    "request_id": str(request_id),
                    "sending_identity_id": str(sending_identity_id),
                    "domain": domain,
                    "dkim_selector": dkim_selector,
                },
                run_id=run_id,
                idempotency_key=IdempotencyKey(f"auth:{request_id}"),
            )
        )
        if result.status in {ToolCallStatus.SUCCEEDED, ToolCallStatus.DUPLICATE}:
            provider_ref = (
                None if result.output is None else result.output.get("provider_ref")
            )
            return _decode_facts(provider_ref)
        if result.status is ToolCallStatus.FAILED_TRANSIENT:
            raise TransientError("DNS 工具暂时不可用")
        raise ToolGatewayError(
            result.error_category or ToolErrorCategory.PROVIDER_PERMANENT
        )
