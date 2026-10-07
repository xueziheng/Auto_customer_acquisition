"""成本政策selection租约的显式报价DTO适配，不复制选择算法。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from domains.costing.errors import CostFreezeError, CostFreezeUnavailableError
from domains.costing.service import CostingApprovalPolicyReader, CostingPolicySelection
from domains.quotations.errors import QuoteApprovalError, QuoteApprovalUnavailableError
from domains.quotations.schemas import QuotePolicySnapshot
from domains.quotations.service import QuotePolicySelection
from shared.schemas.identifiers import TenantId
from workflows.quote_approval.basis_adapter import _policy


class _Selection:
    """只转换当前锁内结果，source也按已有显式字段映射。"""

    def __init__(self, selection: CostingPolicySelection) -> None:
        """传入真实成本租约，不缓存历史选择。"""
        self._selection = selection

    async def current(self) -> QuotePolicySnapshot:
        """缺政策固定阻断，不以旧snapshot或默认政策替代。"""
        try:
            return _policy(await self._selection.current())
        except CostFreezeError as error:
            if error.code == "policy_missing":
                raise QuoteApprovalError("policy_stale") from None
            raise QuoteApprovalError("evidence_invalid") from None
        except CostFreezeUnavailableError as error:
            raise QuoteApprovalUnavailableError(
                "lock_timeout"
                if error.code == "lock_timeout"
                else "dependency_unavailable"
            ) from None


class CostingQuoteApprovalPolicyReader:
    """成本持锁生命周期完整传递给报价session，退出顺序由报价拥有。"""

    def __init__(self, costing: CostingApprovalPolicyReader) -> None:
        """依赖必填，无系统CostingActor或默认允许。"""
        self._costing = costing

    @asynccontextmanager
    async def open(
        self, tenant_id: TenantId, category: str | None
    ) -> AsyncIterator[QuotePolicySelection]:
        """成本租约关闭前，报价调用方必须已提交或回滚。"""
        try:
            async with self._costing.open(tenant_id, category) as selection:
                yield _Selection(selection)
        except CostFreezeUnavailableError as error:
            raise QuoteApprovalUnavailableError(
                "lock_timeout"
                if error.code == "lock_timeout"
                else "dependency_unavailable"
            ) from None
