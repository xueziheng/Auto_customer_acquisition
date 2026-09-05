"""Gateway 专用短寿命地址材料；不得放入模型、日志、工作流或审计。"""

from __future__ import annotations

from domains.outreach.schemas import MessageSendPreflight
from domains.prospecting.schemas import ContactPointKind
from domains.prospecting.service import ProspectingService
from domains.sending_identity.service import SendingIdentityService
from shared.errors import ValidationError
from shared.schemas.identifiers import TenantId
from tool_gateway.handlers.email_send import DeliveryMaterial

from .outreach_fact_readers import sending_reader_actor


class CurrentDeliveryMaterialReader:
    """只接受 Gateway 当前 preflight，逐项绑定材料，不猜地址或联系人。"""

    def __init__(
        self,
        tenant_id: TenantId,
        prospecting: ProspectingService,
        sending: SendingIdentityService,
    ) -> None:
        self._tenant, self._prospecting, self._sending = tenant_id, prospecting, sending

    async def resolve(
        self, tenant_id: TenantId, preflight: MessageSendPreflight
    ) -> DeliveryMaterial:
        if tenant_id != self._tenant or preflight.tenant_id != tenant_id:
            raise ValidationError("当前发送材料不可用")
        contact = await self._prospecting.get_contact_point(
            tenant_id, preflight.contact_point_id
        )
        identity = await self._sending.get(
            tenant_id,
            preflight.sending_identity_id,
            actor=sending_reader_actor(preflight.sending_identity_id),
        )
        if (
            contact.tenant_id != tenant_id
            or contact.account_id != preflight.account_id
            or contact.contact_point_id != preflight.contact_point_id
            or contact.kind is not ContactPointKind.EMAIL
            or identity.identity_id != preflight.sending_identity_id
        ):
            raise ValidationError("当前发送材料不可用")
        return DeliveryMaterial(
            tenant_id,
            preflight.attempt_id,
            preflight.account_id,
            preflight.contact_point_id,
            preflight.sending_identity_id,
            identity.address,
            contact.value,
        )
