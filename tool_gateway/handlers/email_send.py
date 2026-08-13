"""Email send 工具的 ephemeral material 适配器。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from connectors.gmail.client import GmailConnector, GmailSendRequest
from domains.outreach.schemas import MessageSendPreflight
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
class UnsubscribeLinkProvider(Protocol):
    async def build(
        self, tenant_id: TenantId, preflight: MessageSendPreflight
    ) -> str: ...


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
    ) -> None:
        if not isinstance(materials, DeliveryMaterialProvider):
            raise ValidationError("发送材料 provider 无效")
        if not isinstance(unsubscribe_links, UnsubscribeLinkProvider):
            raise ValidationError("退订链接 provider 无效")
        self._gmail = gmail
        self._materials = materials
        self._unsubscribe_links = unsubscribe_links
        self._fingerprints = fingerprints

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
        unsubscribe_url = await self._unsubscribe_links.build(ctx.tenant_id, preflight)
        message_digest, _message_version = self._fingerprints.fingerprint(
            (
                b"gmail-message-id-v1",
                str(preflight.tenant_id).encode(),
                str(preflight.idempotency_key).encode(),
            )
        )
        header_digest, _header_version = self._fingerprints.fingerprint(
            (
                b"gmail-idempotency-header-v1",
                str(preflight.tenant_id).encode(),
                str(preflight.idempotency_key).encode(),
            )
        )
        request = GmailSendRequest(
            from_address=material.from_address,
            recipient_address=material.recipient_address,
            subject=subject,
            body=body,
            unsubscribe_url=unsubscribe_url,
            deterministic_message_id=f"{message_digest}@messages.tradeos.invalid",
            idempotency_header=header_digest,
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
                unsubscribe_url.encode(),
                message_digest.encode(),
                header_digest.encode(),
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
