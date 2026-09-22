"""model.generate 插件：请求私有正文、持久计量、无自动重放。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast

from connectors.deepseek.client import DeepSeekFailure
from shared.schemas.identifiers import IdempotencyKey, TenantId, new_id
from shared.schemas.model_invocation import (
    InvocationIdentity,
    ModelGenerationError,
    ModelLimits,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)
from tool_gateway.checks.idempotency import IdempotencyCheck
from tool_gateway.checks.model import (
    CurrentModelAuthority,
    ModelIdentityCheck,
    ModelQuotaCheck,
)
from tool_gateway.errors import ToolCallStatus, ToolErrorCategory, ToolGatewayError
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.manifest import (
    CostClass,
    IdempotencyRequirement,
    RiskLevel,
    ToolManifest,
    ToolRegistry,
)
from tool_gateway.model_usage import ModelUsageRepository
from tool_gateway.pipeline import (
    CheckStage,
    PreparedToolCall,
    SafeScalar,
    ToolCallContext,
    ToolGateway,
    _GatewayRegistry,
)
from tool_gateway.repository import ToolGatewayUnitOfWorkFactory

MANIFEST = ToolManifest(
    tool_id="model.generate",
    version="v1",
    description="受信身份绑定的单次结构化模型请求",
    risk_level=RiskLevel.MEDIUM,
    cost_class=CostClass.MEDIUM,
    requires_approval=False,
    idempotency=IdempotencyRequirement.REQUIRED,
    required_permissions=("model:generate",),
    checks=("tenant", "permission", "playbook", "idempotency", "rate_limit"),
    input_schema={
        "type": "object",
        "properties": {"invocation_ref": {"type": "string"}},
        "required": ("invocation_ref",),
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {"provider_ref": {"type": "string"}},
        "required": ("provider_ref",),
        "additionalProperties": False,
    },
)
UNKNOWN_USAGE = ModelUsage(
    input_tokens=None, cached_input_tokens=None, output_tokens=None
)


class ModelProvider(Protocol):
    async def generate(self, request: ModelRequest) -> ModelResponse: ...
    async def aclose(self) -> None: ...


class ModelResponseSlot:
    """请求私有且 task-owned；句柄不能跨调用恢复模型正文。"""

    def __init__(self) -> None:
        self._entry: tuple[str, ModelResponse] | None = None
        self._owner: asyncio.Task[object] | None = None

    def put(self, response: ModelResponse) -> str:
        if self._entry is not None:
            raise ValueError("模型结果槽已占用")
        self._owner = asyncio.current_task()
        handle = new_id("mres")
        self._entry = (handle, response)
        return handle

    def take(self, handle: str) -> ModelResponse:
        if (
            self._owner is not asyncio.current_task()
            or self._entry is None
            or self._entry[0] != handle
        ):
            raise ValueError("模型结果不可领取")
        response = self._entry[1]
        self.discard_all()
        return response

    def discard_all(self) -> None:
        self._entry = None
        self._owner = None


class ModelGenerateHandler:
    def __init__(
        self,
        identity: InvocationIdentity,
        request: ModelRequest,
        slot: ModelResponseSlot,
        fingerprints: HmacFingerprintProvider,
        quota: ModelQuotaCheck,
        usage: ModelUsageRepository,
        authority: CurrentModelAuthority,
        provider_factory: Callable[[], ModelProvider],
    ) -> None:
        self._identity, self._request, self._slot = identity, request, slot
        self._fingerprints, self._quota, self._usage = fingerprints, quota, usage
        self._authority, self._provider_factory = authority, provider_factory
        self.failure: ModelGenerationError | None = None

    async def prepare(
        self, ctx: ToolCallContext, preflight: object | None
    ) -> PreparedToolCall:
        identity = self._identity.model_dump_json().encode()
        request = json.dumps(
            {
                "model": self._request.model,
                "instructions": self._request.system_prompt,
                "payload": self._request.payload,
                "max_output_tokens": self._request.max_output_tokens,
            },
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
        fingerprint, version = self._fingerprints.fingerprint((identity, request))
        return PreparedToolCall(
            fingerprint,
            version,
            {"capability": self._identity.capability},
            self._request,
        )

    async def execute(
        self, tenant_id: TenantId, prepared: PreparedToolCall
    ) -> Mapping[str, SafeScalar]:
        reservation = self._quota.reservation
        if (
            tenant_id != self._identity.tenant_id
            or reservation is None
            or reservation.invocation_id is None
        ):
            raise ToolGatewayError(ToolErrorCategory.PERMISSION_DENIED)
        invocation = reservation.invocation_id
        try:
            await self._authority.check(self._identity)
        except ModelGenerationError as exc:
            self.failure = exc
            await self._usage.finish(tenant_id, invocation, UNKNOWN_USAGE, "rejected")
            raise ToolGatewayError(ToolErrorCategory.PERMISSION_DENIED) from None
        await self._usage.mark_dispatched(tenant_id, invocation)
        provider: ModelProvider | None = None
        settled = False
        try:
            provider = self._provider_factory()
            response = await provider.generate(self._request)
            await self._usage.finish(tenant_id, invocation, response.usage, "succeeded")
            settled = True
            # 请求途中停用员工时，不把已计费的结果误当成可应用输出。
            await self._authority.check(self._identity)
            return {"provider_ref": self._slot.put(response)}
        except ModelGenerationError as exc:
            self.failure = exc
            raise ToolGatewayError(ToolErrorCategory.PERMISSION_DENIED) from None
        except DeepSeekFailure as exc:
            self.failure = ModelGenerationError(exc.code)
            uncertain = exc.code in {"unknown", "rate_limit", "provider_error"}
            await self._usage.finish(
                tenant_id,
                invocation,
                UNKNOWN_USAGE,
                "unknown" if uncertain else "invalid",
            )
            settled = True
            raise ToolGatewayError(
                ToolErrorCategory.RECONCILIATION_REQUIRED
                if uncertain
                else ToolErrorCategory.PROVIDER_PERMANENT
            ) from None
        except BaseException:
            if not settled:
                await asyncio.shield(
                    self._usage.finish(tenant_id, invocation, UNKNOWN_USAGE, "unknown")
                )
            raise
        finally:
            if provider is not None:
                await provider.aclose()


class GatewayModelGenerator:
    """每次调用装配独立插件对象，避免可变身份/正文跨请求共享。"""

    def __init__(
        self,
        *,
        authority: CurrentModelAuthority,
        usage: ModelUsageRepository,
        provider_factory: Callable[[], ModelProvider],
        limits: ModelLimits,
        model: str,
        configuration_version: str,
        ledger_factory: ToolGatewayUnitOfWorkFactory,
        fingerprints: HmacFingerprintProvider,
        lease_owner: str,
        lease_duration: timedelta,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._authority, self._usage, self._provider_factory = (
            authority,
            usage,
            provider_factory,
        )
        self._limits, self._model, self._version = limits, model, configuration_version
        self._ledger, self._fingerprints = ledger_factory, fingerprints
        self._owner, self._lease = lease_owner, lease_duration
        self._now = now or (lambda: datetime.now(UTC))

    async def generate(
        self, identity: InvocationIdentity, request: ModelRequest
    ) -> ModelResponse:
        if (
            identity.configuration_version != self._version
            or request.model != self._model
        ):
            raise ModelGenerationError("configuration")
        encoded = json.dumps(request.payload, ensure_ascii=False, allow_nan=False)
        if (
            len(encoded.encode()) + len(request.system_prompt.encode())
            > self._limits.max_input_bytes
            or request.max_output_tokens > self._limits.max_output_tokens
        ):
            raise ModelGenerationError("invalid_request")
        # Pydantic frozen 不冻结内部字典；在 HMAC/发送之前固定深副本。
        request = ModelRequest(
            model=request.model,
            system_prompt=request.system_prompt,
            payload=json.loads(encoded),
            max_output_tokens=request.max_output_tokens,
        )
        slot = ModelResponseSlot()
        quota = ModelQuotaCheck(
            identity, self._usage, self._limits, self._model, self._now, self._owner, self._lease
        )
        handler = ModelGenerateHandler(
            identity,
            request,
            slot,
            self._fingerprints,
            quota,
            self._usage,
            self._authority,
            self._provider_factory,
        )
        authority_checks = [
            ModelIdentityCheck(name, identity, self._authority)
            for name in ("tenant", "permission", "playbook")
        ]
        checks: dict[str, CheckStage] = {c.name: c for c in authority_checks}
        checks.update({"idempotency": IdempotencyCheck(), "rate_limit": quota})
        registry = ToolRegistry()
        registry.register(MANIFEST, handler)
        gateway = ToolGateway(
            cast(_GatewayRegistry, registry),
            checks,
            self._ledger,
            lease_duration=self._lease,
            lease_owner=self._owner,
            now=self._now,
            id_factory=new_id,
        )
        key = "model:" + hashlib.sha256(identity.model_dump_json().encode()).hexdigest()
        try:
            result = await gateway.invoke(
                ToolCallContext(
                    tenant_id=identity.tenant_id,
                    user_id=identity.user_id,
                    run_id=identity.run_id,
                    tool_id=MANIFEST.tool_id,
                    params={"invocation_ref": key},
                    idempotency_key=IdempotencyKey(key),
                )
            )
            if result.status is not ToolCallStatus.SUCCEEDED:
                for check in authority_checks:
                    if check.failure:
                        raise check.failure
                raise (
                    handler.failure or quota.failure or ModelGenerationError("unknown")
                )
            handle = (result.output or {}).get("provider_ref")
            if not isinstance(handle, str):
                raise ModelGenerationError("unknown")
            return slot.take(handle)
        except (ModelGenerationError, asyncio.CancelledError):
            raise
        except Exception:  # noqa: BLE001 - 账本/协议异常不得回显原文
            raise ModelGenerationError("unknown") from None
        finally:
            slot.discard_all()
