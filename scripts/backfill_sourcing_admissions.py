"""为可核验的历史 V2 Sourcing Case 显式补建寻源准入。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Never, Protocol, cast

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from domains.demand.schemas import NeedClusterPriorityFacts
from domains.demand.service_impl import DemandServiceImpl
from domains.sourcing.permissions import (
    Phase2SourcingAuthorizer,
    SourcingActor,
    SourcingScope,
)
from domains.sourcing.schemas import (
    SourcingAdmissionEnqueueCommand,
    SourcingNeedSnapshot,
    SourcingPriorityFactsInput,
    canonical_sourcing_need_snapshot_hash,
)
from domains.sourcing.service import CandidateEvidenceSnapshot
from domains.sourcing.service_impl import SourcingServiceImpl
from infra.db.demand_uow import SqlAlchemyDemandUnitOfWork
from infra.db.session import create_engine_from
from infra.db.sourcing_uow import SqlAlchemySourcingUnitOfWork
from infra.db.tables import (
    SourcingAdmissionRow,
    SourcingCaseRow,
    WorkflowRunRow,
)
from shared.errors import ValidationError
from shared.schemas.identifiers import (
    ArtifactId,
    SourcingCaseId,
    TenantId,
    ValidatedNeedId,
)

BackfillMode = Literal["dry_run", "apply"]
BackfillStatus = Literal["would_create", "applied", "skipped", "unknown"]

_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,39}\Z")
_STARTED_STATES = frozenset({"discovering", "verifying", "candidates_ready"})
_TERMINAL_STATES = frozenset({"handed_to_costing", "failed"})
_RESULT_STATUSES = frozenset({"would_create", "applied", "skipped", "unknown"})
_SAFE_REASONS = frozenset(
    {
        "eligible",
        "admission_ensured",
        "already_admitted",
        "tenant_mismatch",
        "workflow_run_exists",
        "case_already_started",
        "case_terminal",
        "case_state_invalid",
        "unsupported_workflow_version",
        "invalid_case_record",
        "invalid_opened_at",
        "malformed_need_snapshot",
        "priority_facts_invalid",
        "priority_facts_read_unknown",
        "eligibility_changed",
        "admission_write_unknown",
    }
)


@dataclass(frozen=True)
class HistoricalSourcingCase:
    """回填判断所需的最小数据库投影；敏感快照不得进入输出。"""

    tenant_id: TenantId
    case_id: str
    need_id: str
    workflow_version: int
    state: str
    opened_at: object
    need_snapshot: object
    need_snapshot_hash: object
    has_workflow_run: bool
    has_admission: bool


@dataclass(frozen=True)
class BackfillResult:
    """单个案例的安全结果；字段值均来自固定白名单。"""

    case_id: str
    status: BackfillStatus
    reason: str

    def __post_init__(self) -> None:
        if _SAFE_ID.fullmatch(self.case_id) is None:
            raise ValidationError("回填结果 Case ID 无效")
        if self.status not in _RESULT_STATUSES or self.reason not in _SAFE_REASONS:
            raise ValidationError("回填结果分类无效")

    def to_dict(self) -> dict[str, str]:
        return {
            "case_id": self.case_id,
            "status": self.status,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class BackfillReport:
    """命令唯一允许输出的安全报告。"""

    status: Literal["completed", "unknown"]
    mode: BackfillMode
    results: tuple[BackfillResult, ...]

    @classmethod
    def empty(cls, *, mode: BackfillMode) -> BackfillReport:
        return cls(status="completed", mode=mode, results=())

    @classmethod
    def from_results(
        cls,
        *,
        mode: BackfillMode,
        results: Sequence[BackfillResult | Mapping[str, str]],
    ) -> BackfillReport:
        normalized: list[BackfillResult] = []
        for item in results:
            if isinstance(item, BackfillResult):
                normalized.append(item)
                continue
            normalized.append(
                BackfillResult(
                    case_id=item["case_id"],
                    status=cast(BackfillStatus, item["status"]),
                    reason=item["reason"],
                )
            )
        status: Literal["completed", "unknown"] = (
            "unknown"
            if any(item.status == "unknown" for item in normalized)
            else "completed"
        )
        return cls(status=status, mode=mode, results=tuple(normalized))

    @property
    def counts(self) -> dict[str, int]:
        return {
            "total": len(self.results),
            "would_create": sum(
                item.status == "would_create" for item in self.results
            ),
            "applied": sum(item.status == "applied" for item in self.results),
            "skipped": sum(item.status == "skipped" for item in self.results),
            "unknown": sum(item.status == "unknown" for item in self.results),
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "mode": self.mode,
            "counts": self.counts,
            "results": [item.to_dict() for item in self.results],
        }


class HistoricalCaseInventory(Protocol):
    """仅按显式 tenant 读取历史 V2 Case 的最小投影。"""

    async def list_v2_cases(
        self, tenant_id: TenantId
    ) -> list[HistoricalSourcingCase]: ...


class DemandPriorityReader(Protocol):
    async def get_cluster_priority_facts(
        self, tenant_id: TenantId, need_id: ValidatedNeedId
    ) -> NeedClusterPriorityFacts: ...


class SourcingAdmissionWriter(Protocol):
    async def enqueue_admission(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        need_id: ValidatedNeedId,
        *,
        ready_at: datetime,
        command: SourcingAdmissionEnqueueCommand,
        actor: SourcingActor,
    ) -> object: ...


class PostgresHistoricalCaseInventory:
    """在 SQL 层先绑定 tenant，再投影回填判定所需字段。"""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        tenant_id: TenantId,
    ) -> None:
        _require_safe_id(tenant_id)
        self._factory = factory
        self._tenant_id = tenant_id

    async def list_v2_cases(
        self, tenant_id: TenantId
    ) -> list[HistoricalSourcingCase]:
        if tenant_id != self._tenant_id:
            raise ValidationError("历史回填租户绑定无效")
        has_workflow_run = exists(
            select(WorkflowRunRow.run_id).where(
                WorkflowRunRow.tenant_id == SourcingCaseRow.tenant_id,
                WorkflowRunRow.workflow_type == "sourcing_case",
                WorkflowRunRow.subject_ref == SourcingCaseRow.case_id,
            )
        )
        has_admission = exists(
            select(SourcingAdmissionRow.admission_id).where(
                SourcingAdmissionRow.tenant_id == SourcingCaseRow.tenant_id,
                SourcingAdmissionRow.case_id == SourcingCaseRow.case_id,
            )
        )
        statement = (
            select(
                SourcingCaseRow.tenant_id,
                SourcingCaseRow.case_id,
                SourcingCaseRow.need_id,
                SourcingCaseRow.workflow_version,
                SourcingCaseRow.state,
                SourcingCaseRow.opened_at,
                SourcingCaseRow.need_snapshot,
                SourcingCaseRow.need_snapshot_hash,
                has_workflow_run.label("has_workflow_run"),
                has_admission.label("has_admission"),
            )
            .where(
                SourcingCaseRow.tenant_id == str(tenant_id),
                SourcingCaseRow.workflow_version == 2,
            )
            .order_by(SourcingCaseRow.case_id)
        )
        async with self._factory() as session:
            rows = (await session.execute(statement)).all()
        return [
            HistoricalSourcingCase(
                tenant_id=TenantId(row.tenant_id),
                case_id=row.case_id,
                need_id=row.need_id,
                workflow_version=row.workflow_version,
                state=row.state,
                opened_at=row.opened_at,
                need_snapshot=row.need_snapshot,
                need_snapshot_hash=row.need_snapshot_hash,
                has_workflow_run=bool(row.has_workflow_run),
                has_admission=bool(row.has_admission),
            )
            for row in rows
        ]


class _UnavailableCandidateEvidenceReader:
    """回填不读取候选证据；误入该能力时固定失败关闭。"""

    async def read_verified(
        self, tenant_id: TenantId, artifact_id: ArtifactId
    ) -> CandidateEvidenceSnapshot:
        del tenant_id, artifact_id
        raise ValidationError("历史准入回填不允许读取候选证据")


def _require_safe_id(value: object) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise ValidationError("历史回填标识无效")
    return value


def _safe_case_id(value: object) -> str | None:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        return None
    return value


def _utc_opened_at(value: object, *, now: datetime) -> datetime | None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
        or value > now
    ):
        return None
    return value


def _verified_snapshot(row: HistoricalSourcingCase) -> SourcingNeedSnapshot | None:
    try:
        snapshot = SourcingNeedSnapshot.model_validate_json(
            json.dumps(row.need_snapshot, ensure_ascii=False, separators=(",", ":"))
        )
        if (
            snapshot.need_id != ValidatedNeedId(row.need_id)
            or snapshot.completeness < 3
            or not isinstance(row.need_snapshot_hash, str)
            or row.need_snapshot_hash != snapshot.snapshot_hash
            or canonical_sourcing_need_snapshot_hash(snapshot)
            != snapshot.snapshot_hash
        ):
            return None
        return snapshot
    except Exception:  # noqa: BLE001 - 原始快照错误只能映射固定分类。
        return None


def _priority_input(
    value: object,
    *,
    need_id: ValidatedNeedId,
    now: datetime,
) -> SourcingPriorityFactsInput | None:
    try:
        if not isinstance(value, NeedClusterPriorityFacts) or value.need_id != need_id:
            return None
        facts = SourcingPriorityFactsInput(
            need_id=need_id,
            cluster_id=value.cluster_id,  # type: ignore[arg-type]
            cluster_member_count=value.cluster_member_count,
            facts_observed_at=value.facts_observed_at,
        )
        if facts.facts_observed_at > now:
            return None
        return facts
    except Exception:  # noqa: BLE001 - Demand 损坏事实不得穿透或被猜测。
        return None


def _skip(case_id: str, reason: str) -> BackfillResult:
    return BackfillResult(case_id=case_id, status="skipped", reason=reason)


async def execute_backfill(
    *,
    tenant_id: TenantId,
    apply: bool,
    inventory: HistoricalCaseInventory,
    demand: DemandPriorityReader,
    sourcing: SourcingAdmissionWriter,
    now: Callable[[], datetime],
) -> BackfillReport:
    """检查全部同租户 V2 Case；只有明确符合条件者才调用准入域服务。"""
    _require_safe_id(tenant_id)
    if type(apply) is not bool or not callable(now):
        raise ValidationError("历史回填参数无效")
    observed_now = now()
    if (
        not isinstance(observed_now, datetime)
        or observed_now.tzinfo is None
        or observed_now.utcoffset() != timedelta(0)
    ):
        raise ValidationError("历史回填时钟无效")
    rows = await inventory.list_v2_cases(tenant_id)
    rows.sort(key=lambda item: str(getattr(item, "case_id", "")))
    mode: BackfillMode = "apply" if apply else "dry_run"
    actor = SourcingActor(
        "system:sourcing-admission-backfill",
        tenant_id,
        SourcingScope.SYSTEM,
        "system",
    )
    results: list[BackfillResult] = []
    for row in rows:
        if not isinstance(row, HistoricalSourcingCase):
            raise ValidationError("历史回填数据投影无效")
        case_id = _safe_case_id(row.case_id)
        if case_id is None or _safe_case_id(row.need_id) is None:
            if case_id is None:
                raise ValidationError("历史回填 Case 标识无效")
            results.append(_skip(case_id, "invalid_case_record"))
            continue
        if row.tenant_id != tenant_id:
            results.append(_skip(case_id, "tenant_mismatch"))
            continue
        if type(row.workflow_version) is not int or row.workflow_version != 2:
            results.append(_skip(case_id, "unsupported_workflow_version"))
            continue
        if row.has_admission:
            results.append(_skip(case_id, "already_admitted"))
            continue
        if row.has_workflow_run:
            results.append(_skip(case_id, "workflow_run_exists"))
            continue
        if row.state in _STARTED_STATES:
            results.append(_skip(case_id, "case_already_started"))
            continue
        if row.state in _TERMINAL_STATES:
            results.append(_skip(case_id, "case_terminal"))
            continue
        if row.state != "opened":
            results.append(_skip(case_id, "case_state_invalid"))
            continue
        opened_at = _utc_opened_at(row.opened_at, now=observed_now)
        if opened_at is None:
            results.append(_skip(case_id, "invalid_opened_at"))
            continue
        snapshot = _verified_snapshot(row)
        if snapshot is None:
            results.append(_skip(case_id, "malformed_need_snapshot"))
            continue
        need_id = ValidatedNeedId(row.need_id)
        try:
            raw_facts = await demand.get_cluster_priority_facts(tenant_id, need_id)
        except ValidationError:
            results.append(_skip(case_id, "priority_facts_invalid"))
            continue
        except Exception:  # noqa: BLE001 - 存储状态未知时停止，禁止误报坏事实。
            results.append(
                BackfillResult(
                    case_id,
                    status="unknown",
                    reason="priority_facts_read_unknown",
                )
            )
            break
        facts = _priority_input(raw_facts, need_id=need_id, now=observed_now)
        if facts is None:
            results.append(_skip(case_id, "priority_facts_invalid"))
            continue
        if not apply:
            results.append(
                BackfillResult(case_id, status="would_create", reason="eligible")
            )
            continue
        try:
            await sourcing.enqueue_admission(
                tenant_id,
                SourcingCaseId(case_id),
                need_id,
                ready_at=opened_at,
                command=SourcingAdmissionEnqueueCommand(facts=facts),
                actor=actor,
            )
        except ValidationError:
            results.append(_skip(case_id, "eligibility_changed"))
            continue
        except Exception:  # noqa: BLE001 - 结果不确定时停止，禁止扩大未知写入面。
            results.append(
                BackfillResult(
                    case_id,
                    status="unknown",
                    reason="admission_write_unknown",
                )
            )
            break
        results.append(
            BackfillResult(case_id, status="applied", reason="admission_ensured")
        )
    return BackfillReport.from_results(mode=mode, results=tuple(results))


async def run_database_backfill(
    database_url: str,
    tenant_id: TenantId,
    apply: bool,
) -> BackfillReport:
    """装配真实 Demand reader 与 Sourcing service；本函数从不启动 Workflow。"""
    engine: AsyncEngine = create_engine_from(database_url)
    try:
        factory = async_sessionmaker(
            bind=engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        observed_now = datetime.now(UTC)
        clock = lambda: observed_now
        demand = DemandServiceImpl(
            cast(
                Any,
                lambda requested: SqlAlchemyDemandUnitOfWork(
                    factory,
                    requested,
                    now=clock,
                ),
            ),
            now=clock,
        )
        sourcing = SourcingServiceImpl(
            cast(
                Any,
                lambda requested: SqlAlchemySourcingUnitOfWork(factory, requested),
            ),
            Phase2SourcingAuthorizer(tenant_id),
            _UnavailableCandidateEvidenceReader(),
            now=clock,
        )
        return await execute_backfill(
            tenant_id=tenant_id,
            apply=apply,
            inventory=PostgresHistoricalCaseInventory(factory, tenant_id),
            demand=demand,
            sourcing=sourcing,
            now=clock,
        )
    finally:
        await engine.dispose()


class _ArgumentParser(argparse.ArgumentParser):
    """拒绝参数时不回显用户输入。"""

    def error(self, message: str) -> Never:
        del message
        raise ValueError("历史准入回填参数无效")


def _failure_output(
    *,
    status: Literal["not_run", "unknown"],
    reason: Literal["configuration_or_input_invalid", "execution_status_unknown"],
) -> dict[str, object]:
    return {
        "status": status,
        "mode": "unknown",
        "reason": reason,
        "counts": {
            "total": 0,
            "would_create": 0,
            "applied": 0,
            "skipped": 0,
            "unknown": 0,
        },
        "results": [],
    }


def _arguments(
    environment: Mapping[str, str], argv: Sequence[str] | None
) -> tuple[str, TenantId, bool]:
    parser = _ArgumentParser(add_help=False)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--tenant-id")
    options = parser.parse_args(argv)
    if options.apply and options.dry_run:
        raise ValidationError("历史准入回填模式冲突")
    if options.apply and options.tenant_id is None:
        raise ValidationError("apply 必须显式 tenant")
    raw_tenant = options.tenant_id
    if raw_tenant is None:
        raw_tenant = environment.get("TRADEOS_TENANT_ID")
    tenant_id = TenantId(_require_safe_id(raw_tenant))
    database_url = environment.get("DATABASE_URL")
    if not isinstance(database_url, str) or not database_url:
        raise ValidationError("数据库配置无效")
    return database_url, tenant_id, bool(options.apply)


BackfillRunner = Callable[[str, TenantId, bool], Any]


def main(
    environ: Mapping[str, str] | None = None,
    argv: Sequence[str] | None = None,
    *,
    runner: BackfillRunner | None = None,
) -> int:
    """默认只预演；所有输入和底层错误只输出固定 JSON 分类。"""
    environment = os.environ if environ is None else environ
    try:
        if not isinstance(environment, Mapping):
            raise ValidationError("环境配置无效")
        database_url, tenant_id, apply = _arguments(environment, argv)
    except Exception:  # noqa: BLE001 - 参数、环境及 argparse 原文均不得回显。
        print(
            json.dumps(
                _failure_output(
                    status="not_run", reason="configuration_or_input_invalid"
                ),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return 2

    try:
        selected_runner = runner or run_database_backfill
        report = asyncio.run(selected_runner(database_url, tenant_id, apply))
        if not isinstance(report, BackfillReport):
            raise ValidationError("历史准入回填报告无效")
    except Exception:  # noqa: BLE001 - DSN、异常、快照与 Provenance 都不得穿透。
        print(
            json.dumps(
                _failure_output(status="unknown", reason="execution_status_unknown"),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return 3

    print(
        json.dumps(
            report.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return 3 if report.status == "unknown" else 0


if __name__ == "__main__":
    raise SystemExit(main())
