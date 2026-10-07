"""Email send 工具的 ephemeral material 适配器。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from connectors.gmail.client import GmailConnector, GmailSendRequest
from domains.outreach.schemas import (
    DeliveryCorrelationBinding,
    MessageAttemptView,
    MessageSendPreflight,
)
from domains.outreach.service import Actor, OutreachService
from shared.errors import ValidationError
from shared.schemas.identifiers import (
    ContactPointId,
    MessageAttemptId,
    ProspectAccountId,
    SendingIdentityId,
    TenantId,
)
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.pipeline import PreparedToolCall, ToolCallContext

_STABLE_LINK_LABEL_RE = re.compile(r"[a-z0-9-]{1,32}")


@dataclass(frozen=True)
class DeliveryMaterial:
    tenant_id: TenantId
    attempt_id: MessageAttemptId
    account_id: ProspectAccountId
    contact_point_id: ContactPointId
    sending_identity_id: SendingIdentityId
    from_address: str = field(repr=False)
    recipient_address: str = field(repr=False)

    def __post_init__(self) -> None:
        for value in (
            self.tenant_id,
            self.attempt_id,
            self.account_id,
            self.contact_point_id,
            self.sending_identity_id,
        ):
            if not isinstance(value, str) or not value:
                raise ValidationError("发送材料绑定无效")


@runtime_checkable
class DeliveryMaterialProvider(Protocol):
    async def resolve(
        self, tenant_id: TenantId, preflight: MessageSendPreflight
    ) -> DeliveryMaterial: ...


@runtime_checkable
class UnsubscribeLink(Protocol):
    @property
    def tenant_id(self) -> TenantId: ...

    @property
    def attempt_id(self) -> MessageAttemptId: ...

    @property
    def contact_point_id(self) -> ContactPointId: ...

    @property
    def url(self) -> str: ...

    @property
    def active_key_id(self) -> str: ...

    @property
    def policy_version(self) -> str: ...


@runtime_checkable
class UnsubscribeLinkProvider(Protocol):
    async def build(
        self, tenant_id: TenantId, preflight: MessageSendPreflight
    ) -> UnsubscribeLink: ...


class DeliveryCorrelationActorFactory(Protocol):
    def __call__(self, attempt_id: MessageAttemptId) -> Actor: ...


@dataclass(frozen=True)
class _EmailPayload:
    tenant_id: TenantId
    request: GmailSendRequest = field(repr=False)


class EmailSendHandler:
    """在 current-fact preflight 之后解析材料，执行时只调用 Gmail。"""

    def __init__(
        self,
        gmail: GmailConnector,
        materials: DeliveryMaterialProvider,
        unsubscribe_links: UnsubscribeLinkProvider,
        fingerprints: HmacFingerprintProvider,
        *,
        outreach: OutreachService,
        outreach_actor_factory: DeliveryCorrelationActorFactory,
        route_id: str,
    ) -> None:
        if not isinstance(materials, DeliveryMaterialProvider):
            raise ValidationError("发送材料 provider 无效")
        if not isinstance(unsubscribe_links, UnsubscribeLinkProvider):
            raise ValidationError("退订链接 provider 无效")
        self._gmail = gmail
        self._materials = materials
        self._unsubscribe_links = unsubscribe_links
        self._fingerprints = fingerprints
        self._outreach = outreach
        self._outreach_actor_factory = outreach_actor_factory
        self._route_id = DeliveryCorrelationBinding(
            deterministic_message_id=(
                f"<{route_id}.{'0' * 64}@messages.tradeos.invalid>"
            ),
            idempotency_header=f"{route_id}.{'0' * 64}",
            route_id=route_id,
        ).route_id

    async def prepare(
        self, ctx: ToolCallContext, preflight: object | None = None
    ) -> PreparedToolCall:
        if not isinstance(preflight, MessageSendPreflight):
            raise ValidationError("发送 preflight 无效")
        if ctx.tenant_id != preflight.tenant_id:
            raise ValidationError("发送 preflight 绑定无效")
        subject = ctx.params.get("subject")
        body = ctx.params.get("body")
        if not isinstance(subject, str) or not isinstance(body, str):
            raise ValidationError("客户可见内容无效")
        material = await self._materials.resolve(ctx.tenant_id, preflight)
        if not isinstance(material, DeliveryMaterial):
            raise ValidationError("发送材料无效")
        expected = (
            preflight.tenant_id,
            preflight.attempt_id,
            preflight.account_id,
            preflight.contact_point_id,
            preflight.sending_identity_id,
        )
        actual = (
            material.tenant_id,
            material.attempt_id,
            material.account_id,
            material.contact_point_id,
            material.sending_identity_id,
        )
        if actual != expected:
            raise ValidationError("发送材料绑定无效")
        correlation_digest, _correlation_version = self._fingerprints.fingerprint(
            (
                b"gmail-delivery-correlation-v1",
                str(preflight.tenant_id).encode(),
                str(preflight.idempotency_key).encode(),
            )
        )
        binding = DeliveryCorrelationBinding(
            deterministic_message_id=(
                f"<{self._route_id}.{correlation_digest}@messages.tradeos.invalid>"
            ),
            idempotency_header=f"{self._route_id}.{correlation_digest}",
            route_id=self._route_id,
        )
        bound_attempt = await self._outreach.bind_delivery_correlation(
            ctx.tenant_id,
            preflight.attempt_id,
            binding,
            actor=self._outreach_actor_factory(preflight.attempt_id),
        )
        if not isinstance(bound_attempt, MessageAttemptView) or (
            bound_attempt.tenant_id,
            bound_attempt.attempt_id,
            bound_attempt.campaign_id,
            bound_attempt.enrollment_id,
            bound_attempt.campaign_version,
            bound_attempt.step_number,
            bound_attempt.sending_identity_id,
            bound_attempt.idempotency_key,
            bound_attempt.deterministic_message_id,
            bound_attempt.idempotency_header,
        ) != (
            preflight.tenant_id,
            preflight.attempt_id,
            preflight.campaign_id,
            preflight.enrollment_id,
            preflight.campaign_version,
            preflight.step_number,
            preflight.sending_identity_id,
            preflight.idempotency_key,
            binding.deterministic_message_id,
            binding.idempotency_header,
        ):
            raise ValidationError("发送关联绑定结果无效")
        unsubscribe_link = await self._unsubscribe_links.build(
            ctx.tenant_id, preflight
        )
        if not isinstance(unsubscribe_link, UnsubscribeLink):
            raise ValidationError("退订链接无效")
        if (
            unsubscribe_link.tenant_id != preflight.tenant_id
            or unsubscribe_link.attempt_id != preflight.attempt_id
            or unsubscribe_link.contact_point_id != preflight.contact_point_id
            or not isinstance(unsubscribe_link.active_key_id, str)
            or _STABLE_LINK_LABEL_RE.fullmatch(unsubscribe_link.active_key_id) is None
            or not isinstance(unsubscribe_link.policy_version, str)
            or _STABLE_LINK_LABEL_RE.fullmatch(unsubscribe_link.policy_version) is None
        ):
            raise ValidationError("退订链接绑定无效")
        request = GmailSendRequest(
            from_address=material.from_address,
            recipient_address=material.recipient_address,
            subject=subject,
            body=body,
            unsubscribe_url=unsubscribe_link.url,
            deterministic_message_id=(
                binding.deterministic_message_id.removeprefix("<").removesuffix(">")
            ),
            idempotency_header=binding.idempotency_header,
        )
        fingerprint, version = self._fingerprints.fingerprint(
            (
                str(preflight.tenant_id).encode(),
                str(preflight.attempt_id).encode(),
                str(preflight.campaign_id).encode(),
                str(preflight.enrollment_id).encode(),
                str(preflight.account_id).encode(),
                str(preflight.contact_point_id).encode(),
                str(preflight.sending_identity_id).encode(),
                str(preflight.campaign_version).encode(),
                str(preflight.step_number).encode(),
                str(preflight.idempotency_key).encode(),
                material.from_address.encode(),
                material.recipient_address.encode(),
                subject.encode(),
                body.encode(),
                str(unsubscribe_link.tenant_id).encode(),
                str(unsubscribe_link.attempt_id).encode(),
                str(unsubscribe_link.contact_point_id).encode(),
                unsubscribe_link.active_key_id.encode(),
                unsubscribe_link.policy_version.encode(),
                self._route_id.encode(),
                correlation_digest.encode(),
            )
        )
        return PreparedToolCall(
            request_fingerprint=fingerprint,
            fingerprint_version=version,
            audit_projection={
                "attempt_id": str(preflight.attempt_id),
                "subject_bytes": len(subject.encode()),
                "body_bytes": len(body.encode()),
                "has_unsubscribe": True,
            },
            payload=_EmailPayload(ctx.tenant_id, request),
        )

    async def execute(
        self, tenant_id: TenantId, prepared: PreparedToolCall
    ) -> dict[str, str | bool | None]:
        payload = prepared.payload
        if not isinstance(payload, _EmailPayload) or payload.tenant_id != tenant_id:
            raise ValidationError("发送 payload 无效")
        result = await self._gmail.send_once(payload.request)
        return {
            "provider_ref": result.provider_ref,
            "already_existed": result.already_existed,
        }

    async def reconcile(
        self, tenant_id: TenantId, prepared: PreparedToolCall
    ) -> dict[str, str | bool | None]:
        """模糊发送只查询既有 Gmail 消息，搜索未命中也不得重发。"""
        payload = prepared.payload
        if not isinstance(payload, _EmailPayload) or payload.tenant_id != tenant_id:
            raise ValidationError("发送 payload 无效")
        result = await self._gmail.reconcile_once(payload.request)
        return {
            "provider_ref": result.provider_ref,
            "already_existed": result.already_existed,
        }
