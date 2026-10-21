"""持久准备消费者的显式窄端口；原件及联系方式只在单次调用内存中传递。"""
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from domains.demand.service import DemandService
from domains.prospecting.agent_profile import (
    AgentPreparationObservation,
    AgentProfileEvidenceReader,
    AgentProfileSnapshot,
)
from domains.prospecting.inventory import CandidateAllocation
from domains.prospecting.inventory_followup import PreparationClaim
from domains.prospecting.service import ProspectingService
from shared.schemas.identifiers import CandidateAllocationId, EmployeeId, TenantId

PreparationAction = Callable[[ProspectingService, DemandService], Awaitable[dict[str, str]]]


class AgentPreparationTransactions(Protocol):
    async def apply(self, claim: PreparationClaim, action: PreparationAction) -> PreparationClaim: ...


@dataclass(frozen=True, repr=False)
class ExtractedPreparation:
    snapshot: AgentProfileSnapshot
    evidence: AgentProfileEvidenceReader
    observation: AgentPreparationObservation
    extracted_by: str
    extracted_at: datetime


class PreparationExtractor(Protocol):
    async def extract(self, claim: PreparationClaim, allocation: CandidateAllocation,
                      context: list[dict[str, object]], guard: Callable[[], Awaitable[None]],
                      *, target_entity_types: tuple[str, ...]) -> ExtractedPreparation:
        """经当前租户Gateway重新归档后调用既有模型；不复用公共池的私有artifact。"""
        ...


class PreparationQueue(Protocol):
    async def claim_preparation(self, tenant_id: TenantId, lease_duration: timedelta = timedelta(minutes=10)) -> PreparationClaim | None: ...
    async def require_preparation_claim(self, tenant_id: TenantId, claim: PreparationClaim) -> None: ...
    async def block_preparation(self, tenant_id: TenantId, claim: PreparationClaim, *, reason: str) -> None: ...
    async def checkpoint_preparation(self, tenant_id: TenantId, claim: PreparationClaim, *, stage: str, result_refs: dict[str, str] | None = None,
                                     external_started: bool = False, ownership_locked_at: datetime | None = None) -> PreparationClaim: ...
    async def assign_preparation(self, tenant_id: TenantId, claim: PreparationClaim) -> PreparationClaim: ...
    async def get_allocation(self, tenant_id: TenantId, allocation_id: CandidateAllocationId) -> CandidateAllocation | None: ...
    async def assign(self, tenant_id: TenantId, allocation_ids: tuple[CandidateAllocationId, ...], employee_id: EmployeeId, *, acting_employee_id: EmployeeId) -> tuple[CandidateAllocation, ...]: ...
