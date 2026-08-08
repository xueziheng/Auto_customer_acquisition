"""寻源域存储接口。

**内部实现，其他域不得导入。**
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from domains.sourcing.models import CaseState, SourcingCase, SupplierCandidate
from shared.schemas.identifiers import (
    SourcingCaseId,
    SupplierCandidateId,
    TenantId,
    ValidatedNeedId,
)


@runtime_checkable
class SourcingCaseRepository(Protocol):
    async def add(self, case: SourcingCase) -> None: ...

    async def get(
        self, tenant_id: TenantId, case_id: SourcingCaseId
    ) -> SourcingCase | None: ...

    async def update(self, case: SourcingCase) -> None: ...

    async def find_active_for_need(
        self, tenant_id: TenantId, need_id: ValidatedNeedId
    ) -> SourcingCase | None:
        """查该需求的活跃案例（幂等入口）。"""
        ...

    async def list_by_state(
        self, tenant_id: TenantId, state: CaseState, limit: int
    ) -> list[SourcingCase]: ...


@runtime_checkable
class CandidateRepository(Protocol):
    async def add(self, candidate: SupplierCandidate) -> None:
        """保存候选。**被拒的候选也要存**——它们是核验规则的校准
        数据，没有被拒样本就无法评估规则是否过严或过松。"""
        ...

    async def get(
        self, tenant_id: TenantId, candidate_id: SupplierCandidateId
    ) -> SupplierCandidate | None: ...

    async def update(self, candidate: SupplierCandidate) -> None: ...

    async def list_for_case(
        self, tenant_id: TenantId, case_id: SourcingCaseId, include_rejected: bool
    ) -> list[SupplierCandidate]: ...

    async def count_qualified(
        self, tenant_id: TenantId, case_id: SourcingCaseId
    ) -> int:
        """合格候选数，与 ``MAX_QUALIFIED_CANDIDATES`` 比较用。"""
        ...
