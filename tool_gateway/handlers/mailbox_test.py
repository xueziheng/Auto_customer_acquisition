"""操作者本人邮箱的固定模板单封诊断；不创建 Campaign 或认证事实。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from connectors.gmail.client import (
    GmailConnector,
    GmailTransactionalSendRequest,
    SecretResolver,
)
from shared.errors import ValidationError
from shared.schemas.identifiers import IdempotencyKey, TenantId
from tool_gateway.errors import ToolErrorCategory, ToolGatewayError
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.manifest import (
    CostClass,
    IdempotencyRequirement,
    RiskLevel,
    ToolManifest,
)
from tool_gateway.pipeline import (
    CheckRejection,
    PreparedToolCall,
    ToolCallContext,
    ToolInvocationState,
)

SUBJECT = "TradeOS email delivery test"
BODY = (
    "This is a test email sent from TradeOS at your request.\n\n"
    "Please reply to this email to help verify that TradeOS can receive your reply.\n\n"
    "This message contains no marketing content or commercial offer.\n"
)
STAGES = (
    "tenant",
    "permission",
    "suppression",
    "approval",
    "idempotency",
    "rate_limit",
)
MANIFEST = ToolManifest(
    tool_id="email.mailbox.test",
    version="v1",
    description="经本人逐次确认向本人邮箱发送一封固定诊断邮件",
    risk_level=RiskLevel.HIGH,
    cost_class=CostClass.LOW,
    requires_approval=True,
    idempotency=IdempotencyRequirement.REQUIRED,
    required_permissions=("mailbox:test",),
    checks=STAGES,
    input_schema={
        "type": "object",
        "required": ["grant_id"],
        "properties": {"grant_id": {"type": "string"}},
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "required": ["provider_ref", "already_existed"],
        "properties": {
            "provider_ref": {"type": "string"},
            "already_existed": {"type": "boolean"},
        },
        "additionalProperties": False,
    },
)


@dataclass(frozen=True, repr=False)
class MailTestGrant:
    """私有持久授权精确绑定租户、员工、发件人及操作者声明的本人收件箱。"""

    grant_id: str
    tenant_id: TenantId
    employee_id: str
    sender: str
    recipient: str
    approved_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        import re

        if not re.fullmatch(r"mtg_[0-7][0-9A-HJKMNP-TV-Z]{25}", self.grant_id):
            raise ValidationError("邮箱测试授权无效")
        if (
            self.approved_at.tzinfo is None
            or self.expires_at.tzinfo is None
            or not timedelta(0)
            < self.expires_at - self.approved_at
            <= timedelta(hours=1)
        ):
            raise ValidationError("邮箱测试授权期限无效")
        self.request("0" * 64)

    @property
    def key(self) -> IdempotencyKey:
        return IdempotencyKey("mailbox-test:" + self.grant_id)

    def request(self, digest: str) -> GmailTransactionalSendRequest:
        return GmailTransactionalSendRequest(
            self.sender,
            self.recipient,
            SUBJECT,
            BODY,
            f"mailtest.{digest}@messages.tradeos.invalid",
            f"mailtest.{digest}",
        )

    def matches(self, ctx: ToolCallContext) -> bool:
        return (
            ctx.tenant_id == self.tenant_id
            and ctx.user_id == self.employee_id
            and ctx.tool_id == MANIFEST.tool_id
            and ctx.run_id is None
            and ctx.campaign_ref is None
            and ctx.approval_ref == self.grant_id
            and ctx.idempotency_key == self.key
            and dict(ctx.params) == {"grant_id": self.grant_id}
        )


class MailTestPolicy(Protocol):
    async def check(self, stage: str, grant: MailTestGrant) -> bool:
        """检查当前私有授权、在职老板、邮箱绑定、信誉限制和已有抑制事实。"""
        ...


@dataclass(frozen=True, repr=False)
class _Payload:
    tenant_id: TenantId
    request: GmailTransactionalSendRequest = field(repr=False)


class MailTestCheck:
    """六阶段逐次校验；稳定授权键同时限定最多一次已确认发送。"""

    def __init__(
        self,
        name: str,
        grant: MailTestGrant,
        policy: MailTestPolicy,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if name not in STAGES:
            raise ValidationError("邮箱测试阶段无效")
        self.name, self._grant, self._policy, self._now = name, grant, policy, now

    async def check(
        self, ctx: ToolCallContext, state: ToolInvocationState
    ) -> CheckRejection | None:
        del state
        if (
            not self._grant.matches(ctx)
            or not self._grant.approved_at <= self._now() < self._grant.expires_at
            or not await self._policy.check(self.name, self._grant)
        ):
            if self.name == "rate_limit":
                # 幂等占位后必须收敛 canonical 失败，不能再完结 received 请求。
                raise ToolGatewayError(ToolErrorCategory.PERMISSION_DENIED)
            return CheckRejection(
                self.name, "mailbox_test:denied", "本人邮箱测试授权或当前条件不满足"
            )
        return None


class MailTestHandler:
    """只接受固定模板；Gmail 凭证在 EXECUTING 持久化后才解析。"""

    def __init__(
        self,
        grant: MailTestGrant,
        fingerprints: HmacFingerprintProvider,
        gmail: GmailConnector,
        secrets: SecretResolver,
    ) -> None:
        self._grant, self._fingerprints, self._gmail, self._secrets = (
            grant,
            fingerprints,
            gmail,
            secrets,
        )

    async def prepare(
        self, ctx: ToolCallContext, preflight: object | None = None
    ) -> PreparedToolCall:
        del preflight
        if not self._grant.matches(ctx):
            raise ValidationError("邮箱测试调用与授权不匹配")
        digest, version = self._fingerprints.fingerprint(
            tuple(
                value.encode()
                for value in (
                    "mailbox-test-v1",
                    self._grant.grant_id,
                    str(self._grant.tenant_id),
                    self._grant.employee_id,
                    self._grant.sender,
                    self._grant.recipient,
                    SUBJECT,
                    BODY,
                )
            )
        )
        return PreparedToolCall(
            digest,
            version,
            {"grant_id": self._grant.grant_id},
            _Payload(ctx.tenant_id, self._grant.request(digest)),
        )

    async def _request(
        self, tenant_id: TenantId, prepared: PreparedToolCall
    ) -> GmailTransactionalSendRequest:
        if (
            not isinstance(prepared.payload, _Payload)
            or prepared.payload.tenant_id != tenant_id
        ):
            raise ValidationError("邮箱测试材料绑定无效")
        await self._gmail.configure(self._secrets)
        return prepared.payload.request

    async def execute(
        self, tenant_id: TenantId, prepared: PreparedToolCall
    ) -> Mapping[str, str | bool | None]:
        result = await self._gmail.send_transactional_once(
            await self._request(tenant_id, prepared)
        )
        return {
            "provider_ref": result.provider_ref,
            "already_existed": result.already_existed,
        }

    async def reconcile(
        self, tenant_id: TenantId, prepared: PreparedToolCall
    ) -> Mapping[str, str | bool | None]:
        result = await self._gmail.reconcile_transactional_once(
            await self._request(tenant_id, prepared)
        )
        return {
            "provider_ref": result.provider_ref,
            "already_existed": result.already_existed,
        }
