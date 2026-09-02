"""历史 Sourcing Case 准入回填命令的安全边界。"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from domains.demand.schemas import NeedClusterPriorityFacts
from domains.sourcing.schemas import (
    NeedFact,
    SourcingNeedSnapshot,
    canonical_sourcing_need_snapshot_hash,
)
from scripts.backfill_sourcing_admissions import (
    BackfillReport,
    HistoricalSourcingCase,
    execute_backfill,
    main,
)
from shared.errors import ValidationError
from shared.schemas.identifiers import EmployeeId, TenantId, ValidatedNeedId
from shared.schemas.provenance import ProvenanceSummary, SourceType

TENANT = TenantId("tenant-backfill")
OTHER_TENANT = TenantId("tenant-other")
NOW = datetime(2026, 9, 3, 1, tzinfo=UTC)


def _snapshot(need_id: str) -> SourcingNeedSnapshot:
    provenance = ProvenanceSummary(
        source_type=SourceType.CONVERSATION,
        source_id="msg-sensitive-provenance",
        extracted_by="human",
        extracted_at=NOW - timedelta(days=4),
        confirmed_by=EmployeeId("emp-backfill"),
        confirmed_at=NOW - timedelta(days=4),
    )
    draft = SourcingNeedSnapshot(
        need_id=ValidatedNeedId(need_id),
        completeness=3,
        derivation_version="need-completeness-v1",
        product_category=NeedFact(
            value="Industrial hinges secret snapshot text",
            provenance=provenance,
        ),
        quantity=NeedFact(value=5000, provenance=provenance),
        snapshot_hash="0" * 64,
    )
    return draft.model_copy(
        update={"snapshot_hash": canonical_sourcing_need_snapshot_hash(draft)}
    )


def _case(
    case_id: str,
    *,
    tenant_id: TenantId = TENANT,
    state: str = "opened",
    has_workflow_run: bool = False,
    has_admission: bool = False,
    opened_at: object = NOW - timedelta(days=3),
    need_snapshot: object | None = None,
) -> HistoricalSourcingCase:
    need_id = f"need-{case_id}"
    snapshot = _snapshot(need_id) if need_snapshot is None else need_snapshot
    return HistoricalSourcingCase(
        tenant_id=tenant_id,
        case_id=case_id,
        need_id=need_id,
        workflow_version=2,
        state=state,
        opened_at=opened_at,
        need_snapshot=snapshot.model_dump(mode="json")
        if isinstance(snapshot, SourcingNeedSnapshot)
        else snapshot,
        need_snapshot_hash=(
            snapshot.snapshot_hash
            if isinstance(snapshot, SourcingNeedSnapshot)
            else "a" * 64
        ),
        has_workflow_run=has_workflow_run,
        has_admission=has_admission,
    )


class _Inventory:
    def __init__(self, rows: list[HistoricalSourcingCase]) -> None:
        self.rows = rows
        self.calls: list[TenantId] = []

    async def list_v2_cases(
        self, tenant_id: TenantId
    ) -> list[HistoricalSourcingCase]:
        self.calls.append(tenant_id)
        return list(self.rows)


class _Demand:
    def __init__(
        self,
        *,
        error_for: set[str] | None = None,
        unexpected_error_for: set[str] | None = None,
    ) -> None:
        self.error_for = error_for or set()
        self.unexpected_error_for = unexpected_error_for or set()
        self.calls: list[tuple[TenantId, ValidatedNeedId]] = []

    async def get_cluster_priority_facts(
        self, tenant_id: TenantId, need_id: ValidatedNeedId
    ) -> NeedClusterPriorityFacts:
        self.calls.append((tenant_id, need_id))
        if str(need_id) in self.unexpected_error_for:
            raise RuntimeError("postgresql://secret@db/prod priority read traceback")
        if str(need_id) in self.error_for:
            raise ValidationError("secret priority facts exception")
        return NeedClusterPriorityFacts(
            need_id=str(need_id),
            cluster_id=None,
            cluster_member_count=1,
            facts_observed_at=NOW - timedelta(days=2),
        )


class _Sourcing:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[object, ...]] = []

    async def enqueue_admission(
        self,
        tenant_id,
        case_id,
        need_id,
        *,
        ready_at,
        command,
        actor,
    ):
        self.calls.append(
            (tenant_id, case_id, need_id, ready_at, command, actor)
        )
        if self.error is not None:
            raise self.error
        return f"sad-{case_id}"


@pytest.mark.asyncio
async def test_default_dry_run_reports_eligible_without_writing() -> None:
    """把 dry-run 分支误接到 enqueue 会造成未授权历史写入。"""

    inventory = _Inventory([_case("src-eligible")])
    demand = _Demand()
    sourcing = _Sourcing()

    report = await execute_backfill(
        tenant_id=TENANT,
        apply=False,
        inventory=inventory,
        demand=demand,
        sourcing=sourcing,
        now=lambda: NOW,
    )

    assert report.to_dict() == {
        "status": "completed",
        "mode": "dry_run",
        "counts": {
            "total": 1,
            "would_create": 1,
            "applied": 0,
            "skipped": 0,
            "unknown": 0,
        },
        "results": [
            {
                "case_id": "src-eligible",
                "status": "would_create",
                "reason": "eligible",
            }
        ],
    }
    assert sourcing.calls == []


@pytest.mark.asyncio
async def test_apply_writes_only_eligible_owner_tenant_case() -> None:
    """过滤顺序或 tenant 校验缺失会给外租户、旧状态或已有记录重复回填。"""

    malformed = _snapshot("need-src-malformed").model_dump(mode="json")
    malformed["product_category"]["value"] = "tampered"
    rows = [
        _case("src-terminal", state="failed"),
        _case("src-owner"),
        _case("src-started", state="discovering"),
        _case("src-run", has_workflow_run=True),
        _case("src-admitted", has_admission=True),
        _case("src-malformed", need_snapshot=malformed),
        _case("src-foreign", tenant_id=OTHER_TENANT),
    ]
    inventory = _Inventory(rows)
    sourcing = _Sourcing()

    report = await execute_backfill(
        tenant_id=TENANT,
        apply=True,
        inventory=inventory,
        demand=_Demand(),
        sourcing=sourcing,
        now=lambda: NOW,
    )

    assert [item.case_id for item in report.results] == sorted(
        item.case_id for item in report.results
    )
    assert [(item.status, item.reason) for item in report.results] == [
        ("skipped", "already_admitted"),
        ("skipped", "tenant_mismatch"),
        ("skipped", "malformed_need_snapshot"),
        ("applied", "admission_ensured"),
        ("skipped", "workflow_run_exists"),
        ("skipped", "case_already_started"),
        ("skipped", "case_terminal"),
    ]
    assert report.counts == {
        "total": 7,
        "would_create": 0,
        "applied": 1,
        "skipped": 6,
        "unknown": 0,
    }
    assert len(sourcing.calls) == 1
    tenant_id, case_id, need_id, ready_at, command, actor = sourcing.calls[0]
    assert (tenant_id, str(case_id), str(need_id), ready_at) == (
        TENANT,
        "src-owner",
        "need-src-owner",
        NOW - timedelta(days=3),
    )
    assert command.facts is not None
    assert command.facts.need_id == ValidatedNeedId("need-src-owner")
    assert actor.tenant_id == TENANT
    assert actor.role == "system"


@pytest.mark.asyncio
async def test_invalid_opened_at_and_priority_facts_never_write() -> None:
    """不可核验 ready_at 或 Demand 事实不能被猜测成可准入记录。"""

    rows = [
        _case("src-naive", opened_at=NOW.replace(tzinfo=None)),
        _case("src-future", opened_at=NOW + timedelta(seconds=1)),
        _case("src-facts"),
    ]
    sourcing = _Sourcing()
    report = await execute_backfill(
        tenant_id=TENANT,
        apply=True,
        inventory=_Inventory(rows),
        demand=_Demand(error_for={"need-src-facts"}),
        sourcing=sourcing,
        now=lambda: NOW,
    )

    assert [(item.case_id, item.reason) for item in report.results] == [
        ("src-facts", "priority_facts_invalid"),
        ("src-future", "invalid_opened_at"),
        ("src-naive", "invalid_opened_at"),
    ]
    assert sourcing.calls == []


@pytest.mark.asyncio
async def test_unknown_write_stops_and_never_echoes_exception() -> None:
    """不确定写入若继续处理或回显异常，会扩大未知状态并泄漏底层信息。"""

    report = await execute_backfill(
        tenant_id=TENANT,
        apply=True,
        inventory=_Inventory([_case("src-a"), _case("src-b")]),
        demand=_Demand(),
        sourcing=_Sourcing(
            error=RuntimeError(
                "postgresql://secret-user:secret-pass@db/prod provenance raw"
            )
        ),
        now=lambda: NOW,
    )

    rendered = json.dumps(report.to_dict(), ensure_ascii=False)
    assert report.status == "unknown"
    assert [(item.case_id, item.status, item.reason) for item in report.results] == [
        ("src-a", "unknown", "admission_write_unknown")
    ]
    assert "secret-user" not in rendered
    assert "secret-pass" not in rendered
    assert "provenance raw" not in rendered


@pytest.mark.asyncio
async def test_unknown_priority_read_stops_instead_of_claiming_facts_invalid() -> None:
    """存储故障若伪装成坏事实并以 exit 0 结束，会误导运维继续 apply。"""

    report = await execute_backfill(
        tenant_id=TENANT,
        apply=True,
        inventory=_Inventory([_case("src-a"), _case("src-b")]),
        demand=_Demand(unexpected_error_for={"need-src-a"}),
        sourcing=_Sourcing(),
        now=lambda: NOW,
    )

    rendered = json.dumps(report.to_dict(), ensure_ascii=False)
    assert report.status == "unknown"
    assert [(item.case_id, item.status, item.reason) for item in report.results] == [
        ("src-a", "unknown", "priority_facts_read_unknown")
    ]
    assert "secret@db" not in rendered
    assert "traceback" not in rendered


def test_apply_requires_explicit_exact_tenant_before_runner_io(capsys) -> None:
    """apply 若回退环境租户，会让一个遗漏参数的命令产生真实写入。"""

    calls: list[object] = []

    async def runner(database_url, tenant_id, apply):
        calls.append((database_url, tenant_id, apply))
        return BackfillReport.empty(mode="apply")

    exit_code = main(
        {
            "DATABASE_URL": "postgresql+asyncpg://secret-user:secret-pass@db/prod",
            "TRADEOS_TENANT_ID": str(TENANT),
        },
        ["--apply"],
        runner=runner,
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert calls == []
    assert output == {
        "status": "not_run",
        "mode": "unknown",
        "reason": "configuration_or_input_invalid",
        "counts": {
            "total": 0,
            "would_create": 0,
            "applied": 0,
            "skipped": 0,
            "unknown": 0,
        },
        "results": [],
    }


def test_no_arguments_is_dry_run_and_stdout_is_safe_json(capsys) -> None:
    """默认模式不能写，且 stdout 不能混入 DSN、快照、Provenance 或异常文本。"""

    async def runner(database_url, tenant_id, apply):
        assert database_url.endswith("/prod")
        assert tenant_id == TENANT
        assert apply is False
        return BackfillReport.from_results(
            mode="dry_run",
            results=(
                {
                    "case_id": "src-safe",
                    "status": "would_create",
                    "reason": "eligible",
                },
            ),
        )

    exit_code = main(
        {
            "DATABASE_URL": "postgresql+asyncpg://secret-user:secret-pass@db/prod",
            "TRADEOS_TENANT_ID": str(TENANT),
            "UNRELATED_SECRET": "raw provenance secret snapshot text",
        },
        [],
        runner=runner,
    )

    raw = capsys.readouterr().out
    output = json.loads(raw)
    assert exit_code == 0
    assert output["mode"] == "dry_run"
    assert output["results"] == [
        {
            "case_id": "src-safe",
            "status": "would_create",
            "reason": "eligible",
        }
    ]
    lowered = raw.casefold()
    for secret in (
        "secret-user",
        "secret-pass",
        "provenance",
        "snapshot text",
        "unrelated_secret",
    ):
        assert secret not in lowered


def test_cli_execution_failure_is_fixed_json_and_exit_three(capsys) -> None:
    """数据库/组合异常不得穿透为 traceback、DSN 或任意异常原文。"""

    async def runner(database_url, tenant_id, apply):
        raise RuntimeError(
            f"{database_url} {tenant_id} secret snapshot provenance traceback"
        )

    exit_code = main(
        {
            "DATABASE_URL": "postgresql+asyncpg://secret-user:secret-pass@db/prod",
            "TRADEOS_TENANT_ID": str(TENANT),
        },
        ["--dry-run"],
        runner=runner,
    )

    raw = capsys.readouterr().out
    output = json.loads(raw)
    assert exit_code == 3
    assert output["status"] == "unknown"
    assert output["reason"] == "execution_status_unknown"
    assert output["results"] == []
    for secret in ("secret-user", "secret-pass", "snapshot", "provenance", "traceback"):
        assert secret not in raw.casefold()


def test_bad_tenant_argument_is_rejected_without_echo(capsys) -> None:
    """控制字符或首尾空白租户不能进入查询，也不能从 argparse 回显。"""

    exit_code = main(
        {"DATABASE_URL": "postgresql+asyncpg://hidden@db/prod"},
        ["--apply", "--tenant-id", " tenant-secret\n"],
    )

    raw = capsys.readouterr().out
    assert exit_code == 2
    assert json.loads(raw)["reason"] == "configuration_or_input_invalid"
    assert "tenant-secret" not in raw


@pytest.mark.asyncio
async def test_snapshot_hash_drift_is_malformed_without_sensitive_output() -> None:
    """只校验 Pydantic 形状会漏过正文或三方 hash 漂移。"""

    row = _case("src-hash")
    row = replace(row, need_snapshot_hash="b" * 64)
    report = await execute_backfill(
        tenant_id=TENANT,
        apply=True,
        inventory=_Inventory([row]),
        demand=_Demand(),
        sourcing=_Sourcing(),
        now=lambda: NOW,
    )

    assert report.to_dict()["results"] == [
        {
            "case_id": "src-hash",
            "status": "skipped",
            "reason": "malformed_need_snapshot",
        }
    ]
    assert "Industrial hinges" not in json.dumps(report.to_dict())
