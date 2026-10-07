"""研究确认前的应用投影；GET 只读持久状态，不调用 connector 或解析凭证。"""

import json
from dataclasses import asdict
from typing import Protocol

from agent_runtime.assistant.discovery_queries import query_source_channel
from connectors.search_contracts import SearchCostStatus
from domains.directives.schemas import ProposalView
from domains.directives.service import DirectiveService
from shared.errors import PermissionDenied
from shared.schemas.identifiers import RunId, TenantId
from tool_gateway.free_search_contracts import SearchQuotaSnapshot

from .research_schemas import (
    DiscoveryExecutionView,
    DiscoveryProposalView,
    ResearchAccessState,
    ResearchAccessView,
)


class DiscoveryExecutionReader(Protocol):
    """按 tenant 与提案幂等键精确读取所有状态 Run，不限最近 N 条。"""

    async def find_run(self, tenant_id: TenantId, proposal_id: str) -> RunId | None: ...


async def read_discovery_execution(
    tenant_id: TenantId,
    proposal: ProposalView,
    directives: DirectiveService,
    access: "ResearchAccessService",
    reader: DiscoveryExecutionReader | None,
) -> DiscoveryExecutionView:
    """只读恢复已持久回执；确认但未启动时，仅当前有效提案可显式恢复。"""
    if reader is None:
        return DiscoveryExecutionView(state="unknown")
    try:
        run_id = await reader.find_run(tenant_id, proposal.proposal_id)
    except Exception:  # noqa: BLE001 查询失败不推断没有 Run，也不泄露存储详情
        return DiscoveryExecutionView(state="unknown")
    if run_id is not None:
        return DiscoveryExecutionView(state="started", run_id=str(run_id))
    if proposal.state != "confirmed":
        return DiscoveryExecutionView(state="not_started")
    active = await directives.get_active(tenant_id)
    projected = await access.proposal(tenant_id, proposal)
    return DiscoveryExecutionView(
        state="not_started",
        can_resume=(
            projected.can_confirm
            and active is not None
            and active.source_proposal_id == proposal.proposal_id
        ),
    )


class SearchQuotaSnapshotReader(Protocol):
    """仅允许读取快照，接口不提供 usage 或 credential 访问。"""

    async def snapshot(self) -> SearchQuotaSnapshot | None: ...


class ResearchAccessService:
    """tenant-bound 的受信配置标记和持久额度展示，不替代执行门禁。"""

    def __init__(
        self,
        tenant_id: TenantId,
        quota: SearchQuotaSnapshotReader | None,
        *,
        configured: bool,
    ) -> None:
        self._tenant_id = tenant_id
        self._quota = quota
        self._configured = configured

    async def status(self, tenant_id: TenantId) -> ResearchAccessView:
        """首次未建账户仍可请求核验；不能把配置存在说成账户或 worker 就绪。"""
        if tenant_id != self._tenant_id:
            raise PermissionDenied("研究配置不属于当前租户")
        if not self._configured:
            return ResearchAccessView(
                state="not_configured", can_confirm_research=False
            )
        try:
            snapshot = await self._quota.snapshot() if self._quota is not None else None
        except Exception:  # noqa: BLE001 只读故障一律脱敏并拒绝确认，不泄露存储异常
            return ResearchAccessView(state="snapshot_unavailable", can_confirm_research=False)
        if snapshot is None:
            return ResearchAccessView(
                state="configured_unverified", can_confirm_research=True
            )
        if snapshot.tenant_id != tenant_id:
            raise PermissionDenied("研究快照不属于当前租户")
        state: ResearchAccessState
        remaining = None
        if (
            snapshot.paygo_enabled is True
            or snapshot.cost_status is SearchCostStatus.PAID
        ):
            state = "paid_enabled"
        elif (
            not snapshot.included_credits_free
            and (
                snapshot.cost_status is not SearchCostStatus.FREE
                or snapshot.paygo_enabled is not False
            )
        ):
            state = "usage_unknown"
        else:
            remaining = snapshot.remaining
            state = "quota_exhausted" if remaining == 0 else "free_last_verified"
        return ResearchAccessView(
            state=state,
            can_confirm_research=True,
            confirmation_requires_recheck=state != "free_last_verified",
            remaining_lower_bound=remaining,
            checked_at=snapshot.checked_at,
        )

    async def proposal(
        self, tenant_id: TenantId, proposal: ProposalView
    ) -> DiscoveryProposalView:
        """使用服务端投影决定确认按钮；历史缺省保持触达准备语义。"""
        if tenant_id != self._tenant_id:
            raise PermissionDenied("研究提案不属于当前租户")
        research = proposal.parsed_fields.get("execution_mode") == "research_only"
        access = await self.status(tenant_id) if research else None
        reason = None
        lanes: tuple[str, ...] = ()
        source_channels: tuple[str, ...] = ()
        if research:
            try:
                queries = json.loads(proposal.parsed_fields.get("queries", "[]"))
                queries = queries if isinstance(queries, list) else []
                lanes = tuple(
                    dict.fromkeys(
                        item["discovery_lane"]
                        for item in queries
                        if isinstance(item, dict)
                        and item.get("discovery_lane")
                        in {"importer", "distributor", "ecommerce"}
                    )
                )
                source_channels = tuple(
                    dict.fromkeys(
                        query_source_channel(item["query"])
                        for item in queries
                        if isinstance(item, dict)
                        and isinstance(item.get("query"), str)
                        and item["query"].strip()
                    )
                )
            except (ValueError, TypeError):
                lanes = ()
            if any(
                not proposal.parsed_fields.get(key, "").isdigit()
                or int(proposal.parsed_fields[key]) <= 0
                for key in (
                    "max_search_queries",
                    "max_pages_read",
                    "max_signals",
                    "max_hypotheses",
                )
            ):
                reason = "budget_missing"
            elif access is not None and not access.can_confirm_research:
                reason = access.state
        if proposal.state not in {"pending_confirmation", "confirmed"}:
            reason = "proposal_not_confirmable"
        return DiscoveryProposalView(
            **asdict(proposal),
            execution_mode="research_only" if research else "outreach_preparation",
            planned_discovery_lanes=lanes,
            planned_source_channels=source_channels,
            can_confirm=reason is None,
            confirmation_blocked_reason=reason,
            research_access=access,
        )
