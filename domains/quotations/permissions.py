"""报价准备权限仅作用于内部最小事实，不扩大CRM、文件或原件ACL。"""

from typing import Literal, Protocol

from domains.quotations.errors import QuoteContextPermissionError
from shared.schemas.identifiers import TenantId
from shared.schemas.quote_facts import QuoteEmployeeFact


class QuotePreparationPolicy(Protocol):
    """prepare/read_internal用途授权与其他动作独立。"""

    def require(self, tenant_id: TenantId, actor: QuoteEmployeeFact,
                *, action: Literal["prepare", "read_internal"]) -> str:
        """校验本次在职与租户事实，不接默认CRM许可。"""
        ...


class StrictQuotePreparationPolicy:
    """只允许现有四成本角色，不让manager/sales借内部投影读取来源。"""

    def require(self, tenant_id: TenantId, actor: QuoteEmployeeFact,
                *, action: Literal["prepare", "read_internal"]) -> str:
        """许可仅对当前租户当前员工的指定用途有效。"""
        if (actor.tenant_id != tenant_id or not actor.is_active
            or actor.role not in {"boss", "product", "sourcing", "finance"}
            or action not in {"prepare", "read_internal"}):
            raise QuoteContextPermissionError("permission_denied")
        return f"quote-preparation:{actor.role}:{action}"
