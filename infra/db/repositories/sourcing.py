"""Sourcing V2 的 tenant-bound SQLAlchemy 仓储。"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import cast

from pydantic import BaseModel
from sqlalchemy import (
    ColumnElement,
    CursorResult,
    Select,
    and_,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from domains.sourcing.errors import SourcingCaseConflictError, SourcingPlanStaleError
from domains.sourcing.models import (
    AdmissionBlockedReason,
    AdmissionState,
    CaseState,
    EvidenceSnapshot,
    LadderCheck,
    LadderOutcome,
    MatchExplanation,
    MatchLadderRung,
    PriceRejectionReason,
    PublicPlanStatus,
    PublicSourcingPlan,
    SourcingAdmission,
    SourcingCase,
    SourcingPrioritySnapshot,
    SourcingReconciliationStatus,
    SourcingReview,
    SourcingSearchExecution,
    SourcingSearchExecutionStatus,
    SourcingSearchReconciliation,
    SourcingStopCode,
    SourcingStopDetail,
    SourcingStopStage,
    SourcingSupplyOption,
    SpecComparison,
    SpecMatchLevel,
    SupplierCandidate,
    SupplyOptionSource,
)
from domains.sourcing.schemas import (
    IndicativePriceTier,
    PublicCandidateDraft,
    PublicCandidateDraftPriceTier,
    PublicCandidateDraftSpec,
    PublicSourcingPlanCommand,
    PublicSourcingQuery,
    SourcingCostPriceOption,
    SourcingHandoffSnapshot,
    SourcingMatchInference,
    SourcingNeedSnapshot,
    SourcingObservedFact,
    SourcingSupplierClaim,
)
from infra.db.base import TenantScopedRepository
from infra.db.tables import (
    ProductCandidatePriceRefRow,
    ProductCandidateSourceRow,
    ProductRow,
    SourcingAdmissionRow,
    SourcingCandidateDraftRow,
    SourcingCandidateEvidenceRow,
    SourcingCandidateRow,
    SourcingCaseRow,
    SourcingLadderCheckRow,
    SourcingPrioritySnapshotRow,
    SourcingPublicPlanRow,
    SourcingReviewRow,
    SourcingSearchExecutionRow,
    SourcingSearchReconciliationRow,
    SourcingSupplyOptionRow,
)
from shared.errors import ValidationError
from shared.schemas.identifiers import (
    ArtifactId,
    EmployeeId,
    NeedClusterId,
    OpportunityId,
    ProductId,
    RunId,
    SourcingAdmissionId,
    SourcingCaseId,
    SourcingPlanId,
    SourcingPrioritySnapshotId,
    SourcingReviewId,
    SourcingSupplyOptionId,
    SupplierCandidateId,
    TenantId,
    ValidatedNeedId,
)
from shared.schemas.provenance import ProvenanceSummary


class _TenantBoundRepository(TenantScopedRepository):
    """构造绑定 tenant，且每个公共入口再次核对显式 tenant。"""

    def __init__(self, session: AsyncSession, tenant_id: TenantId) -> None:
        super().__init__(tenant_id)
        self._session = session

    def _require_tenant(self, tenant_id: TenantId) -> None:
        if tenant_id != self._tenant_id:
            raise ValueError("请求租户与仓储绑定租户不一致")


def _require_utc_datetime(value: object, field_name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
    ):
        raise ValidationError(f"{field_name} 必须是 UTC 时间")
    return value


def _require_bounded_identifier(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 200
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValidationError(f"{field_name} 无效")
    return value


def _require_limit(value: object, *, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValidationError(f"limit 必须是 1..{maximum} 的整数")
    return value


def _comparison_to_json(value: SpecComparison) -> dict[str, object]:
    return {
        "spec_name": value.spec_name,
        "required": value.required,
        "offered": value.offered,
        "level": value.level.value,
        "substitutable": value.substitutable,
        "substitution_impact": value.substitution_impact,
        "needs_customer_confirmation": value.needs_customer_confirmation,
        "customer_confirmation": (
            value.customer_confirmation.model_dump(mode="json")
            if value.customer_confirmation is not None
            else None
        ),
        "product_id": str(value.product_id) if value.product_id is not None else None,
        "evidence_ref": (
            str(value.evidence_ref) if value.evidence_ref is not None else None
        ),
    }


def _comparison_from_json(value: dict[str, object]) -> SpecComparison:
    return SpecComparison(
        spec_name=str(value["spec_name"]),
        required=str(value["required"]),
        offered=cast(str | None, value.get("offered")),
        level=SpecMatchLevel(str(value["level"])),
        substitutable=cast(bool | None, value.get("substitutable")),
        substitution_impact=cast(str | None, value.get("substitution_impact")),
        needs_customer_confirmation=bool(
            value.get("needs_customer_confirmation", False)
        ),
        customer_confirmation=(
            ProvenanceSummary.model_validate(value["customer_confirmation"])
            if value.get("customer_confirmation") is not None
            else None
        ),
        product_id=(
            ProductId(str(value["product_id"])) if value.get("product_id") else None
        ),
        evidence_ref=(
            ArtifactId(str(value["evidence_ref"]))
            if value.get("evidence_ref")
            else None
        ),
    )


def _stop_detail_to_json(value: SourcingStopDetail | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "stage": value.stage.value,
        "query_index": value.query_index,
        "provider_http_status": value.provider_http_status,
        "observed_count": value.observed_count,
        "configured_limit": value.configured_limit,
    }


def _stop_detail_from_json(
    value: dict[str, object] | None,
) -> SourcingStopDetail | None:
    if value is None:
        return None
    return SourcingStopDetail(
        stage=SourcingStopStage(str(value["stage"])),
        query_index=cast(int | None, value.get("query_index")),
        provider_http_status=cast(int | None, value.get("provider_http_status")),
        observed_count=cast(int | None, value.get("observed_count")),
        configured_limit=cast(int | None, value.get("configured_limit")),
    )


def _case_to_row(case: SourcingCase) -> SourcingCaseRow:
    if case.need_snapshot is None:
        raise ValidationError("V2 SourcingCase 必须携带不可变需求快照")
    if case.trigger_key is None or case.need_snapshot_hash is None:
        raise ValidationError("V2 SourcingCase 缺少 trigger_key 或 need_snapshot_hash")
    if case.need_snapshot_hash != case.need_snapshot.snapshot_hash:
        raise ValidationError("案例快照哈希与需求快照正文不一致")
    return SourcingCaseRow(
        tenant_id=case.tenant_id,
        case_id=case.case_id,
        need_id=case.need_id,
        opportunity_id=case.opportunity_id,
        workflow_version=case.workflow_version,
        trigger_key=case.trigger_key,
        need_snapshot=case.need_snapshot.model_dump(mode="json"),
        need_snapshot_hash=case.need_snapshot_hash,
        state=case.state.value,
        ladder_checked_to=(
            case.ladder_checked_to.value if case.ladder_checked_to is not None else None
        ),
        active_search_plan_id=case.active_search_plan_id,
        sealed_candidate_ids=list(case.sealed_candidate_ids),
        candidate_set_hash=case.candidate_set_hash,
        candidates_verified_at=case.candidates_verified_at,
        stop_code=case.stop_code.value if case.stop_code is not None else None,
        stop_detail=_stop_detail_to_json(case.stop_detail),
        assigned_to=case.assigned_to,
        version=case.version,
        opened_at=case.opened_at,
        state_changed_at=case.state_changed_at or case.opened_at,
        completed_at=case.completed_at,
        failed_reason=case.failed_reason,
    )


def _row_to_case(row: SourcingCaseRow) -> SourcingCase:
    return SourcingCase(
        case_id=SourcingCaseId(row.case_id),
        tenant_id=TenantId(row.tenant_id),
        need_id=ValidatedNeedId(row.need_id),
        opened_at=row.opened_at,
        opportunity_id=(
            OpportunityId(row.opportunity_id) if row.opportunity_id else None
        ),
        state=CaseState(row.state),
        ladder_checked_to=(
            MatchLadderRung(row.ladder_checked_to)
            if row.ladder_checked_to is not None
            else None
        ),
        completed_at=row.completed_at,
        failed_reason=row.failed_reason,
        assigned_to=EmployeeId(row.assigned_to) if row.assigned_to else None,
        workflow_version=row.workflow_version,
        trigger_key=row.trigger_key,
        need_snapshot=SourcingNeedSnapshot.model_validate_json(
            json.dumps(row.need_snapshot)
        ),
        need_snapshot_hash=row.need_snapshot_hash,
        active_search_plan_id=(
            SourcingPlanId(row.active_search_plan_id)
            if row.active_search_plan_id is not None
            else None
        ),
        sealed_candidate_ids=tuple(
            SupplierCandidateId(item) for item in row.sealed_candidate_ids
        ),
        candidate_set_hash=row.candidate_set_hash,
        candidates_verified_at=row.candidates_verified_at,
        stop_code=SourcingStopCode(row.stop_code) if row.stop_code else None,
        stop_detail=_stop_detail_from_json(row.stop_detail),
        version=row.version,
        state_changed_at=row.state_changed_at,
    )


class SourcingCaseRepositoryImpl(_TenantBoundRepository):
    """案例 CRUD；更新以实体上一版本做 CAS。"""

    def _scoped(self) -> Select[tuple[SourcingCaseRow]]:
        return self.scoped_query(SourcingCaseRow)

    async def get_or_create(
        self, tenant_id: TenantId, case: SourcingCase
    ) -> tuple[SourcingCase, bool]:
        self._require_tenant(tenant_id)
        if case.tenant_id != tenant_id:
            raise ValueError("案例租户与请求租户不一致")
        row = _case_to_row(case)
        values = {
            column.name: getattr(row, column.name)
            for column in SourcingCaseRow.__table__.columns
        }
        inserted_id = (
            await self._session.execute(
                pg_insert(SourcingCaseRow)
                .values(**values)
                .on_conflict_do_nothing(constraint="uq_sourcing_cases_trigger")
                .returning(SourcingCaseRow.case_id)
            )
        ).scalar_one_or_none()
        if inserted_id is not None:
            return case, True
        canonical = (
            await self._session.execute(
                self._scoped().where(SourcingCaseRow.trigger_key == case.trigger_key)
            )
        ).scalar_one()
        return _row_to_case(canonical), False

    async def add(self, tenant_id: TenantId, case: SourcingCase) -> None:
        self._require_tenant(tenant_id)
        if case.tenant_id != tenant_id:
            raise ValueError("案例租户与请求租户不一致")
        self._session.add(_case_to_row(case))
        await self._session.flush()

    async def get(
        self, tenant_id: TenantId, case_id: SourcingCaseId
    ) -> SourcingCase | None:
        self._require_tenant(tenant_id)
        row = (
            await self._session.execute(
                self._scoped().where(SourcingCaseRow.case_id == case_id)
            )
        ).scalar_one_or_none()
        return _row_to_case(row) if row is not None else None

    async def get_for_update(
        self, tenant_id: TenantId, case_id: SourcingCaseId
    ) -> SourcingCase | None:
        self._require_tenant(tenant_id)
        row = (
            await self._session.execute(
                self.scoped_query(SourcingCaseRow)
                .where(SourcingCaseRow.case_id == case_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        return _row_to_case(row) if row is not None else None

    async def update(
        self,
        tenant_id: TenantId,
        case: SourcingCase,
        *,
        clear_recoverable_stop: bool = False,
    ) -> None:
        self._require_tenant(tenant_id)
        if case.tenant_id != tenant_id:
            raise ValueError("案例租户与请求租户不一致")
        if case.version <= 1:
            raise SourcingCaseConflictError("案例更新缺少可比较的上一版本")
        row = _case_to_row(case)
        values = {
            column.name: getattr(row, column.name)
            for column in SourcingCaseRow.__table__.columns
            if column.name
            not in {
                "tenant_id",
                "case_id",
                "need_id",
                "workflow_version",
                "trigger_key",
                "need_snapshot",
                "need_snapshot_hash",
                "opened_at",
            }
        }
        if case.stop_code is None and not clear_recoverable_stop:
            # stop 是一个不改变业务 revision 的安全 Workflow 投影。先前读取的
            # 普通业务写不能将它擦除；只有完成该恢复动作的调用方可显式清除。
            values["stop_code"] = SourcingCaseRow.stop_code
            values["stop_detail"] = SourcingCaseRow.stop_detail
        result = await self._session.execute(
            update(SourcingCaseRow)
            .where(
                SourcingCaseRow.tenant_id == tenant_id,
                SourcingCaseRow.case_id == case.case_id,
                SourcingCaseRow.version == case.version - 1,
            )
            .values(**values)
        )
        if cast(CursorResult[object], result).rowcount != 1:
            raise SourcingCaseConflictError("案例版本已变化，拒绝过期覆盖")

    async def set_recoverable_stop(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        *,
        expected_version: int,
        stop_code: SourcingStopCode,
        stop_detail: SourcingStopDetail,
    ) -> None:
        """写入不会改变审核选择事实的等待投影，仍以版本和空 stop 条件拒绝覆盖。"""

        self._require_tenant(tenant_id)
        if expected_version < 1:
            raise SourcingCaseConflictError("等待停止缺少可比较的案例版本")
        result = await self._session.execute(
            update(SourcingCaseRow)
            .where(
                SourcingCaseRow.tenant_id == tenant_id,
                SourcingCaseRow.case_id == case_id,
                SourcingCaseRow.version == expected_version,
                SourcingCaseRow.stop_code.is_(None),
            )
            .values(
                stop_code=stop_code.value,
                stop_detail=_stop_detail_to_json(stop_detail),
            )
        )
        if cast(CursorResult[object], result).rowcount != 1:
            raise SourcingCaseConflictError("案例等待停止已变化，拒绝覆盖")

    async def find_active_for_need(
        self, tenant_id: TenantId, need_id: ValidatedNeedId
    ) -> SourcingCase | None:
        self._require_tenant(tenant_id)
        row = (
            await self._session.execute(
                self._scoped().where(
                    SourcingCaseRow.need_id == need_id,
                    SourcingCaseRow.state.in_(
                        ("opened", "discovering", "verifying", "candidates_ready")
                    ),
                )
            )
        ).scalar_one_or_none()
        return _row_to_case(row) if row is not None else None

    async def get_by_trigger(
        self, tenant_id: TenantId, trigger_key: str
    ) -> SourcingCase | None:
        self._require_tenant(tenant_id)
        row = (
            await self._session.execute(
                self._scoped().where(SourcingCaseRow.trigger_key == trigger_key)
            )
        ).scalar_one_or_none()
        return _row_to_case(row) if row is not None else None

    async def list_by_state(
        self, tenant_id: TenantId, state: CaseState, limit: int
    ) -> list[SourcingCase]:
        self._require_tenant(tenant_id)
        rows = (
            await self._session.execute(
                self._scoped()
                .where(SourcingCaseRow.state == state.value)
                .order_by(SourcingCaseRow.opened_at, SourcingCaseRow.case_id)
                .limit(max(0, min(limit, 50)))
            )
        ).scalars()
        return [_row_to_case(row) for row in rows]


def _admission_values(value: SourcingAdmission) -> dict[str, object]:
    return {
        "tenant_id": str(value.tenant_id),
        "admission_id": str(value.admission_id),
        "case_id": str(value.case_id),
        "need_id": str(value.need_id),
        "state": value.state.value,
        "ready_at": value.ready_at,
        "current_snapshot_id": (
            str(value.current_snapshot_id)
            if value.current_snapshot_id is not None
            else None
        ),
        "claim_token": value.claim_token,
        "claim_expires_at": value.claim_expires_at,
        "workflow_run_id": (
            str(value.workflow_run_id) if value.workflow_run_id is not None else None
        ),
        "blocked_reason": (
            value.blocked_reason.value if value.blocked_reason is not None else None
        ),
        "admitted_at": value.admitted_at,
        "admitted_by": value.admitted_by,
        "created_at": value.created_at,
        "updated_at": value.updated_at,
    }


def _row_to_admission(row: SourcingAdmissionRow) -> SourcingAdmission:
    return SourcingAdmission(
        tenant_id=TenantId(row.tenant_id),
        admission_id=SourcingAdmissionId(row.admission_id),
        case_id=SourcingCaseId(row.case_id),
        need_id=ValidatedNeedId(row.need_id),
        state=AdmissionState(row.state),
        ready_at=row.ready_at,
        current_snapshot_id=(
            SourcingPrioritySnapshotId(row.current_snapshot_id)
            if row.current_snapshot_id is not None
            else None
        ),
        claim_token=row.claim_token,
        claim_expires_at=row.claim_expires_at,
        workflow_run_id=(
            RunId(row.workflow_run_id) if row.workflow_run_id is not None else None
        ),
        blocked_reason=(
            AdmissionBlockedReason(row.blocked_reason)
            if row.blocked_reason is not None
            else None
        ),
        admitted_at=row.admitted_at,
        admitted_by=row.admitted_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _snapshot_values(value: SourcingPrioritySnapshot) -> dict[str, object]:
    return {
        "tenant_id": str(value.tenant_id),
        "snapshot_id": str(value.snapshot_id),
        "admission_id": str(value.admission_id),
        "case_id": str(value.case_id),
        "need_id": str(value.need_id),
        "cluster_id": str(value.cluster_id) if value.cluster_id is not None else None,
        "cluster_member_count": value.cluster_member_count,
        "ready_at": value.ready_at,
        "ranking_version": value.ranking_version,
        "facts_observed_at": value.facts_observed_at,
        "facts_hash": value.facts_hash,
        "created_at": value.created_at,
    }


def _row_to_snapshot(row: SourcingPrioritySnapshotRow) -> SourcingPrioritySnapshot:
    return SourcingPrioritySnapshot(
        tenant_id=TenantId(row.tenant_id),
        snapshot_id=SourcingPrioritySnapshotId(row.snapshot_id),
        admission_id=SourcingAdmissionId(row.admission_id),
        case_id=SourcingCaseId(row.case_id),
        need_id=ValidatedNeedId(row.need_id),
        cluster_id=(
            NeedClusterId(row.cluster_id) if row.cluster_id is not None else None
        ),
        cluster_member_count=row.cluster_member_count,
        ready_at=row.ready_at,
        ranking_version=row.ranking_version,
        facts_observed_at=row.facts_observed_at,
        facts_hash=row.facts_hash,
        created_at=row.created_at,
    )


class SourcingAdmissionRepositoryImpl(_TenantBoundRepository):
    """PostgreSQL 准入队列；排序、锁与租户边界都在 SQL 中完成。"""

    def _admissions(self) -> Select[tuple[SourcingAdmissionRow]]:
        return self.scoped_query(SourcingAdmissionRow)

    def _snapshots(self) -> Select[tuple[SourcingPrioritySnapshotRow]]:
        return self.scoped_query(SourcingPrioritySnapshotRow)

    def _admissions_with_current_snapshot(
        self,
    ) -> Select[tuple[SourcingAdmissionRow, SourcingPrioritySnapshotRow]]:
        """以 tenant+current pointer 连接安全读取投影，保留无首快照阻断项。"""

        return (
            select(SourcingAdmissionRow, SourcingPrioritySnapshotRow)
            .select_from(SourcingAdmissionRow)
            .outerjoin(
                SourcingPrioritySnapshotRow,
                and_(
                    SourcingPrioritySnapshotRow.tenant_id
                    == SourcingAdmissionRow.tenant_id,
                    SourcingPrioritySnapshotRow.admission_id
                    == SourcingAdmissionRow.admission_id,
                    SourcingPrioritySnapshotRow.snapshot_id
                    == SourcingAdmissionRow.current_snapshot_id,
                ),
            )
            .where(SourcingAdmissionRow.tenant_id == str(self._tenant_id))
        )

    @staticmethod
    def _read_pair(
        admission_row: SourcingAdmissionRow,
        snapshot_row: SourcingPrioritySnapshotRow | None,
    ) -> tuple[SourcingAdmission, SourcingPrioritySnapshot | None]:
        admission = _row_to_admission(admission_row)
        if admission.current_snapshot_id is not None and snapshot_row is None:
            raise ValidationError("准入 current snapshot 不存在或不属于当前租户")
        snapshot = (
            _row_to_snapshot(snapshot_row) if snapshot_row is not None else None
        )
        return admission, snapshot

    @staticmethod
    def _require_initial_pair(
        tenant_id: TenantId,
        admission: SourcingAdmission,
        snapshot: SourcingPrioritySnapshot | None,
    ) -> None:
        if admission.tenant_id != tenant_id:
            raise ValueError("准入租户与请求租户不一致")
        for field_name in ("ready_at", "created_at", "updated_at"):
            _require_utc_datetime(getattr(admission, field_name), field_name)
        if snapshot is None:
            if not (
                admission.state is AdmissionState.BLOCKED
                and admission.blocked_reason
                is AdmissionBlockedReason.PRIORITY_FACTS_INVALID
                and admission.current_snapshot_id is None
            ):
                raise ValidationError(
                    "无首快照仅允许 priority_facts_invalid blocked 准入"
                )
            return
        for field_name in ("ready_at", "facts_observed_at", "created_at"):
            _require_utc_datetime(getattr(snapshot, field_name), field_name)
        if (
            snapshot.tenant_id != tenant_id
            or snapshot.admission_id != admission.admission_id
            or snapshot.case_id != admission.case_id
            or snapshot.need_id != admission.need_id
            or snapshot.ready_at != admission.ready_at
            or admission.current_snapshot_id != snapshot.snapshot_id
        ):
            raise ValidationError("首快照与准入记录不一致")

    async def _current_snapshot(
        self, admission: SourcingAdmissionRow
    ) -> SourcingPrioritySnapshot | None:
        if admission.current_snapshot_id is None:
            return None
        row = (
            await self._session.execute(
                self._snapshots().where(
                    SourcingPrioritySnapshotRow.admission_id == admission.admission_id,
                    SourcingPrioritySnapshotRow.snapshot_id
                    == admission.current_snapshot_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise ValidationError("准入 current snapshot 不存在或不属于当前租户")
        return _row_to_snapshot(row)

    async def get_or_create(
        self,
        tenant_id: TenantId,
        admission: SourcingAdmission,
        initial_snapshot: SourcingPrioritySnapshot | None,
    ) -> tuple[SourcingAdmission, SourcingPrioritySnapshot | None, bool]:
        self._require_tenant(tenant_id)
        self._require_initial_pair(tenant_id, admission, initial_snapshot)
        inserted_id = (
            await self._session.execute(
                pg_insert(SourcingAdmissionRow)
                .values(**_admission_values(admission))
                .on_conflict_do_nothing()
                .returning(SourcingAdmissionRow.admission_id)
            )
        ).scalar_one_or_none()
        if inserted_id is not None:
            if initial_snapshot is not None:
                inserted_snapshot_id = (
                    await self._session.execute(
                        pg_insert(SourcingPrioritySnapshotRow)
                        .values(**_snapshot_values(initial_snapshot))
                        .on_conflict_do_nothing()
                        .returning(SourcingPrioritySnapshotRow.snapshot_id)
                    )
                ).scalar_one_or_none()
                if inserted_snapshot_id is None:
                    raise ValidationError("首快照标识或事实哈希已被其他记录占用")
            return admission, initial_snapshot, True

        rows = (
            (
                await self._session.execute(
                    self._admissions()
                    .where(
                        or_(
                            SourcingAdmissionRow.admission_id
                            == str(admission.admission_id),
                            SourcingAdmissionRow.case_id == str(admission.case_id),
                            SourcingAdmissionRow.need_id == str(admission.need_id),
                        )
                    )
                    .limit(2)
                )
            )
            .scalars()
            .all()
        )
        if len(rows) != 1:
            raise ValidationError("准入唯一键与既有 canonical 记录冲突")
        canonical = rows[0]
        if canonical.case_id != str(admission.case_id) or canonical.need_id != str(
            admission.need_id
        ):
            raise ValidationError("准入标识与既有 Case/Need canonical 记录冲突")
        return (
            _row_to_admission(canonical),
            await self._current_snapshot(canonical),
            False,
        )

    async def get(
        self, tenant_id: TenantId, admission_id: SourcingAdmissionId
    ) -> SourcingAdmission | None:
        self._require_tenant(tenant_id)
        row = (
            await self._session.execute(
                self._admissions().where(
                    SourcingAdmissionRow.admission_id == str(admission_id)
                )
            )
        ).scalar_one_or_none()
        return _row_to_admission(row) if row is not None else None

    async def get_with_current_snapshot(
        self, tenant_id: TenantId, admission_id: SourcingAdmissionId
    ) -> tuple[SourcingAdmission, SourcingPrioritySnapshot | None] | None:
        self._require_tenant(tenant_id)
        row = (
            await self._session.execute(
                self._admissions_with_current_snapshot().where(
                    SourcingAdmissionRow.admission_id == str(admission_id)
                )
            )
        ).one_or_none()
        if row is None:
            return None
        return self._read_pair(row[0], row[1])

    async def append_snapshot_if_changed(
        self, tenant_id: TenantId, snapshot: SourcingPrioritySnapshot
    ) -> tuple[SourcingAdmission, SourcingPrioritySnapshot, bool]:
        self._require_tenant(tenant_id)
        if snapshot.tenant_id != tenant_id:
            raise ValueError("快照租户与请求租户不一致")
        for field_name in ("ready_at", "facts_observed_at", "created_at"):
            _require_utc_datetime(getattr(snapshot, field_name), field_name)
        row = (
            await self._session.execute(
                self._admissions()
                .where(SourcingAdmissionRow.admission_id == str(snapshot.admission_id))
                .with_for_update(of=SourcingAdmissionRow)
            )
        ).scalar_one_or_none()
        if row is None:
            raise ValidationError("准入记录不存在")
        current = _row_to_admission(row)
        if (
            current.case_id != snapshot.case_id
            or current.need_id != snapshot.need_id
            or current.ready_at != snapshot.ready_at
        ):
            raise ValidationError("优先级快照与准入记录不一致")

        inserted_id = (
            await self._session.execute(
                pg_insert(SourcingPrioritySnapshotRow)
                .values(**_snapshot_values(snapshot))
                .on_conflict_do_nothing()
                .returning(SourcingPrioritySnapshotRow.snapshot_id)
            )
        ).scalar_one_or_none()
        created = inserted_id is not None
        if created:
            canonical = snapshot
        else:
            canonical_row = (
                await self._session.execute(
                    self._snapshots().where(
                        SourcingPrioritySnapshotRow.admission_id
                        == str(snapshot.admission_id),
                        SourcingPrioritySnapshotRow.facts_hash == snapshot.facts_hash,
                    )
                )
            ).scalar_one_or_none()
            if canonical_row is None:
                raise ValidationError("快照标识已被其他事实占用")
            canonical = _row_to_snapshot(canonical_row)
            if current.current_snapshot_id == canonical.snapshot_id and not (
                current.state is AdmissionState.BLOCKED
                and current.blocked_reason
                is AdmissionBlockedReason.PRIORITY_FACTS_INVALID
            ):
                return current, canonical, False

        changed = current.with_current_snapshot(snapshot)
        if canonical.snapshot_id != snapshot.snapshot_id:
            changed = replace(
                changed,
                current_snapshot_id=canonical.snapshot_id,
            )
        updated_row = (
            await self._session.execute(
                update(SourcingAdmissionRow)
                .where(
                    SourcingAdmissionRow.tenant_id == str(tenant_id),
                    SourcingAdmissionRow.admission_id == str(snapshot.admission_id),
                    SourcingAdmissionRow.state == current.state.value,
                    SourcingAdmissionRow.updated_at == current.updated_at,
                    SourcingAdmissionRow.updated_at <= snapshot.created_at,
                )
                .values(
                    state=changed.state.value,
                    current_snapshot_id=str(changed.current_snapshot_id),
                    blocked_reason=(
                        changed.blocked_reason.value
                        if changed.blocked_reason is not None
                        else None
                    ),
                    updated_at=changed.updated_at,
                )
                .returning(SourcingAdmissionRow)
            )
        ).scalar_one_or_none()
        if updated_row is None:
            raise ValidationError("准入 current snapshot 并发推进失败")
        return _row_to_admission(updated_row), canonical, created

    async def list_cluster_refresh_targets(
        self,
        tenant_id: TenantId,
        cluster_id: NeedClusterId,
        changed_need_id: ValidatedNeedId,
    ) -> list[tuple[SourcingAdmission, SourcingPrioritySnapshot | None]]:
        self._require_tenant(tenant_id)
        _require_bounded_identifier(cluster_id, "cluster_id")
        _require_bounded_identifier(changed_need_id, "changed_need_id")
        rows = (
            await self._session.execute(
                self._admissions_with_current_snapshot()
                .where(
                    SourcingAdmissionRow.tenant_id == str(tenant_id),
                    SourcingAdmissionRow.state.in_(
                        (AdmissionState.WAITING.value, AdmissionState.BLOCKED.value)
                    ),
                    or_(
                        SourcingPrioritySnapshotRow.cluster_id == str(cluster_id),
                        SourcingAdmissionRow.need_id == str(changed_need_id),
                    ),
                )
                .order_by(SourcingAdmissionRow.admission_id)
            )
        ).all()
        return [self._read_pair(row[0], row[1]) for row in rows]

    @staticmethod
    def _claimable(now: datetime) -> ColumnElement[bool]:
        return or_(
            SourcingAdmissionRow.state == AdmissionState.WAITING.value,
            and_(
                SourcingAdmissionRow.state == AdmissionState.STARTING.value,
                SourcingAdmissionRow.claim_expires_at <= now,
            ),
        )

    async def claim_ordered(
        self,
        tenant_id: TenantId,
        limit: int,
        claim_token: str,
        claim_expires_at: datetime,
        now: datetime,
    ) -> list[SourcingAdmission]:
        self._require_tenant(tenant_id)
        limit = _require_limit(limit, maximum=50)
        claim_token = _require_bounded_identifier(claim_token, "claim_token")
        claim_expires_at = _require_utc_datetime(
            claim_expires_at, "claim_expires_at"
        )
        now = _require_utc_datetime(now, "now")
        if claim_expires_at <= now:
            raise ValidationError("claim_expires_at 必须晚于 now")
        candidates = (
            (
                await self._session.execute(
                    self._admissions()
                    .join(
                        SourcingPrioritySnapshotRow,
                        and_(
                            SourcingPrioritySnapshotRow.tenant_id
                            == SourcingAdmissionRow.tenant_id,
                            SourcingPrioritySnapshotRow.admission_id
                            == SourcingAdmissionRow.admission_id,
                            SourcingPrioritySnapshotRow.snapshot_id
                            == SourcingAdmissionRow.current_snapshot_id,
                        ),
                    )
                    .where(
                        self._claimable(now),
                        SourcingAdmissionRow.updated_at <= now,
                    )
                    .order_by(
                        SourcingPrioritySnapshotRow.cluster_member_count.desc(),
                        SourcingPrioritySnapshotRow.ready_at,
                        SourcingPrioritySnapshotRow.need_id,
                        SourcingPrioritySnapshotRow.snapshot_id,
                    )
                    .limit(limit)
                    .with_for_update(of=SourcingAdmissionRow, skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        claimed: list[SourcingAdmission] = []
        for candidate in candidates:
            changed = (
                await self._session.execute(
                    update(SourcingAdmissionRow)
                    .where(
                        SourcingAdmissionRow.tenant_id == str(tenant_id),
                        SourcingAdmissionRow.admission_id == candidate.admission_id,
                        SourcingAdmissionRow.current_snapshot_id
                        == candidate.current_snapshot_id,
                        self._claimable(now),
                        SourcingAdmissionRow.updated_at <= now,
                    )
                    .values(
                        state=AdmissionState.STARTING.value,
                        claim_token=claim_token,
                        claim_expires_at=claim_expires_at,
                        updated_at=now,
                    )
                    .returning(SourcingAdmissionRow)
                )
            ).scalar_one_or_none()
            if changed is not None:
                claimed.append(_row_to_admission(changed))
        return claimed

    async def complete(
        self,
        tenant_id: TenantId,
        admission_id: SourcingAdmissionId,
        claim_token: str,
        workflow_run_id: RunId,
        admitted_by: str,
        admitted_at: datetime,
    ) -> SourcingAdmission | None:
        self._require_tenant(tenant_id)
        claim_token = _require_bounded_identifier(claim_token, "claim_token")
        _require_bounded_identifier(workflow_run_id, "workflow_run_id")
        admitted_by = _require_bounded_identifier(admitted_by, "admitted_by")
        admitted_at = _require_utc_datetime(admitted_at, "admitted_at")
        row = (
            await self._session.execute(
                update(SourcingAdmissionRow)
                .where(
                    SourcingAdmissionRow.tenant_id == str(tenant_id),
                    SourcingAdmissionRow.admission_id == str(admission_id),
                    SourcingAdmissionRow.state == AdmissionState.STARTING.value,
                    SourcingAdmissionRow.claim_token == claim_token,
                    SourcingAdmissionRow.updated_at <= admitted_at,
                )
                .values(
                    state=AdmissionState.ADMITTED.value,
                    claim_token=None,
                    claim_expires_at=None,
                    workflow_run_id=str(workflow_run_id),
                    admitted_by=admitted_by,
                    admitted_at=admitted_at,
                    updated_at=admitted_at,
                )
                .returning(SourcingAdmissionRow)
            )
        ).scalar_one_or_none()
        return _row_to_admission(row) if row is not None else None

    async def release_expired_claims(
        self, tenant_id: TenantId, now: datetime
    ) -> list[SourcingAdmission]:
        self._require_tenant(tenant_id)
        now = _require_utc_datetime(now, "now")
        rows = (
            (
                await self._session.execute(
                    self._admissions()
                    .where(
                        SourcingAdmissionRow.state == AdmissionState.STARTING.value,
                        SourcingAdmissionRow.claim_expires_at <= now,
                        SourcingAdmissionRow.updated_at <= now,
                    )
                    .order_by(
                        SourcingAdmissionRow.claim_expires_at,
                        SourcingAdmissionRow.admission_id,
                    )
                    .with_for_update(of=SourcingAdmissionRow, skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        released: list[SourcingAdmission] = []
        for candidate in rows:
            changed = (
                await self._session.execute(
                    update(SourcingAdmissionRow)
                    .where(
                        SourcingAdmissionRow.tenant_id == str(tenant_id),
                        SourcingAdmissionRow.admission_id == candidate.admission_id,
                        SourcingAdmissionRow.state == AdmissionState.STARTING.value,
                        SourcingAdmissionRow.claim_token == candidate.claim_token,
                        SourcingAdmissionRow.claim_expires_at <= now,
                        SourcingAdmissionRow.updated_at <= now,
                    )
                    .values(
                        state=AdmissionState.WAITING.value,
                        claim_token=None,
                        claim_expires_at=None,
                        updated_at=now,
                    )
                    .returning(SourcingAdmissionRow)
                )
            ).scalar_one_or_none()
            if changed is not None:
                released.append(_row_to_admission(changed))
        return released

    async def release(
        self,
        tenant_id: TenantId,
        admission_id: SourcingAdmissionId,
        claim_token: str,
        released_at: datetime,
    ) -> SourcingAdmission | None:
        self._require_tenant(tenant_id)
        claim_token = _require_bounded_identifier(claim_token, "claim_token")
        released_at = _require_utc_datetime(released_at, "released_at")
        row = (
            await self._session.execute(
                update(SourcingAdmissionRow)
                .where(
                    SourcingAdmissionRow.tenant_id == str(tenant_id),
                    SourcingAdmissionRow.admission_id == str(admission_id),
                    SourcingAdmissionRow.state == AdmissionState.STARTING.value,
                    SourcingAdmissionRow.claim_token == claim_token,
                    SourcingAdmissionRow.updated_at <= released_at,
                )
                .values(
                    state=AdmissionState.WAITING.value,
                    claim_token=None,
                    claim_expires_at=None,
                    updated_at=released_at,
                )
                .returning(SourcingAdmissionRow)
            )
        ).scalar_one_or_none()
        return _row_to_admission(row) if row is not None else None

    async def block(
        self,
        tenant_id: TenantId,
        admission_id: SourcingAdmissionId,
        reason: AdmissionBlockedReason,
        blocked_at: datetime,
        claim_token: str | None = None,
    ) -> SourcingAdmission | None:
        self._require_tenant(tenant_id)
        if not isinstance(reason, AdmissionBlockedReason):
            raise ValidationError("blocked_reason 必须是 AdmissionBlockedReason")
        blocked_at = _require_utc_datetime(blocked_at, "blocked_at")
        if claim_token is not None:
            claim_token = _require_bounded_identifier(claim_token, "claim_token")
        eligibility = (
            SourcingAdmissionRow.state == AdmissionState.WAITING.value
            if claim_token is None
            else and_(
                SourcingAdmissionRow.state == AdmissionState.STARTING.value,
                SourcingAdmissionRow.claim_token == claim_token,
            )
        )
        row = (
            await self._session.execute(
                update(SourcingAdmissionRow)
                .where(
                    SourcingAdmissionRow.tenant_id == str(tenant_id),
                    SourcingAdmissionRow.admission_id == str(admission_id),
                    eligibility,
                    SourcingAdmissionRow.updated_at <= blocked_at,
                )
                .values(
                    state=AdmissionState.BLOCKED.value,
                    claim_token=None,
                    claim_expires_at=None,
                    blocked_reason=reason.value,
                    updated_at=blocked_at,
                )
                .returning(SourcingAdmissionRow)
            )
        ).scalar_one_or_none()
        return _row_to_admission(row) if row is not None else None

    async def list_by_state(
        self, tenant_id: TenantId, state: AdmissionState, limit: int
    ) -> list[SourcingAdmission]:
        return [
            admission
            for admission, _ in await self.list_by_state_with_current_snapshot(
                tenant_id, state, limit
            )
        ]

    async def list_by_state_with_current_snapshot(
        self, tenant_id: TenantId, state: AdmissionState, limit: int
    ) -> list[tuple[SourcingAdmission, SourcingPrioritySnapshot | None]]:
        self._require_tenant(tenant_id)
        if not isinstance(state, AdmissionState):
            raise ValidationError("admission state 无效")
        limit = _require_limit(limit, maximum=200)
        rows = (
            (
                await self._session.execute(
                    self._admissions_with_current_snapshot()
                    .where(SourcingAdmissionRow.state == state.value)
                    .order_by(
                        SourcingPrioritySnapshotRow.cluster_member_count.desc().nulls_last(),
                        SourcingAdmissionRow.ready_at,
                        SourcingAdmissionRow.need_id,
                        SourcingAdmissionRow.admission_id,
                    )
                    .limit(limit)
                )
            )
            .all()
        )
        return [self._read_pair(row[0], row[1]) for row in rows]


def _ladder_to_row(value: LadderCheck) -> SourcingLadderCheckRow:
    return SourcingLadderCheckRow(
        tenant_id=value.tenant_id,
        check_id=value.check_id,
        case_id=value.case_id,
        sequence_number=value.sequence_number,
        rung=value.rung.value,
        outcome=value.outcome.value,
        input_snapshot=value.input_snapshot,
        input_snapshot_hash=value.input_snapshot_hash,
        conclusion=value.conclusion,
        match_object_type=value.match_object_type,
        match_object_id=value.match_object_id,
        spec_comparisons=[_comparison_to_json(item) for item in value.spec_comparisons],
        evidence_refs=list(value.evidence_refs),
        checked_by=value.checked_by,
        checked_at=value.checked_at,
    )


def _row_to_ladder(row: SourcingLadderCheckRow) -> LadderCheck:
    return LadderCheck(
        check_id=row.check_id,
        tenant_id=TenantId(row.tenant_id),
        case_id=SourcingCaseId(row.case_id),
        sequence_number=row.sequence_number,
        rung=MatchLadderRung(row.rung),
        outcome=LadderOutcome(row.outcome),
        input_snapshot=row.input_snapshot,
        input_snapshot_hash=row.input_snapshot_hash,
        conclusion=row.conclusion,
        match_object_type=row.match_object_type,
        match_object_id=row.match_object_id,
        spec_comparisons=tuple(
            _comparison_from_json(item) for item in row.spec_comparisons
        ),
        evidence_refs=tuple(str(item) for item in row.evidence_refs),
        checked_by=EmployeeId(row.checked_by),
        checked_at=row.checked_at,
    )


class LadderCheckRepositoryImpl(_TenantBoundRepository):
    async def add(self, tenant_id: TenantId, check: LadderCheck) -> None:
        self._require_tenant(tenant_id)
        if check.tenant_id != tenant_id:
            raise ValueError("阶梯检查租户与请求租户不一致")
        self._session.add(_ladder_to_row(check))

    async def list_for_case(
        self, tenant_id: TenantId, case_id: SourcingCaseId
    ) -> list[LadderCheck]:
        self._require_tenant(tenant_id)
        rows = (
            await self._session.execute(
                self.scoped_query(SourcingLadderCheckRow)
                .where(SourcingLadderCheckRow.case_id == case_id)
                .order_by(SourcingLadderCheckRow.sequence_number)
            )
        ).scalars()
        return [_row_to_ladder(row) for row in rows]


def _plan_to_row(plan: PublicSourcingPlan) -> SourcingPublicPlanRow:
    return SourcingPublicPlanRow(
        tenant_id=plan.tenant_id,
        plan_id=plan.plan_id,
        case_id=plan.case_id,
        target_countries=list(plan.target_countries),
        product_category=plan.product_category,
        queries=[item.model_dump(mode="json") for item in plan.queries],
        max_search_queries=plan.max_search_queries,
        max_pages_read=plan.max_pages_read,
        provider=plan.provider,
        search_depth=plan.search_depth,
        usage_credits_remaining=plan.usage_credits_remaining,
        worst_case_credits=plan.worst_case_credits,
        version=plan.version,
        expected_case_version=plan.expected_case_version,
        plan_hash=plan.plan_hash,
        status=plan.status.value,
        confirmed_by=plan.confirmed_by,
        confirmed_at=plan.confirmed_at,
        authorized_plan_hash=plan.authorized_plan_hash,
        created_at=plan.created_at,
    )


def _row_to_plan(row: SourcingPublicPlanRow) -> PublicSourcingPlan:
    try:
        if not isinstance(row.target_countries, list) or any(
            not isinstance(item, str) for item in row.target_countries
        ):
            raise ValueError
        queries = tuple(
            PublicSourcingQuery.model_validate(item, strict=True)
            for item in row.queries
        )
        if row.provider != "tavily" or row.search_depth != "basic":
            raise ValueError
        command = PublicSourcingPlanCommand(
            plan_id=SourcingPlanId(row.plan_id),
            case_id=SourcingCaseId(row.case_id),
            target_countries=tuple(row.target_countries),
            product_category=row.product_category,
            queries=queries,
            max_search_queries=row.max_search_queries,
            max_pages_read=row.max_pages_read,
            provider="tavily",
            search_depth="basic",
            usage_credits_remaining=row.usage_credits_remaining,
            worst_case_credits=row.worst_case_credits,
            version=row.version,
            expected_case_version=row.expected_case_version,
        )
        canonical = PublicSourcingPlan.create(
            TenantId(row.tenant_id), command, created_at=row.created_at
        )
        if canonical.plan_hash != row.plan_hash:
            raise ValueError
    except Exception:  # noqa: BLE001 - 历史异形 JSON 只投影为固定失败。
        raise ValidationError("公开寻源计划查询绑定无效") from None
    return replace(
        canonical,
        status=PublicPlanStatus(row.status),
        confirmed_by=EmployeeId(row.confirmed_by) if row.confirmed_by else None,
        confirmed_at=row.confirmed_at,
        authorized_plan_hash=row.authorized_plan_hash,
    )


class PublicSourcingPlanRepositoryImpl(_TenantBoundRepository):
    async def add(self, tenant_id: TenantId, plan: PublicSourcingPlan) -> None:
        self._require_tenant(tenant_id)
        if plan.tenant_id != tenant_id:
            raise ValueError("计划租户与请求租户不一致")
        self._session.add(_plan_to_row(plan))

    async def get(
        self, tenant_id: TenantId, plan_id: SourcingPlanId
    ) -> PublicSourcingPlan | None:
        self._require_tenant(tenant_id)
        row = (
            await self._session.execute(
                self.scoped_query(SourcingPublicPlanRow).where(
                    SourcingPublicPlanRow.plan_id == plan_id
                )
            )
        ).scalar_one_or_none()
        return _row_to_plan(row) if row is not None else None

    async def get_for_update(
        self, tenant_id: TenantId, plan_id: SourcingPlanId
    ) -> PublicSourcingPlan | None:
        self._require_tenant(tenant_id)
        row = (
            await self._session.execute(
                self.scoped_query(SourcingPublicPlanRow)
                .where(SourcingPublicPlanRow.plan_id == plan_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        return _row_to_plan(row) if row is not None else None

    async def update(self, tenant_id: TenantId, plan: PublicSourcingPlan) -> None:
        self._require_tenant(tenant_id)
        if plan.tenant_id != tenant_id:
            raise ValueError("计划租户与请求租户不一致")
        if plan.status is PublicPlanStatus.AUTHORIZED:
            current_case_version = await self._session.scalar(
                select(SourcingCaseRow.version)
                .where(
                    SourcingCaseRow.tenant_id == tenant_id,
                    SourcingCaseRow.case_id == plan.case_id,
                )
                .with_for_update()
            )
            if current_case_version != plan.expected_case_version:
                raise SourcingCaseConflictError("公开寻源计划绑定的案例版本已变化")
        row = _plan_to_row(plan)
        predecessor_statuses = {
            PublicPlanStatus.PENDING_CONFIRMATION: ("pending_confirmation",),
            PublicPlanStatus.AUTHORIZED: ("pending_confirmation",),
            PublicPlanStatus.RUNNING: ("authorized",),
            PublicPlanStatus.EXHAUSTED: ("running",),
            PublicPlanStatus.BLOCKED: ("authorized", "running"),
            PublicPlanStatus.COMPLETED: ("running",),
        }[plan.status]
        expected_version = (
            plan.version - 1
            if plan.status is PublicPlanStatus.PENDING_CONFIRMATION
            else plan.version
        )
        predicates = [
            SourcingPublicPlanRow.tenant_id == tenant_id,
            SourcingPublicPlanRow.plan_id == plan.plan_id,
            SourcingPublicPlanRow.version == expected_version,
            SourcingPublicPlanRow.status.in_(predecessor_statuses),
        ]
        if plan.status is not PublicPlanStatus.PENDING_CONFIRMATION:
            predicates.append(SourcingPublicPlanRow.plan_hash == plan.plan_hash)
        result = await self._session.execute(
            update(SourcingPublicPlanRow)
            .where(*predicates)
            .values(
                **{
                    column.name: getattr(row, column.name)
                    for column in SourcingPublicPlanRow.__table__.columns
                    if column.name
                    not in {"tenant_id", "plan_id", "case_id", "created_at"}
                }
            )
        )
        if cast(CursorResult[object], result).rowcount != 1:
            raise SourcingCaseConflictError("公开寻源计划版本已变化")

    async def get_active_for_case(
        self, tenant_id: TenantId, case_id: SourcingCaseId
    ) -> PublicSourcingPlan | None:
        self._require_tenant(tenant_id)
        row = (
            await self._session.execute(
                self.scoped_query(SourcingPublicPlanRow)
                .where(
                    SourcingPublicPlanRow.case_id == case_id,
                    SourcingPublicPlanRow.status.in_(
                        ("pending_confirmation", "authorized", "running")
                    ),
                )
                .order_by(SourcingPublicPlanRow.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return _row_to_plan(row) if row is not None else None


def _pydantic_map_to_json(values: dict[str, BaseModel]) -> dict[str, object]:
    return {key: value.model_dump(mode="json") for key, value in values.items()}


def _candidate_to_row(candidate: SupplierCandidate) -> SourcingCandidateRow:
    if candidate.indicative_price_tiers and not candidate.price_unit:
        raise ValidationError("参考价数量档必须有计价单位")
    tiers = [
        tier.model_dump(mode="json")
        for tier in sorted(
            candidate.indicative_price_tiers,
            key=lambda item: item.minimum_quantity,
        )
    ]
    match = None
    if candidate.match is not None:
        match = {
            "rung": candidate.match.rung.value,
            "comparisons": [
                _comparison_to_json(item) for item in candidate.match.comparisons
            ],
            "summary": candidate.match.summary,
        }
    return SourcingCandidateRow(
        tenant_id=candidate.tenant_id,
        candidate_id=candidate.candidate_id,
        case_id=candidate.case_id,
        supplier_name=candidate.supplier_name,
        source_platform=candidate.source_platform,
        product_title=candidate.product_title,
        observed_facts=_pydantic_map_to_json(
            cast(dict[str, BaseModel], candidate.observed_facts)
        ),
        supplier_claims=_pydantic_map_to_json(
            cast(dict[str, BaseModel], candidate.supplier_claims)
        ),
        match_inferences=_pydantic_map_to_json(
            cast(dict[str, BaseModel], candidate.match_inferences)
        ),
        verified_specs=[_comparison_to_json(item) for item in candidate.verified_specs],
        indicative_price_tiers=tiers,
        moq=candidate.moq,
        price_unit=candidate.price_unit,
        currency=candidate.currency,
        match_explanation=match,
        rejected=candidate.rejected,
        rejection_reasons=[item.value for item in candidate.rejection_reasons],
        verified_by=candidate.verified_by,
        public_draft_source_key=candidate.public_draft_source_key,
        created_at=candidate.created_at,
    )


async def _row_to_candidate(
    session: AsyncSession, row: SourcingCandidateRow
) -> SupplierCandidate:
    evidence_rows = (
        await session.execute(
            select(SourcingCandidateEvidenceRow)
            .where(
                SourcingCandidateEvidenceRow.tenant_id == row.tenant_id,
                SourcingCandidateEvidenceRow.candidate_id == row.candidate_id,
            )
            .order_by(
                SourcingCandidateEvidenceRow.observed_at,
                SourcingCandidateEvidenceRow.artifact_id,
            )
        )
    ).scalars()
    evidence = tuple(
        EvidenceSnapshot(
            url=item.url,
            observed_at=item.observed_at,
            content_hash=item.content_hash,
            artifact_ref=item.artifact_id,
        )
        for item in evidence_rows
    )
    match = None
    if row.match_explanation is not None:
        match = MatchExplanation(
            rung=MatchLadderRung(int(row.match_explanation["rung"])),
            comparisons=[
                _comparison_from_json(item)
                for item in cast(
                    list[dict[str, object]], row.match_explanation["comparisons"]
                )
            ],
            summary=str(row.match_explanation["summary"]),
        )
    prices = tuple(
        IndicativePriceTier.model_validate_json(json.dumps(item))
        for item in row.indicative_price_tiers
    )
    return SupplierCandidate(
        candidate_id=SupplierCandidateId(row.candidate_id),
        tenant_id=TenantId(row.tenant_id),
        case_id=SourcingCaseId(row.case_id),
        supplier_name=row.supplier_name,
        product_title=row.product_title,
        created_at=row.created_at,
        source_platform=row.source_platform,
        observed_facts={
            key: SourcingObservedFact.model_validate_json(json.dumps(value))
            for key, value in row.observed_facts.items()
        },
        supplier_claims={
            key: SourcingSupplierClaim.model_validate_json(json.dumps(value))
            for key, value in row.supplier_claims.items()
        },
        match_inferences={
            key: SourcingMatchInference.model_validate_json(json.dumps(value))
            for key, value in row.match_inferences.items()
        },
        verified_specs=[_comparison_from_json(item) for item in row.verified_specs],
        indicative_price_tiers=prices,
        moq=row.moq,
        price_unit=row.price_unit,
        currency=row.currency,
        evidence=evidence[0] if evidence else None,
        evidence_snapshots=evidence,
        match=match,
        rejected=row.rejected,
        rejection_reasons=[
            PriceRejectionReason(item) for item in row.rejection_reasons
        ],
        verified_by=EmployeeId(row.verified_by) if row.verified_by else None,
        public_draft_source_key=row.public_draft_source_key,
    )


class CandidateRepositoryImpl(_TenantBoundRepository):
    async def add(self, tenant_id: TenantId, candidate: SupplierCandidate) -> None:
        self._require_tenant(tenant_id)
        if candidate.tenant_id != tenant_id:
            raise ValueError("候选租户与请求租户不一致")
        self._session.add(_candidate_to_row(candidate))
        await self._session.flush()
        snapshots = candidate.evidence_snapshots or (
            (candidate.evidence,) if candidate.evidence is not None else ()
        )
        seen: set[str] = set()
        for snapshot in snapshots:
            if snapshot.artifact_ref in seen:
                continue
            seen.add(snapshot.artifact_ref)
            self._session.add(
                SourcingCandidateEvidenceRow(
                    tenant_id=tenant_id,
                    candidate_id=candidate.candidate_id,
                    artifact_id=snapshot.artifact_ref,
                    url=snapshot.url,
                    observed_at=snapshot.observed_at,
                    content_hash=snapshot.content_hash,
                )
            )

    async def get(
        self, tenant_id: TenantId, candidate_id: SupplierCandidateId
    ) -> SupplierCandidate | None:
        self._require_tenant(tenant_id)
        row = (
            await self._session.execute(
                self.scoped_query(SourcingCandidateRow).where(
                    SourcingCandidateRow.candidate_id == candidate_id
                )
            )
        ).scalar_one_or_none()
        return await _row_to_candidate(self._session, row) if row is not None else None

    async def get_or_create_public_draft(
        self, tenant_id: TenantId, candidate: SupplierCandidate
    ) -> tuple[SupplierCandidate, bool]:
        self._require_tenant(tenant_id)
        if (
            candidate.tenant_id != tenant_id
            or candidate.public_draft_source_key is None
        ):
            raise ValueError("公开草稿候选绑定无效")
        row = _candidate_to_row(candidate)
        values = {
            column.name: getattr(row, column.name)
            for column in SourcingCandidateRow.__table__.columns
        }
        inserted_id = (
            await self._session.execute(
                pg_insert(SourcingCandidateRow)
                .values(**values)
                .on_conflict_do_nothing(
                    index_elements=("tenant_id", "public_draft_source_key"),
                    index_where=text("public_draft_source_key IS NOT NULL"),
                )
                .returning(SourcingCandidateRow.candidate_id)
            )
        ).scalar_one_or_none()
        if inserted_id is not None:
            for snapshot in candidate.evidence_snapshots or (
                (candidate.evidence,) if candidate.evidence is not None else ()
            ):
                self._session.add(
                    SourcingCandidateEvidenceRow(
                        tenant_id=tenant_id,
                        candidate_id=candidate.candidate_id,
                        artifact_id=snapshot.artifact_ref,
                        url=snapshot.url,
                        observed_at=snapshot.observed_at,
                        content_hash=snapshot.content_hash,
                    )
                )
            await self._session.flush()
            return candidate, True
        canonical = await self.get_by_public_draft_source_key(
            tenant_id, candidate.public_draft_source_key
        )
        if canonical is None:
            raise ValidationError("公开草稿候选 canonical 冲突不可解析")
        return canonical, False

    async def get_by_public_draft_source_key(
        self, tenant_id: TenantId, source_key: str
    ) -> SupplierCandidate | None:
        self._require_tenant(tenant_id)
        row = (
            await self._session.execute(
                self.scoped_query(SourcingCandidateRow).where(
                    SourcingCandidateRow.public_draft_source_key == source_key
                )
            )
        ).scalar_one_or_none()
        return await _row_to_candidate(self._session, row) if row is not None else None

    async def update(self, tenant_id: TenantId, candidate: SupplierCandidate) -> None:
        self._require_tenant(tenant_id)
        if candidate.tenant_id != tenant_id:
            raise ValueError("候选租户与请求租户不一致")
        row = _candidate_to_row(candidate)
        await self._session.execute(
            update(SourcingCandidateRow)
            .where(
                SourcingCandidateRow.tenant_id == tenant_id,
                SourcingCandidateRow.candidate_id == candidate.candidate_id,
            )
            .values(
                **{
                    column.name: getattr(row, column.name)
                    for column in SourcingCandidateRow.__table__.columns
                    if column.name
                    not in {"tenant_id", "candidate_id", "case_id", "created_at"}
                }
            )
        )

    async def list_for_case(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        include_rejected: bool,
        limit: int | None = None,
    ) -> list[SupplierCandidate]:
        self._require_tenant(tenant_id)
        query = self.scoped_query(SourcingCandidateRow).where(
            SourcingCandidateRow.case_id == case_id
        )
        if not include_rejected:
            query = query.where(SourcingCandidateRow.rejected.is_(False))
        query = query.order_by(
            SourcingCandidateRow.created_at, SourcingCandidateRow.candidate_id
        )
        if limit is not None:
            query = query.limit(limit)
        rows = (await self._session.execute(query)).scalars()
        return [await _row_to_candidate(self._session, row) for row in rows]

    async def count_qualified(
        self, tenant_id: TenantId, case_id: SourcingCaseId
    ) -> int:
        candidates = await self.list_for_case(tenant_id, case_id, False)
        return sum(candidate.passes_verification()[0] for candidate in candidates)


def _option_from_row(row: SourcingSupplyOptionRow) -> SourcingSupplyOption:
    return SourcingSupplyOption(
        option_id=SourcingSupplyOptionId(row.option_id),
        tenant_id=TenantId(row.tenant_id),
        case_id=SourcingCaseId(row.case_id),
        source=SupplyOptionSource(row.source),
        product_id=ProductId(row.product_id),
        supplier_candidate_id=(
            SupplierCandidateId(row.supplier_candidate_id)
            if row.supplier_candidate_id
            else None
        ),
        is_qualified=row.is_qualified,
        created_at=row.created_at,
    )


class SupplyOptionRepositoryImpl(_TenantBoundRepository):
    async def get_or_create_existing_product(
        self, tenant_id: TenantId, option: SourcingSupplyOption
    ) -> tuple[SourcingSupplyOption, bool]:
        self._require_tenant(tenant_id)
        if (
            option.tenant_id != tenant_id
            or option.source is not SupplyOptionSource.EXISTING_PRODUCT
            or option.supplier_candidate_id is not None
        ):
            raise ValueError("现有产品 Option 绑定无效")
        inserted_id = (
            await self._session.execute(
                pg_insert(SourcingSupplyOptionRow)
                .values(
                    tenant_id=tenant_id,
                    option_id=option.option_id,
                    case_id=option.case_id,
                    source=option.source.value,
                    product_id=option.product_id,
                    supplier_candidate_id=None,
                    is_qualified=option.is_qualified,
                    created_at=option.created_at,
                )
                .on_conflict_do_nothing(
                    index_elements=("tenant_id", "case_id", "product_id"),
                    index_where=text("source = 'existing_product'"),
                )
                .returning(SourcingSupplyOptionRow.option_id)
            )
        ).scalar_one_or_none()
        if inserted_id is not None:
            return option, True
        canonical = (
            await self._session.execute(
                self.scoped_query(SourcingSupplyOptionRow).where(
                    SourcingSupplyOptionRow.case_id == option.case_id,
                    SourcingSupplyOptionRow.product_id == option.product_id,
                    SourcingSupplyOptionRow.source
                    == SupplyOptionSource.EXISTING_PRODUCT.value,
                )
            )
        ).scalar_one()
        return _option_from_row(canonical), False

    async def get_or_create_supplier_candidate(
        self, tenant_id: TenantId, option: SourcingSupplyOption
    ) -> tuple[SourcingSupplyOption, bool]:
        self._require_tenant(tenant_id)
        if (
            option.tenant_id != tenant_id
            or option.source is not SupplyOptionSource.SUPPLIER_CANDIDATE
            or option.supplier_candidate_id is None
        ):
            raise ValueError("供应商候选 Option 绑定无效")
        values = {
            "tenant_id": tenant_id,
            "option_id": option.option_id,
            "case_id": option.case_id,
            "source": option.source.value,
            "product_id": option.product_id,
            "supplier_candidate_id": option.supplier_candidate_id,
            "is_qualified": option.is_qualified,
            "created_at": option.created_at,
        }
        inserted_id = (
            await self._session.execute(
                pg_insert(SourcingSupplyOptionRow)
                .values(**values)
                .on_conflict_do_nothing(
                    constraint="uq_sourcing_supply_options_supplier_candidate"
                )
                .returning(SourcingSupplyOptionRow.option_id)
            )
        ).scalar_one_or_none()
        if inserted_id is not None:
            return option, True
        canonical = (
            await self._session.execute(
                self.scoped_query(SourcingSupplyOptionRow).where(
                    SourcingSupplyOptionRow.case_id == option.case_id,
                    SourcingSupplyOptionRow.supplier_candidate_id
                    == option.supplier_candidate_id,
                )
            )
        ).scalar_one()
        return _option_from_row(canonical), False

    async def add(self, tenant_id: TenantId, option: SourcingSupplyOption) -> None:
        self._require_tenant(tenant_id)
        if option.tenant_id != tenant_id:
            raise ValueError("供给选项租户与请求租户不一致")
        self._session.add(
            SourcingSupplyOptionRow(
                tenant_id=tenant_id,
                option_id=option.option_id,
                case_id=option.case_id,
                source=option.source.value,
                product_id=option.product_id,
                supplier_candidate_id=option.supplier_candidate_id,
                is_qualified=option.is_qualified,
                created_at=option.created_at,
            )
        )

    async def get(
        self, tenant_id: TenantId, option_id: SourcingSupplyOptionId
    ) -> SourcingSupplyOption | None:
        self._require_tenant(tenant_id)
        row = (
            await self._session.execute(
                self.scoped_query(SourcingSupplyOptionRow).where(
                    SourcingSupplyOptionRow.option_id == option_id
                )
            )
        ).scalar_one_or_none()
        return _option_from_row(row) if row is not None else None

    async def list_for_case(
        self, tenant_id: TenantId, case_id: SourcingCaseId
    ) -> list[SourcingSupplyOption]:
        self._require_tenant(tenant_id)
        rows = (
            await self._session.execute(
                self.scoped_query(SourcingSupplyOptionRow)
                .where(SourcingSupplyOptionRow.case_id == case_id)
                .order_by(SourcingSupplyOptionRow.option_id)
            )
        ).scalars()
        return [_option_from_row(row) for row in rows]


def _review_from_row(row: SourcingReviewRow) -> SourcingReview:
    return SourcingReview(
        review_id=SourcingReviewId(row.review_id),
        tenant_id=TenantId(row.tenant_id),
        case_id=SourcingCaseId(row.case_id),
        primary_option_id=SourcingSupplyOptionId(row.primary_option_id),
        alternate_option_ids=tuple(
            SourcingSupplyOptionId(item) for item in row.alternate_option_ids
        ),
        reason=row.reason,
        expected_case_version=row.expected_case_version,
        submitted_by=EmployeeId(row.submitted_by),
        submitted_at=row.submitted_at,
        confirmed_by=EmployeeId(row.confirmed_by) if row.confirmed_by else None,
        confirmed_at=row.confirmed_at,
    )


class SourcingReviewRepositoryImpl(_TenantBoundRepository):
    async def _require_case_version(self, review: SourcingReview) -> None:
        matched = await self._session.scalar(
            select(SourcingCaseRow.case_id)
            .where(
                SourcingCaseRow.tenant_id == review.tenant_id,
                SourcingCaseRow.case_id == review.case_id,
                SourcingCaseRow.version == review.expected_case_version,
            )
            .with_for_update()
        )
        if matched is None:
            raise SourcingCaseConflictError("审核绑定的案例版本已变化")

    async def add(self, tenant_id: TenantId, review: SourcingReview) -> None:
        self._require_tenant(tenant_id)
        if review.tenant_id != tenant_id:
            raise ValueError("审核租户与请求租户不一致")
        await self._require_case_version(review)
        self._session.add(
            SourcingReviewRow(
                tenant_id=tenant_id,
                review_id=review.review_id,
                case_id=review.case_id,
                primary_option_id=review.primary_option_id,
                primary_selection={"option_id": str(review.primary_option_id)},
                alternate_option_ids=list(review.alternate_option_ids),
                reason=review.reason,
                expected_case_version=review.expected_case_version,
                submitted_by=review.submitted_by,
                submitted_at=review.submitted_at,
                confirmed_by=review.confirmed_by,
                confirmed_at=review.confirmed_at,
            )
        )

    async def get(
        self, tenant_id: TenantId, review_id: SourcingReviewId
    ) -> SourcingReview | None:
        self._require_tenant(tenant_id)
        row = (
            await self._session.execute(
                self.scoped_query(SourcingReviewRow).where(
                    SourcingReviewRow.review_id == review_id
                )
            )
        ).scalar_one_or_none()
        return _review_from_row(row) if row is not None else None

    async def get_for_case(
        self, tenant_id: TenantId, case_id: SourcingCaseId
    ) -> SourcingReview | None:
        self._require_tenant(tenant_id)
        row = (
            await self._session.execute(
                self.scoped_query(SourcingReviewRow).where(
                    SourcingReviewRow.case_id == case_id
                )
            )
        ).scalar_one_or_none()
        return _review_from_row(row) if row is not None else None

    async def update(self, tenant_id: TenantId, review: SourcingReview) -> None:
        self._require_tenant(tenant_id)
        if review.tenant_id != tenant_id:
            raise ValueError("审核租户与请求租户不一致")
        await self._require_case_version(review)
        result = await self._session.execute(
            update(SourcingReviewRow)
            .where(
                SourcingReviewRow.tenant_id == tenant_id,
                SourcingReviewRow.review_id == review.review_id,
                SourcingReviewRow.case_id == review.case_id,
                SourcingReviewRow.expected_case_version == review.expected_case_version,
                SourcingReviewRow.confirmed_by.is_(None),
                SourcingReviewRow.confirmed_at.is_(None),
            )
            .values(confirmed_by=review.confirmed_by, confirmed_at=review.confirmed_at)
        )
        if cast(CursorResult[object], result).rowcount != 1:
            raise SourcingCaseConflictError("审核事实已变化")


def _handoff_quantity(snapshot: dict[str, object]) -> int | None:
    quantity_fact = snapshot.get("quantity")
    if not isinstance(quantity_fact, dict):
        return None
    quantity = quantity_fact.get("value")
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
        return None
    return quantity


class SourcingHandoffRepositoryImpl(_TenantBoundRepository):
    """只读构建已绑定 Opportunity 与已确认主选的冻结成本交接快照。"""

    async def get_snapshot(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        review_id: SourcingReviewId,
    ) -> SourcingHandoffSnapshot | None:
        self._require_tenant(tenant_id)
        row = (
            await self._session.execute(
                select(
                    SourcingCaseRow,
                    SourcingReviewRow,
                    SourcingSupplyOptionRow,
                    ProductRow,
                )
                .join(
                    SourcingReviewRow,
                    (SourcingReviewRow.tenant_id == SourcingCaseRow.tenant_id)
                    & (SourcingReviewRow.case_id == SourcingCaseRow.case_id),
                )
                .join(
                    SourcingSupplyOptionRow,
                    (SourcingSupplyOptionRow.tenant_id == SourcingReviewRow.tenant_id)
                    & (SourcingSupplyOptionRow.case_id == SourcingReviewRow.case_id)
                    & (
                        SourcingSupplyOptionRow.option_id
                        == SourcingReviewRow.primary_option_id
                    ),
                )
                .join(
                    ProductRow,
                    (ProductRow.tenant_id == SourcingSupplyOptionRow.tenant_id)
                    & (ProductRow.product_id == SourcingSupplyOptionRow.product_id),
                )
                .where(
                    SourcingCaseRow.tenant_id == tenant_id,
                    SourcingReviewRow.tenant_id == tenant_id,
                    SourcingSupplyOptionRow.tenant_id == tenant_id,
                    ProductRow.tenant_id == tenant_id,
                    SourcingCaseRow.case_id == case_id,
                    SourcingReviewRow.review_id == review_id,
                    SourcingReviewRow.confirmed_by.is_not(None),
                    SourcingReviewRow.confirmed_at.is_not(None),
                    SourcingSupplyOptionRow.is_qualified.is_(True),
                    SourcingCaseRow.state == CaseState.HANDED_TO_COSTING.value,
                    SourcingCaseRow.opportunity_id.is_not(None),
                    SourcingCaseRow.version
                    == SourcingReviewRow.expected_case_version + 1,
                )
            )
        ).one_or_none()
        if row is None:
            return None
        case, review, option, product = row
        quantity = _handoff_quantity(case.need_snapshot)
        if case.opportunity_id is None or quantity is None:
            return None

        price_options: tuple[SourcingCostPriceOption, ...]
        supplier_candidate_id: SupplierCandidateId | None
        if option.source == SupplyOptionSource.SUPPLIER_CANDIDATE.value:
            if option.supplier_candidate_id is None:
                return None
            candidate = await self._session.scalar(
                select(SourcingCandidateRow).where(
                    SourcingCandidateRow.tenant_id == tenant_id,
                    SourcingCandidateRow.case_id == case_id,
                    SourcingCandidateRow.candidate_id == option.supplier_candidate_id,
                )
            )
            source = await self._session.scalar(
                select(ProductCandidateSourceRow).where(
                    ProductCandidateSourceRow.tenant_id == tenant_id,
                    ProductCandidateSourceRow.product_id == option.product_id,
                    ProductCandidateSourceRow.sourcing_case_id == case_id,
                    ProductCandidateSourceRow.supplier_candidate_id
                    == option.supplier_candidate_id,
                )
            )
            if (
                candidate is None
                or source is None
                or candidate.moq is None
                or product.moq is None
                or candidate.moq != product.moq
                or quantity < candidate.moq
            ):
                return None
            evidence_refs = set(
                (
                    await self._session.execute(
                        select(SourcingCandidateEvidenceRow.artifact_id).where(
                            SourcingCandidateEvidenceRow.tenant_id == tenant_id,
                            SourcingCandidateEvidenceRow.candidate_id
                            == option.supplier_candidate_id,
                        )
                    )
                ).scalars()
            )
            price_rows = list(
                (
                    await self._session.execute(
                        select(ProductCandidatePriceRefRow)
                        .where(
                            ProductCandidatePriceRefRow.tenant_id == tenant_id,
                            ProductCandidatePriceRefRow.product_id == option.product_id,
                        )
                        .order_by(
                            ProductCandidatePriceRefRow.minimum_quantity,
                            ProductCandidatePriceRefRow.artifact_id,
                        )
                    )
                ).scalars()
            )
            if not price_rows or any(
                item.artifact_id not in evidence_refs for item in price_rows
            ):
                return None
            price_options = tuple(
                SourcingCostPriceOption(
                    minimum_quantity=item.minimum_quantity,
                    unit_amount=item.unit_amount,
                    currency=item.currency,
                    unit=item.unit,
                    evidence_ref=ArtifactId(item.artifact_id),
                    source_kind="supplier_candidate",
                )
                for item in price_rows
            )
            supplier_candidate_id = SupplierCandidateId(option.supplier_candidate_id)
            moq = candidate.moq
        elif option.source == SupplyOptionSource.EXISTING_PRODUCT.value:
            cost_parts = (
                product.internal_cost_amount,
                product.internal_cost_currency,
                product.internal_cost_basis,
                product.internal_cost_unit,
                product.internal_cost_source_ref,
            )
            if (
                not all(item is not None for item in cost_parts)
                or product.moq is None
                or quantity < product.moq
                or not str(product.internal_cost_basis).strip()
                or not str(product.internal_cost_unit).strip()
            ):
                return None
            price_options = (
                SourcingCostPriceOption(
                    minimum_quantity=product.moq,
                    unit_amount=cast(Decimal, product.internal_cost_amount),
                    currency=cast(str, product.internal_cost_currency),
                    unit=cast(str, product.internal_cost_unit),
                    evidence_ref=ArtifactId(
                        cast(str, product.internal_cost_source_ref)
                    ),
                    source_kind="existing_product",
                ),
            )
            supplier_candidate_id = None
            moq = product.moq
        else:
            return None

        try:
            return SourcingHandoffSnapshot(
                case_id=SourcingCaseId(case.case_id),
                review_id=SourcingReviewId(review.review_id),
                need_id=ValidatedNeedId(case.need_id),
                opportunity_id=OpportunityId(case.opportunity_id),
                primary_option_id=SourcingSupplyOptionId(option.option_id),
                product_id=ProductId(option.product_id),
                supplier_candidate_id=supplier_candidate_id,
                quantity=quantity,
                moq=moq,
                price_options=price_options,
            )
        except ValueError:
            return None


def _execution_from_row(row: SourcingSearchExecutionRow) -> SourcingSearchExecution:
    return SourcingSearchExecution(
        execution_id=row.execution_id,
        tenant_id=TenantId(row.tenant_id),
        case_id=SourcingCaseId(row.case_id),
        plan_id=SourcingPlanId(row.plan_id),
        run_id=RunId(row.run_id),
        plan_hash=row.plan_hash,
        query_index=row.query_index,
        request_key=row.request_key,
        query_hash=row.query_hash,
        locator_results=tuple(
            cast(dict[str, object], item) for item in row.locator_results
        ),
        provider_status=SourcingSearchExecutionStatus(row.provider_status),
        created_at=row.created_at,
        completed_at=row.completed_at,
    )


class SourcingSearchExecutionRepositoryImpl(_TenantBoundRepository):
    async def get_or_create_canonical(
        self, tenant_id: TenantId, execution: SourcingSearchExecution
    ) -> SourcingSearchExecution:
        self._require_tenant(tenant_id)
        if execution.tenant_id != tenant_id:
            raise ValueError("搜索回执租户与请求租户不一致")
        values = {
            "tenant_id": str(tenant_id),
            "execution_id": execution.execution_id,
            "case_id": str(execution.case_id),
            "plan_id": str(execution.plan_id),
            "run_id": execution.run_id,
            "plan_hash": execution.plan_hash,
            "query_index": execution.query_index,
            "request_key": execution.request_key,
            "query_hash": execution.query_hash,
            "locator_results": list(execution.locator_results),
            "provider_status": execution.provider_status.value,
            "created_at": execution.created_at,
            "completed_at": execution.completed_at,
        }
        inserted = (
            await self._session.execute(
                pg_insert(SourcingSearchExecutionRow)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["tenant_id", "request_key"])
                .returning(SourcingSearchExecutionRow.execution_id)
            )
        ).scalar_one_or_none()
        canonical = await self.get_by_request_key(tenant_id, execution.request_key)
        if canonical is None:
            raise ValidationError("搜索回执幂等读取失败")
        if inserted is None and (
            canonical.execution_id != execution.execution_id
            or canonical.tenant_id != execution.tenant_id
            or canonical.case_id != execution.case_id
            or canonical.plan_id != execution.plan_id
            or canonical.run_id != execution.run_id
            or canonical.plan_hash != execution.plan_hash
            or canonical.query_index != execution.query_index
            or canonical.request_key != execution.request_key
            or canonical.query_hash != execution.query_hash
            or canonical.locator_results != execution.locator_results
            or canonical.provider_status is not execution.provider_status
        ):
            raise ValidationError("搜索回执幂等键已绑定不同内容")
        return canonical

    async def add(
        self, tenant_id: TenantId, execution: SourcingSearchExecution
    ) -> None:
        self._require_tenant(tenant_id)
        if execution.tenant_id != tenant_id:
            raise ValueError("搜索回执租户与请求租户不一致")
        self._session.add(
            SourcingSearchExecutionRow(
                tenant_id=tenant_id,
                execution_id=execution.execution_id,
                case_id=execution.case_id,
                plan_id=execution.plan_id,
                run_id=execution.run_id,
                plan_hash=execution.plan_hash,
                query_index=execution.query_index,
                request_key=execution.request_key,
                query_hash=execution.query_hash,
                locator_results=list(execution.locator_results),
                provider_status=execution.provider_status.value,
                created_at=execution.created_at,
                completed_at=execution.completed_at,
            )
        )

    async def get_by_request_key(
        self, tenant_id: TenantId, request_key: str
    ) -> SourcingSearchExecution | None:
        self._require_tenant(tenant_id)
        row = (
            await self._session.execute(
                self.scoped_query(SourcingSearchExecutionRow).where(
                    SourcingSearchExecutionRow.request_key == request_key
                )
            )
        ).scalar_one_or_none()
        return _execution_from_row(row) if row is not None else None

    async def list_uncertain_for_case(
        self, tenant_id: TenantId, case_id: SourcingCaseId, limit: int
    ) -> list[SourcingSearchExecution]:
        self._require_tenant(tenant_id)
        rows = (
            await self._session.execute(
                self.scoped_query(SourcingSearchExecutionRow)
                .where(
                    SourcingSearchExecutionRow.case_id == case_id,
                    SourcingSearchExecutionRow.provider_status
                    == SourcingSearchExecutionStatus.UNCERTAIN.value,
                )
                .order_by(
                    SourcingSearchExecutionRow.created_at,
                    SourcingSearchExecutionRow.execution_id,
                )
                .limit(limit)
            )
        ).scalars()
        return [_execution_from_row(row) for row in rows]

    async def update(
        self, tenant_id: TenantId, execution: SourcingSearchExecution
    ) -> None:
        self._require_tenant(tenant_id)
        if execution.tenant_id != tenant_id:
            raise ValueError("搜索回执租户与请求租户不一致")
        await self._session.execute(
            update(SourcingSearchExecutionRow)
            .where(
                SourcingSearchExecutionRow.tenant_id == tenant_id,
                SourcingSearchExecutionRow.execution_id == execution.execution_id,
                SourcingSearchExecutionRow.request_key == execution.request_key,
            )
            .values(
                locator_results=list(execution.locator_results),
                provider_status=execution.provider_status.value,
                completed_at=execution.completed_at,
            )
        )


def _draft_from_row(row: SourcingCandidateDraftRow) -> PublicCandidateDraft:
    return PublicCandidateDraft(
        draft_id=row.draft_id,
        tenant_id=TenantId(row.tenant_id),
        case_id=SourcingCaseId(row.case_id),
        run_id=RunId(row.run_id),
        plan_id=SourcingPlanId(row.plan_id),
        plan_hash=row.plan_hash,
        query_index=row.query_index,
        result_index=row.result_index,
        source_key=row.source_key,
        supplier_name=row.supplier_name,
        product_title=row.product_title,
        specs=tuple(
            PublicCandidateDraftSpec.model_validate(item) for item in row.specs
        ),
        moq=row.moq,
        indicative_price_tiers=tuple(
            PublicCandidateDraftPriceTier.model_validate(
                {**item, "amount": Decimal(str(item["amount"]))}
            )
            for item in row.indicative_price_tiers
        ),
        rejection_codes=tuple(row.rejection_codes),
        evidence_url=row.evidence_url,
        evidence_observed_at=row.evidence_observed_at,
        evidence_hash=row.evidence_hash,
        evidence_artifact_ref=ArtifactId(row.evidence_artifact_ref),
        created_at=row.created_at,
    )


class PublicCandidateDraftRepositoryImpl(_TenantBoundRepository):
    """按已核验 Artifact 位置原子返回唯一校准草稿。"""

    async def get_or_create_canonical(
        self, tenant_id: TenantId, draft: PublicCandidateDraft
    ) -> PublicCandidateDraft:
        self._require_tenant(tenant_id)
        if draft.tenant_id != tenant_id:
            raise ValueError("候选草稿租户与请求租户不一致")
        values = draft.model_dump(mode="json")
        values["case_id"] = str(draft.case_id)
        values["run_id"] = str(draft.run_id)
        values["plan_id"] = str(draft.plan_id)
        values["evidence_artifact_ref"] = str(draft.evidence_artifact_ref)
        values["evidence_observed_at"] = draft.evidence_observed_at
        values["created_at"] = draft.created_at
        values["specs"] = [item.model_dump(mode="json") for item in draft.specs]
        values["indicative_price_tiers"] = [
            item.model_dump(mode="json") for item in draft.indicative_price_tiers
        ]
        inserted = (
            await self._session.execute(
                pg_insert(SourcingCandidateDraftRow)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["tenant_id", "source_key"])
                .returning(SourcingCandidateDraftRow.draft_id)
            )
        ).scalar_one_or_none()
        row = (
            await self._session.execute(
                self.scoped_query(SourcingCandidateDraftRow).where(
                    SourcingCandidateDraftRow.source_key == draft.source_key
                )
            )
        ).scalar_one()
        canonical = _draft_from_row(row)
        if canonical != draft and inserted is None:
            raise ValidationError("候选草稿幂等键已绑定不同内容")
        return canonical

    async def get_by_source_key(
        self, tenant_id: TenantId, source_key: str
    ) -> PublicCandidateDraft | None:
        self._require_tenant(tenant_id)
        row = (
            await self._session.execute(
                self.scoped_query(SourcingCandidateDraftRow).where(
                    SourcingCandidateDraftRow.source_key == source_key
                )
            )
        ).scalar_one_or_none()
        return _draft_from_row(row) if row is not None else None

    async def list_exact_for_verification(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        run_id: RunId,
        plan_id: SourcingPlanId,
        plan_hash: str,
    ) -> list[PublicCandidateDraft]:
        self._require_tenant(tenant_id)
        rows = (
            await self._session.execute(
                self.scoped_query(SourcingCandidateDraftRow)
                .where(
                    SourcingCandidateDraftRow.case_id == case_id,
                    SourcingCandidateDraftRow.run_id == run_id,
                    SourcingCandidateDraftRow.plan_id == plan_id,
                    SourcingCandidateDraftRow.plan_hash == plan_hash,
                )
                .order_by(
                    SourcingCandidateDraftRow.query_index,
                    SourcingCandidateDraftRow.result_index,
                    SourcingCandidateDraftRow.source_key,
                )
            )
        ).scalars()
        return [_draft_from_row(row) for row in rows]


def _reconciliation_from_row(
    row: SourcingSearchReconciliationRow,
) -> SourcingSearchReconciliation:
    return SourcingSearchReconciliation(
        reconciliation_id=row.reconciliation_id,
        tenant_id=TenantId(row.tenant_id),
        execution_id=row.execution_id,
        status=SourcingReconciliationStatus(row.status),
        reason=row.reason,
        provider_usage_artifact_ref=ArtifactId(row.provider_usage_artifact_ref),
        created_at=row.created_at,
        reconciled_by=EmployeeId(row.reconciled_by) if row.reconciled_by else None,
        reconciled_at=row.reconciled_at,
    )


class SourcingSearchReconciliationRepositoryImpl(_TenantBoundRepository):
    async def get_or_create_canonical(
        self, tenant_id: TenantId, reconciliation: SourcingSearchReconciliation
    ) -> SourcingSearchReconciliation:
        self._require_tenant(tenant_id)
        if reconciliation.tenant_id != tenant_id:
            raise ValueError("人工核对租户与请求租户不一致")
        values = {
            "tenant_id": tenant_id,
            "reconciliation_id": reconciliation.reconciliation_id,
            "execution_id": reconciliation.execution_id,
            "status": reconciliation.status.value,
            "reason": reconciliation.reason,
            "provider_usage_artifact_ref": reconciliation.provider_usage_artifact_ref,
            "created_at": reconciliation.created_at,
            "reconciled_by": reconciliation.reconciled_by,
            "reconciled_at": reconciliation.reconciled_at,
        }
        inserted_id = (
            await self._session.execute(
                pg_insert(SourcingSearchReconciliationRow)
                .values(**values)
                .on_conflict_do_nothing()
                .returning(SourcingSearchReconciliationRow.reconciliation_id)
            )
        ).scalar_one_or_none()
        if inserted_id is not None:
            return reconciliation
        rows = (
            (
                await self._session.execute(
                    self.scoped_query(SourcingSearchReconciliationRow)
                    .where(
                        or_(
                            SourcingSearchReconciliationRow.reconciliation_id
                            == reconciliation.reconciliation_id,
                            SourcingSearchReconciliationRow.execution_id
                            == reconciliation.execution_id,
                        )
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        if len(rows) != 1:
            raise SourcingPlanStaleError("不确定搜索核对事实冲突")
        canonical = _reconciliation_from_row(rows[0])
        if (
            canonical.reconciliation_id != reconciliation.reconciliation_id
            or canonical.execution_id != reconciliation.execution_id
            or canonical.status is not reconciliation.status
            or canonical.reason != reconciliation.reason
            or canonical.provider_usage_artifact_ref
            != reconciliation.provider_usage_artifact_ref
            or canonical.reconciled_by != reconciliation.reconciled_by
        ):
            raise SourcingPlanStaleError("不确定搜索核对事实冲突")
        return canonical

    async def add(
        self, tenant_id: TenantId, reconciliation: SourcingSearchReconciliation
    ) -> None:
        self._require_tenant(tenant_id)
        if reconciliation.tenant_id != tenant_id:
            raise ValueError("人工核对租户与请求租户不一致")
        self._session.add(
            SourcingSearchReconciliationRow(
                tenant_id=tenant_id,
                reconciliation_id=reconciliation.reconciliation_id,
                execution_id=reconciliation.execution_id,
                status=reconciliation.status.value,
                reason=reconciliation.reason,
                provider_usage_artifact_ref=reconciliation.provider_usage_artifact_ref,
                created_at=reconciliation.created_at,
                reconciled_by=reconciliation.reconciled_by,
                reconciled_at=reconciliation.reconciled_at,
            )
        )

    async def get_for_execution(
        self, tenant_id: TenantId, execution_id: str
    ) -> SourcingSearchReconciliation | None:
        self._require_tenant(tenant_id)
        row = (
            await self._session.execute(
                self.scoped_query(SourcingSearchReconciliationRow).where(
                    SourcingSearchReconciliationRow.execution_id == execution_id
                )
            )
        ).scalar_one_or_none()
        return _reconciliation_from_row(row) if row is not None else None


__all__ = (
    "CandidateRepositoryImpl",
    "LadderCheckRepositoryImpl",
    "PublicCandidateDraftRepositoryImpl",
    "PublicSourcingPlanRepositoryImpl",
    "SourcingCaseRepositoryImpl",
    "SourcingHandoffRepositoryImpl",
    "SourcingReviewRepositoryImpl",
    "SourcingSearchExecutionRepositoryImpl",
    "SourcingSearchReconciliationRepositoryImpl",
    "SupplyOptionRepositoryImpl",
)
