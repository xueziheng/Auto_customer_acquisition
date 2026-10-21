"""真人管理员的固定供应商样本诊断；结果不进入联系人或发送流程。"""
from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from connectors.hunter.client import (
    HunterConnector,
    HunterConnectorError,
    HunterSecretResolver,
)
from connectors.hunter.secrets import BoundHunterSecretResolver
from shared.errors import ValidationError
from shared.schemas.identifiers import TenantId
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.contact_provider_errors import map_contact_provider_error
from tool_gateway.manifest import (
    CostClass,
    IdempotencyRequirement,
    RiskLevel,
    ToolManifest,
)
from tool_gateway.pipeline import PreparedToolCall, SafeScalar, ToolCallContext

HUNTER_DIAGNOSTIC_MANIFEST = ToolManifest(
    tool_id="provider.hunter.sample", version="v1",
    description="管理员显式查询固定供应商域名样本，只返回计数",
    risk_level=RiskLevel.MEDIUM, cost_class=CostClass.LOW, requires_approval=False,
    idempotency=IdempotencyRequirement.REQUIRED,
    required_permissions=("platform:configure",),
    checks=("tenant", "permission", "idempotency", "rate_limit"),
    input_schema={
        "type": "object",
        "properties": {"configuration_version": {
            "type": "string", "pattern": "^[a-z0-9][a-z0-9._-]{0,31}$",
        }},
        "required": ("configuration_version",), "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "provider_ref": {"type": "string"},
            "status": {"type": "string", "enum": ("validation_passed",)},
        },
        "required": ("provider_ref", "status"), "additionalProperties": False,
    },
)


@dataclass(frozen=True)
class HunterDiagnosticSummary:
    """仅保存结构化计数；不保存邮箱、姓名或 Provider 原文。"""
    email_count: int
    named_contact_count: int
    source_count: int


class HunterDiagnosticHandler:
    """单次受信调用装配，固定 hunter.io/最多十项/每配置版本同一幂等键。

    这只是供应商接入诊断，不执行客户获客；业务补全仍走原 contact.enrich 门禁。
    authorize 必须重新读取当前平台授权；Gateway 先持久提交 EXECUTING 再调用本执行器。
    """

    def __init__(
        self, *, tenant_id: TenantId, actor_id: str, configuration_version: str,
        authorize: Callable[[], Awaitable[None]], connector: HunterConnector,
        resolver: HunterSecretResolver, secret_ref: str,
        fingerprints: HmacFingerprintProvider,
    ) -> None:
        if (
            not isinstance(configuration_version, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,31}", configuration_version) is None
            or not isinstance(tenant_id, str) or not tenant_id
            or not isinstance(actor_id, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}", actor_id) is None
            or not callable(authorize) or not isinstance(connector, HunterConnector)
            or not isinstance(fingerprints, HmacFingerprintProvider)
        ):
            raise ValidationError("诊断配置版本无效")
        self._tenant = tenant_id
        self._actor = actor_id
        self._version = configuration_version
        self._authorize = authorize
        self._connector = connector
        self._resolver = BoundHunterSecretResolver(resolver, secret_ref)
        self._fingerprints = fingerprints
        self.summary: HunterDiagnosticSummary | None = None
        self._prepare_started = False
        self._execution_started = False
        self._prepared: PreparedToolCall | None = None

    @property
    def idempotency_key(self) -> str:
        """固定配置版本只能进行一次有费用的样本请求，不提供自动换键重试。"""
        return "hunter-sample:" + self._version

    async def prepare(self, ctx: ToolCallContext, preflight: object | None) -> PreparedToolCall:
        """校验精确绑定，不接受任意域名、分页、数量或任意幂等键。"""
        if (
            self._prepare_started or preflight is not None or ctx.tenant_id != self._tenant
            or str(ctx.user_id) != self._actor
            or ctx.tool_id != HUNTER_DIAGNOSTIC_MANIFEST.tool_id
            or dict(ctx.params) != {"configuration_version": self._version}
            or ctx.idempotency_key != self.idempotency_key
        ):
            raise ValidationError("诊断调用范围无效")
        self._prepare_started = True
        await self._authorize()
        self.summary = None
        digest, version = self._fingerprints.fingerprint((
            str(self._tenant).encode(), b"hunter.io", self._version.encode(),
        ))
        prepared = PreparedToolCall(digest, version, {
            "provider": "hunter", "configuration_version": self._version,
            "sample_limit": 10,
        }, self._version)
        self._prepared = prepared
        return prepared

    async def execute(
        self, tenant_id: TenantId, prepared: PreparedToolCall,
    ) -> Mapping[str, SafeScalar]:
        """授权复核后仅访问一次 Domain Search，立即丢弃 PII，仅保留计数。"""
        if (
            self._execution_started or tenant_id != self._tenant
            or prepared is not self._prepared or prepared is None
        ):
            raise ValidationError("诊断执行范围无效")
        # 在首个 await 前领取，撤权、取消和故障同样消耗本实例，禁止直接重入造成重复付费。
        self._execution_started = True
        await self._authorize()
        try:
            await self._connector.configure(self._resolver)
            result = await self._connector.find_contacts("hunter.io", ())
        except HunterConnectorError as error:
            raise map_contact_provider_error(error) from None
        await self._authorize()
        self.summary = HunterDiagnosticSummary(
            email_count=len(result.candidates),
            named_contact_count=sum(candidate.full_name is not None for candidate in result.candidates),
            source_count=sum(len(candidate.sources) for candidate in result.candidates),
        )
        return {"provider_ref": "hunter-sample:" + self._version, "status": "validation_passed"}
