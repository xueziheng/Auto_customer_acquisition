"""客户入站资料沿现API的boss-only读取下限，不扩张会话权限。"""

from shared.schemas.evidence_read import QuoteEvidenceError, QuoteMessageReferenceFact
from shared.schemas.identifiers import TenantId
from shared.schemas.quote_facts import QuoteEmployeeFact


def require_inbound_source_access(
    tenant_id: TenantId, actor: QuoteEmployeeFact, message: QuoteMessageReferenceFact
) -> None:
    """纯当前身份与真实email入站检查，无数据访问。"""
    if (
        actor.tenant_id != tenant_id
        or message.tenant_id != tenant_id
        or actor.is_active is not True
        or actor.role != "boss"
        or message.channel != "email"
        or message.direction != "inbound"
    ):
        raise QuoteEvidenceError("permission_denied")


from typing import Literal

from shared.errors import PermissionDenied


def require_inbound_review_access(
    tenant_id: TenantId, actor: QuoteEmployeeFact, *, action: Literal["read", "retry"]
) -> None:
    """当前active boss才能读取技术待核对与原位恢复；不需要或伪造Message。"""
    if (
        action not in {"read", "retry"}
        or actor.tenant_id != tenant_id
        or actor.is_active is not True
        or actor.role != "boss"
    ):
        raise PermissionDenied("入站待核对权限拒绝")
