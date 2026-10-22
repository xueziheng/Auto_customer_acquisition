"""人工筛选的可信来源绑定与研究排程应用编排；不包含外部调用或发送授权。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from domains.demand.schemas import DemandSignalView
from domains.directives.service import DirectiveService
from domains.products.service import EnterpriseKnowledgeService
from domains.prospecting.schemas import ProspectAccountView
from domains.prospecting.service import (
    ProspectingService,
    ProspectingServicePlanStore,
    ScreeningAssessment,
    ScreeningEvidence,
    ScreeningEvidenceKind,
    ServicePlanScheduleBody,
    ServicePlanState,
    evaluate_screening,
)
from shared.errors import (
    PermissionDenied,
    TenantIsolationViolation,
    TransientError,
    ValidationError,
)
from shared.schemas.identifiers import EmployeeId, ProspectAccountId, TenantId, UserId
from workflows.enterprise_knowledge.confirmed_context import ConfirmedContextReader


class ScreeningEvidenceReader(Protocol):
    """只读取当前租户企业已经绑定的研究信号，不接受任意网页地址。"""

    async def for_accounts(
        self, tenant_id: TenantId, accounts: list[ProspectAccountView]
    ) -> dict[str, tuple[DemandSignalView, ...]]:
        """由受信适配器按租户及信号引用返回 canonical 原始来源。"""
        ...


@dataclass(frozen=True)
class ScreeningApplicationResult:
    """提供给表现层的确定性评估与已记录证据，不携带内部存储身份。"""

    assessment: ScreeningAssessment | None
    evidence: tuple[ScreeningEvidence, ...]
    eligible_signals: tuple[DemandSignalView, ...] = ()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _required_store(
    store: ProspectingServicePlanStore | None,
) -> ProspectingServicePlanStore:
    if store is None:
        raise TransientError("获客服务计划尚未配置")
    return store


class ProspectScreeningApplication:
    """将人工分类绑定到真实企业信号；筛选判定仍只调用潜客域规则。"""

    def __init__(
        self,
        prospecting: ProspectingService | None,
        reader: ScreeningEvidenceReader | None,
        store: ProspectingServicePlanStore | None,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._prospecting = prospecting
        self._reader = reader
        self._plan_store = store
        self._clock = clock

    async def _account(
        self, tenant_id: TenantId, account_id: str
    ) -> ProspectAccountView:
        if self._prospecting is None:
            raise TransientError("潜客服务尚未配置")
        if not account_id or len(account_id) > 40 or account_id != account_id.strip():
            raise ValidationError("潜在企业标识无效")
        account = await self._prospecting.get_account(
            tenant_id, ProspectAccountId(account_id)
        )
        if account.tenant_id != tenant_id or str(account.account_id) != account_id:
            raise TenantIsolationViolation("筛选证据企业归属不匹配")
        return account

    async def _eligible_signals(
        self, tenant_id: TenantId, account: ProspectAccountView
    ) -> tuple[DemandSignalView, ...]:
        """候选和写入复用同一服务端筛选，不把关联推断或缺原件信号作为证据。"""
        if self._reader is None:
            return ()
        records = await self._reader.for_accounts(tenant_id, [account])
        return tuple(
            signal
            for signal in records.get(str(account.account_id), ())
            if signal.signal_id in account.source_signal_refs
            and not signal.is_inference
            and signal.source_type == "web_page"
            and signal.source_url
            and signal.page_hash
            and signal.snapshot_artifact_ref
            and signal.raw_observation.strip()
        )

    async def get(
        self, tenant_id: TenantId, account_id: str
    ) -> ScreeningApplicationResult:
        """核验企业归属后读取证据；没有服务计划时保留未配置评估。"""
        account = await self._account(tenant_id, account_id)
        store = _required_store(self._plan_store)
        plan = await store.get(tenant_id)
        evidence = await store.get_evidence(tenant_id, ProspectAccountId(account_id))
        return ScreeningApplicationResult(
            assessment=(
                evaluate_screening(
                    plan.tier,
                    tenant_id,
                    ProspectAccountId(account_id),
                    evidence,
                    self._clock(),
                )
                if plan
                else None
            ),
            evidence=evidence,
            eligible_signals=await self._eligible_signals(tenant_id, account),
        )

    async def record(
        self,
        tenant_id: TenantId,
        account_id: str,
        *,
        reviewer: EmployeeId,
        kind: ScreeningEvidenceKind,
        signal_id: str,
        occurred_at: datetime | None,
    ) -> ScreeningApplicationResult:
        """只接受分类与发生时间；原文、URL和复核身份均由受信调用链绑定。"""
        account = await self._account(tenant_id, account_id)
        if self._reader is None:
            raise TransientError("筛选证据读取器尚未配置")
        records = await self._eligible_signals(tenant_id, account)
        signal = next(
            (
                item
                for item in records
                if item.signal_id == signal_id
                and item.signal_id in account.source_signal_refs
            ),
            None,
        )
        if signal is None or not signal.source_url:
            raise ValidationError("筛选只能引用当前企业可追溯的原始网页信号")
        now = self._clock()
        evidence = ScreeningEvidence(
            tenant_id=tenant_id,
            account_id=ProspectAccountId(account_id),
            kind=kind,
            source_ref=signal.signal_id,
            source_url=signal.source_url,
            source_quote=signal.raw_observation,
            occurred_at=occurred_at,
            recorded_at=now,
            reviewed_by=reviewer,
            reviewed_at=now,
        )
        # 没有已分配档位时也调用同一领域校验，不允许先存未来日期再等待配置。
        try:
            evaluate_screening(
                "advanced", tenant_id, ProspectAccountId(account_id), (evidence,), now
            )
        except ValueError:
            raise ValidationError("筛选证据时间或归属无效") from None
        await _required_store(self._plan_store).append_evidence(
            tenant_id, ProspectAccountId(account_id), evidence
        )
        return await self.get(tenant_id, account_id)


class ServicePlanScheduleApplication:
    """复用已确认提案配置研究排程；暂停保留所有在途批次的原授权身份。"""

    def __init__(
        self,
        store: ProspectingServicePlanStore | None,
        directives: DirectiveService | None,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._plan_store = store
        self._directives = directives
        self._clock = clock

    async def configure_target(
        self, tenant_id: TenantId, *, employee_id: EmployeeId,
        user_id: UserId | None, body: ServicePlanScheduleBody,
        knowledge: EnterpriseKnowledgeService | None,
    ) -> ServicePlanState:
        """当前确认资料作为统一任务目标；暂停仍保留原操作者和目标。"""
        store = _required_store(self._plan_store)
        current = await store.get(tenant_id)
        query = body.query
        if not body.enabled and current is not None and current.target_kind == "inventory_request":
            query = current.query
        if query is None:
            return await self.configure(tenant_id, employee_id=employee_id, user_id=user_id,
                proposal_id=body.proposal_id, enabled=body.enabled, expected_version=body.expected_version)
        if not user_id:
            raise PermissionDenied("当前管理员缺少用户绑定")
        if body.enabled:
            if not query.confirmed_refs or knowledge is None:
                raise ValidationError("周期获客需要企业已确认产品资料")
            await ConfirmedContextReader(knowledge).read(tenant_id, employee_id, query.confirmed_refs)
        return await store.configure_inventory_schedule(tenant_id, query=query,
            employee_id=current.employee_id if not body.enabled and current and current.employee_id else str(employee_id),
            acting_user_id=current.acting_user_id if not body.enabled and current and current.acting_user_id else str(user_id),
            enabled=body.enabled, expected_version=body.expected_version, now=self._clock())

    async def configure(
        self,
        tenant_id: TenantId,
        *,
        employee_id: EmployeeId,
        user_id: UserId | None,
        proposal_id: str | None,
        enabled: bool,
        expected_version: int,
    ) -> ServicePlanState:
        """启用只绑定当前原确认人，暂停忽略浏览器替换的提案身份。"""
        actor: str | None = None
        if enabled:
            if not proposal_id or self._directives is None:
                raise ValidationError("周期探索需要已确认的研究提案")
            if user_id is None:
                raise ValidationError("当前员工缺少用户映射")
            actor = str(user_id)
            plan = await self._directives.get_confirmed_discovery_plan(
                tenant_id, proposal_id, employee_id
            )
            active = await self._directives.get_active(tenant_id)
            if (
                plan.execution_mode != "research_only"
                or active is None
                or active.source_proposal_id != proposal_id
            ):
                raise ValidationError("周期探索只允许当前已确认的研究提案")
        else:
            current = await _required_store(self._plan_store).get(tenant_id)
            if current is not None:
                proposal_id, actor = current.proposal_id, current.acting_user_id
        return await _required_store(self._plan_store).configure_schedule(
            tenant_id,
            proposal_id=proposal_id,
            acting_user_id=actor,
            enabled=enabled,
            expected_version=expected_version,
            now=self._clock(),
        )


class ScreeningDetailView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    assessment: ScreeningAssessment | None
    evidence: list[ScreeningEvidence]
    eligible_signals: list[DemandSignalView] = Field(default_factory=list)
