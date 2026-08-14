"""内部事务通知邮件工具：安全 ID 入参，preflight 后才物化地址与正文。"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from connectors.gmail.client import GmailConnector, GmailTransactionalSendRequest
from domains.sending_identity.service import (
    Actor as SendingIdentityActor,
)
from domains.sending_identity.service import (
    IdentityView,
    SendingIdentityService,
    SendPermission,
    SendReservation,
)
from notification_gateway.jobs import NotificationKind
from notification_gateway.models import Notification
from shared.errors import PolicyViolation, TradeOSError, TransientError, ValidationError
from shared.schemas.identifiers import (
    EmployeeId,
    IdempotencyKey,
    NotificationJobId,
    SendingIdentityId,
    TenantId,
    UserId,
)
from tool_gateway.errors import ToolCallStatus, ToolErrorCategory, ToolGatewayError
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.manifest import (
    CostClass,
    HighRiskStageProfile,
    IdempotencyRequirement,
    RiskLevel,
    ToolManifest,
)
from tool_gateway.pipeline import (
    CheckRejection,
    PreparedToolCall,
    ToolCallContext,
    ToolCallResult,
    ToolInvocationState,
)

_ULID = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"
_TENANT = re.compile(rf"tn_{_ULID}\Z")
_EMPLOYEE = re.compile(rf"emp_{_ULID}\Z")
_JOB = re.compile(rf"njb_{_ULID}\Z")
_IDENTITY = re.compile(rf"sid_{_ULID}\Z")
_TEMPLATE_CODES = frozenset(item.value for item in NotificationKind)

MANIFEST = ToolManifest(
    tool_id="notification.email.send",
    version="v1",
    description="发送固定模板的内部事务通知邮件",
    risk_level=RiskLevel.HIGH,
    cost_class=CostClass.LOW,
    requires_approval=False,
    idempotency=IdempotencyRequirement.REQUIRED,
    required_permissions=("notification:email_send",),
    checks=("tenant", "permission", "idempotency", "rate_limit"),
    high_risk_stage_profile=HighRiskStageProfile.INTERNAL_TRANSACTIONAL,
    input_schema={
        "type": "object",
        "required": (
            "notification_id",
            "recipient_employee_id",
            "template_code",
        ),
        "properties": {
            "notification_id": {"type": "string"},
            "recipient_employee_id": {"type": "string"},
            "template_code": {"type": "string"},
        },
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "required": ("provider_ref", "already_existed"),
        "properties": {
            "provider_ref": {"type": "string"},
            "already_existed": {"type": "boolean"},
        },
        "additionalProperties": False,
    },
)


@runtime_checkable
class _Recipient(Protocol):
    @property
    def tenant_id(self) -> TenantId: ...

    @property
    def employee_id(self) -> EmployeeId: ...

    @property
    def address(self) -> str: ...


@runtime_checkable
class _RecipientDirectory(Protocol):
    async def resolve(
        self, tenant_id: TenantId, employee_id: EmployeeId
    ) -> _Recipient: ...


@runtime_checkable
class _Gateway(Protocol):
    async def invoke(self, ctx: ToolCallContext) -> ToolCallResult: ...


@dataclass(frozen=True, repr=False)
class _PendingPayload:
    tenant_id: TenantId
    notification_id: NotificationJobId
    recipient_employee_id: EmployeeId
    template_code: str


@dataclass(frozen=True, repr=False)
class _EmailPayload:
    tenant_id: TenantId
    request: GmailTransactionalSendRequest = field(repr=False)


class NotificationEmailSendHandler:
    """prepared fingerprint 只含安全 ID；地址和固定内容由 rate preflight 补齐。"""

    def __init__(
        self,
        gmail: GmailConnector,
        fingerprints: HmacFingerprintProvider,
    ) -> None:
        if not isinstance(gmail, GmailConnector) or not isinstance(
            fingerprints, HmacFingerprintProvider
        ):
            raise ValidationError("事务通知 handler 依赖无效")
        self._gmail = gmail
        self._fingerprints = fingerprints
        self._bound: ContextVar[Notification | None] = ContextVar(
            "notification_email_bound", default=None
        )

    @contextmanager
    def bind(self, notification: Notification) -> Iterator[None]:
        if not isinstance(notification, Notification):
            raise ValidationError("事务通知绑定无效")
        token: Token[Notification | None] = self._bound.set(notification)
        try:
            yield
        finally:
            self._bound.reset(token)

    async def prepare(
        self, ctx: ToolCallContext, preflight: object | None
    ) -> PreparedToolCall:
        del preflight
        values = _safe_params(ctx.params)
        notification_id, employee_id, template_code = values
        notification = self._bound.get()
        if not _matches_notification(
            notification,
            ctx.tenant_id,
            notification_id,
            employee_id,
            template_code,
        ):
            raise ValidationError("事务通知绑定无效")
        fingerprint, version = self._fingerprints.fingerprint(
            (
                b"notification-email-v1",
                str(ctx.tenant_id).encode(),
                str(notification_id).encode(),
                str(employee_id).encode(),
                template_code.encode(),
            )
        )
        return PreparedToolCall(
            fingerprint,
            version,
            {
                "notification_id": str(notification_id),
                "employee_id": str(employee_id),
                "template_code": template_code,
            },
            _PendingPayload(
                ctx.tenant_id,
                notification_id,
                employee_id,
                template_code,
            ),
        )

    def materialize(
        self,
        prepared: PreparedToolCall,
        *,
        from_address: str,
        recipient_address: str,
    ) -> PreparedToolCall:
        payload = prepared.payload
        notification = self._bound.get()
        if not isinstance(payload, _PendingPayload) or not _matches_notification(
            notification,
            payload.tenant_id,
            payload.notification_id,
            payload.recipient_employee_id,
            payload.template_code,
        ):
            raise ValidationError("事务通知 payload 无效")
        assert notification is not None
        subject, body = _render_fixed_content(notification, payload.template_code)
        digest = prepared.request_fingerprint
        request = GmailTransactionalSendRequest(
            from_address=from_address,
            recipient_address=recipient_address,
            subject=subject,
            body=body,
            deterministic_message_id=(
                f"notification.{digest}@messages.tradeos.invalid"
            ),
            idempotency_header=f"notification.{digest}",
        )
        return PreparedToolCall(
            prepared.request_fingerprint,
            prepared.fingerprint_version,
            prepared.audit_projection,
            _EmailPayload(payload.tenant_id, request),
        )

    async def execute(
        self, tenant_id: TenantId, prepared: PreparedToolCall
    ) -> Mapping[str, str | bool | None]:
        payload = prepared.payload
        if not isinstance(payload, _EmailPayload) or payload.tenant_id != tenant_id:
            raise ValidationError("事务通知 payload 无效")
        result = await self._gmail.send_transactional_once(payload.request)
        return {
            "provider_ref": result.provider_ref,
            "already_existed": result.already_existed,
        }

    async def reconcile(
        self, tenant_id: TenantId, prepared: PreparedToolCall
    ) -> Mapping[str, str | bool | None]:
        payload = prepared.payload
        if not isinstance(payload, _EmailPayload) or payload.tenant_id != tenant_id:
            raise ValidationError("事务通知 payload 无效")
        result = await self._gmail.reconcile_transactional_once(payload.request)
        return {
            "provider_ref": result.provider_ref,
            "already_existed": result.already_existed,
        }


class NotificationEmailTenantCheck:
    name = "tenant"

    def __init__(self, tenant_id: TenantId) -> None:
        if not _valid(_TENANT, tenant_id):
            raise ValidationError("事务通知租户配置无效")
        self._tenant_id = tenant_id

    async def check(
        self, ctx: ToolCallContext, state: ToolInvocationState
    ) -> CheckRejection | None:
        del state
        try:
            _safe_params(ctx.params)
        except ValidationError:
            return CheckRejection(
                self.name,
                "tenant:resource_binding",
                "事务通知资源租户绑定无效",
            )
        if ctx.tenant_id != self._tenant_id or ctx.campaign_ref is not None:
            return CheckRejection(
                self.name,
                "tenant:mismatch",
                "事务通知资源租户绑定无效",
            )
        return None


class NotificationEmailRateLimitCheck:
    name = "rate_limit"

    def __init__(
        self,
        handler: NotificationEmailSendHandler,
        recipients: _RecipientDirectory,
        sending_identities: SendingIdentityService,
        sending_identity_id: SendingIdentityId,
        actor: SendingIdentityActor,
    ) -> None:
        if (
            not isinstance(handler, NotificationEmailSendHandler)
            or not isinstance(recipients, _RecipientDirectory)
            or not _valid(_IDENTITY, sending_identity_id)
            or not isinstance(actor, SendingIdentityActor)
            or not all(
                callable(getattr(sending_identities, name, None))
                for name in ("get", "check_send_permission", "reserve_send_slot")
            )
        ):
            raise ValidationError("事务通知 rate-limit 依赖无效")
        self._handler = handler
        self._recipients = recipients
        self._sending = sending_identities
        self._identity_id = sending_identity_id
        self._actor = actor

    async def check(
        self, ctx: ToolCallContext, state: ToolInvocationState
    ) -> CheckRejection | None:
        if state.prepared is None or ctx.idempotency_key is None:
            return self._rejection("notification:preflight_missing")
        payload = state.prepared.payload
        if not isinstance(payload, _PendingPayload) or payload.tenant_id != ctx.tenant_id:
            return self._rejection("notification:preflight_invalid")
        try:
            identity = await self._sending.get(
                ctx.tenant_id,
                self._identity_id,
                actor=self._actor,
            )
            if not isinstance(identity, IdentityView) or identity.role.value != "transactional":
                return self._rejection("notification:transactional_identity_required")
            permission = await self._sending.check_send_permission(
                ctx.tenant_id,
                self._identity_id,
                False,
                actor=self._actor,
            )
            if not isinstance(permission, SendPermission) or not permission.allowed:
                return self._rejection("notification:identity_unavailable")
            recipient = await self._recipients.resolve(
                ctx.tenant_id, payload.recipient_employee_id
            )
            if (
                not isinstance(recipient, _Recipient)
                or recipient.tenant_id != ctx.tenant_id
                or recipient.employee_id != payload.recipient_employee_id
            ):
                return self._rejection("notification:recipient_binding")
            materialized = self._handler.materialize(
                state.prepared,
                from_address=identity.address,
                recipient_address=recipient.address,
            )
            reservation = await self._sending.reserve_send_slot(
                ctx.tenant_id,
                self._identity_id,
                IdempotencyKey(str(ctx.idempotency_key)),
                False,
                actor=self._actor,
            )
            if not isinstance(reservation, SendReservation):
                raise ValidationError("事务通知 reservation 无效")
            state.prepared = materialized
            return None
        except (PolicyViolation, ValidationError):
            return self._rejection("notification:preflight_rejected")
        except TradeOSError as error:
            category = (
                ToolErrorCategory.PROVIDER_TRANSIENT
                if error.is_retryable
                else ToolErrorCategory.PROVIDER_PERMANENT
            )
            raise ToolGatewayError(category) from None
        except Exception:  # noqa: BLE001 -- domain/DB 原始异常不得穿透 Gateway
            raise ToolGatewayError(ToolErrorCategory.UNEXPECTED) from None

    def _rejection(self, rule: str) -> CheckRejection:
        return CheckRejection(
            self.name,
            rule,
            "事务通知发送前检查未通过",
        )


class ToolGatewayTransactionalNotificationSender:
    """worker-facing adapter；调用上下文只含通知 job/员工/固定模板代码。"""

    def __init__(
        self,
        gateway: _Gateway,
        handler: NotificationEmailSendHandler,
        user_id: UserId,
    ) -> None:
        if (
            not isinstance(gateway, _Gateway)
            or not isinstance(handler, NotificationEmailSendHandler)
            or not isinstance(user_id, str)
            or not user_id
        ):
            raise ValidationError("事务通知 sender 依赖无效")
        self._gateway = gateway
        self._handler = handler
        self._user_id = user_id

    async def send(self, notification: Notification) -> None:
        if (
            not isinstance(notification, Notification)
            or not _valid(_TENANT, notification.tenant_id)
            or not _valid(_EMPLOYEE, notification.recipient)
            or not _valid(_JOB, notification.source_job_id)
            or not isinstance(notification.context.kind, NotificationKind)
        ):
            raise PolicyViolation("事务通知发送请求无效")
        job_id = NotificationJobId(str(notification.source_job_id))
        template_code = notification.context.kind.value
        with self._handler.bind(notification):
            result = await self._gateway.invoke(
                ToolCallContext(
                    tenant_id=notification.tenant_id,
                    user_id=self._user_id,
                    tool_id=MANIFEST.tool_id,
                    params={
                        "notification_id": str(job_id),
                        "recipient_employee_id": str(notification.recipient),
                        "template_code": template_code,
                    },
                    idempotency_key=IdempotencyKey(f"notification:{job_id}"),
                )
            )
        if not isinstance(result, ToolCallResult):
            raise TransientError("事务通知邮件暂不可用")
        if result.status in {ToolCallStatus.SUCCEEDED, ToolCallStatus.DUPLICATE}:
            return
        if result.status is ToolCallStatus.FAILED_TRANSIENT:
            raise TransientError("事务通知邮件暂不可用")
        raise PolicyViolation("事务通知邮件被拒绝")


def _safe_params(
    params: Mapping[str, object],
) -> tuple[NotificationJobId, EmployeeId, str]:
    if set(params) != {
        "notification_id",
        "recipient_employee_id",
        "template_code",
    }:
        raise ValidationError("事务通知参数无效")
    notification_id = params.get("notification_id")
    employee_id = params.get("recipient_employee_id")
    template_code = params.get("template_code")
    if (
        not isinstance(notification_id, str)
        or not isinstance(employee_id, str)
        or not _valid(_JOB, notification_id)
        or not _valid(_EMPLOYEE, employee_id)
        or not isinstance(template_code, str)
        or template_code not in _TEMPLATE_CODES
    ):
        raise ValidationError("事务通知参数无效")
    return (
        NotificationJobId(notification_id),
        EmployeeId(employee_id),
        template_code,
    )


def _matches_notification(
    notification: Notification | None,
    tenant_id: TenantId,
    notification_id: NotificationJobId,
    employee_id: EmployeeId,
    template_code: str,
) -> bool:
    return (
        isinstance(notification, Notification)
        and notification.tenant_id == tenant_id
        and notification.source_job_id == notification_id
        and notification.recipient == employee_id
        and isinstance(notification.context.kind, NotificationKind)
        and notification.context.kind.value == template_code
    )


def _render_fixed_content(
    notification: Notification, template_code: str
) -> tuple[str, str]:
    if (
        template_code not in _TEMPLATE_CODES
        or notification.context.kind.value != template_code
        or not isinstance(notification.title, str)
        or not notification.title
        or not isinstance(notification.next_step, str)
        or not notification.next_step
        or not isinstance(notification.link, str)
        or not notification.link.startswith("/")
    ):
        raise ValidationError("事务通知模板无效")
    subject = f"TradeOS 通知：{notification.title}"
    body = (
        f"{notification.title}\n\n"
        f"下一步：{notification.next_step}\n"
        f"任务链接：{notification.link}\n"
    )
    return subject, body


def _valid(pattern: re.Pattern[str], value: object) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None
