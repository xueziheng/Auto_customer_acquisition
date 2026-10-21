"""服务档位 CLI 的无副作用预览、CAS 参数和脱敏错误边界。"""

from __future__ import annotations

import importlib
import importlib.util
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest

from domains.prospecting.service import ServicePlanState
from shared.errors import InvalidStateTransition

NOW = datetime(2026, 10, 10, 8, tzinfo=UTC)
ARGS = [
    "assign",
    "--tenant-id",
    "tenant_cli",
    "--actor-id",
    "emp_admin",
    "--tier",
    "standard",
]


def _cli():
    assert (
        importlib.util.find_spec("scripts.manage_prospecting_service_plan") is not None
    )
    return importlib.import_module("scripts.manage_prospecting_service_plan")


def _state(**changes):
    data = {
        "tenant_id": "tenant_cli",
        "tier": "basic",
        "version": 4,
        "period_started_at": NOW - timedelta(days=1),
        "period_ends_at": NOW + timedelta(days=29),
        "candidates_reserved": 12,
        "searches_reserved": 24,
        "contacts_reserved": 3,
        "schedule_enabled": False,
        "proposal_id": "private_proposal",
        "acting_user_id": "private_actor",
    }
    data.update(changes)
    return ServicePlanState(**data)


class _Store:
    def __init__(self, state=None):
        self.state = state
        self.assigned = []
        self.reads = []
        self.error = None

    async def get(self, tenant_id):
        self.reads.append(tenant_id)
        return self.state

    async def assign(self, tenant_id, tier, **kwargs):
        self.assigned.append((tenant_id, tier, kwargs))
        if self.error:
            raise self.error
        self.state = _state(tenant_id=tenant_id, tier=tier)
        return self.state


def _factory(store, calls):
    @asynccontextmanager
    async def runtime():
        calls.append("opened")
        try:
            yield store
        finally:
            calls.append("closed")

    return runtime


def test_assign_defaults_to_dry_run_without_reading_database_environment(
    capsys, monkeypatch
):
    cli = _cli()

    def forbidden_runtime():
        raise AssertionError("预览不得打开数据库")

    monkeypatch.delenv("DATABASE_URL", raising=False)
    code = cli.main(ARGS, runtime_factory=forbidden_runtime, now=lambda: NOW)
    result = json.loads(capsys.readouterr().out)
    assert code == 0
    assert result["status"] == "dry_run"
    assert result["tier"] == "standard"
    assert result["expected_version"] is None
    assert result["policy"]["contact_limit"] == 120


@pytest.mark.parametrize("version", [None, 4])
def test_apply_passes_explicit_tenant_actor_and_expected_version(version, capsys):
    cli, store, calls = _cli(), _Store(), []
    args = (
        ARGS
        + ["--apply"]
        + ([] if version is None else ["--expected-version", str(version)])
    )
    assert cli.main(args, runtime_factory=_factory(store, calls), now=lambda: NOW) == 0
    assert store.assigned == [
        (
            "tenant_cli",
            "standard",
            {
                "actor_id": "emp_admin",
                "expected_version": version,
                "now": NOW,
            },
        )
    ]
    assert calls == ["opened", "closed"]
    assert json.loads(capsys.readouterr().out)["status"] == "applied"


def test_status_projects_only_safe_tenant_fields_and_does_not_assign(capsys):
    cli, store, calls = _cli(), _Store(_state()), []
    assert (
        cli.main(
            ["status", "--tenant-id", "tenant_cli"],
            runtime_factory=_factory(store, calls),
            now=lambda: NOW,
        )
        == 0
    )
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["status"] == "ok"
    assert result["plan"]["version"] == 4
    assert result["plan"]["contacts_reserved"] == 3
    assert "private_proposal" not in captured.out
    assert "private_actor" not in captured.out
    assert store.assigned == []
    assert store.reads == ["tenant_cli"]


def test_status_for_unassigned_tenant_is_explicit(capsys):
    cli = _cli()
    assert (
        cli.main(
            ["status", "--tenant-id", "tenant_cli"],
            runtime_factory=_factory(_Store(), []),
            now=lambda: NOW,
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["plan"] is None


@pytest.mark.parametrize(
    "args",
    [
        ARGS + ["--apply", "--dry-run"],
        ARGS + ["--expected-version", "0"],
        ARGS + ["--expected-version", "-1"],
        ["assign", "--tenant-id", "tenant_cli", "--tier", "standard", "--apply"],
        [
            "assign",
            "--tenant-id",
            "tenant_cli",
            "--actor-id",
            "emp_admin",
            "--tier",
            "premium",
        ],
        ["status", "--tenant-id", "tenant_cli", "--password", "synthetic_secret"],
        ["status", "--tenant-id", "postgresql://synthetic_secret@localhost/db"],
    ],
)
def test_invalid_arguments_never_echo_values_or_open_runtime(args, capsys):
    cli = _cli()

    def forbidden_runtime():
        raise AssertionError("参数错误不得打开数据库")

    assert cli.main(args, runtime_factory=forbidden_runtime) == 2
    captured = capsys.readouterr()
    assert json.loads(captured.err) == {
        "status": "not_run",
        "reason": "invalid_arguments",
    }
    assert "synthetic_secret" not in captured.out + captured.err


def test_apply_error_after_possible_commit_reports_unknown_without_details(capsys):
    cli, store, calls = _cli(), _Store(), []
    store.error = RuntimeError('postgresql://user:synthetic_secret' + '@db.invalid/database')
    assert (
        cli.main(
            ARGS + ["--apply"], runtime_factory=_factory(store, calls), now=lambda: NOW
        )
        == 3
    )
    captured = capsys.readouterr()
    assert json.loads(captured.err) == {
        "status": "unknown",
        "reason": "execution_status_unknown",
    }
    assert "synthetic_secret" not in captured.out + captured.err
    assert calls == ["opened", "closed"]


def test_known_version_conflict_is_safely_rejected(capsys):
    cli, store = _cli(), _Store()
    store.error = InvalidStateTransition("internal synthetic_secret")
    assert (
        cli.main(
            ARGS + ["--apply"], runtime_factory=_factory(store, []), now=lambda: NOW
        )
        == 3
    )
    captured = capsys.readouterr()
    assert json.loads(captured.err) == {
        "status": "not_run",
        "reason": "request_rejected",
    }
    assert "synthetic_secret" not in captured.err


def test_wrong_tenant_return_is_rejected_without_outputting_foreign_record(capsys):
    cli = _cli()
    assert (
        cli.main(
            ["status", "--tenant-id", "tenant_cli"],
            runtime_factory=_factory(_Store(_state(tenant_id="tenant_foreign")), []),
            now=lambda: NOW,
        )
        == 3
    )
    captured = capsys.readouterr()
    assert "tenant_foreign" not in captured.out + captured.err
    assert captured.out == ""


def test_schema_gate_precedes_store_construction_and_disposes_on_failure(
    monkeypatch, capsys
):
    cli = _cli()
    calls = []

    class Engine:
        async def dispose(self):
            calls.append("disposed")

    monkeypatch.setenv(
        "DATABASE_URL",
        'postgresql+asyncpg://fixture:synthetic_secret' + '@example.invalid/test',
    )
    monkeypatch.setattr(cli, "create_engine_from", lambda value: Engine())

    async def schema(engine):
        calls.append("schema_checked")
        raise cli.DatabaseSchemaError()

    monkeypatch.setattr(cli, "assert_database_schema_current", schema)

    def forbidden_store(factory):
        raise AssertionError("schema检查前不得构造store")

    monkeypatch.setattr(cli, "PostgresProspectingServicePlanStore", forbidden_store)
    assert cli.main(ARGS + ["--apply"], now=lambda: NOW) == 3
    captured = capsys.readouterr()
    assert calls == ["schema_checked", "disposed"]
    assert json.loads(captured.err) == {
        "status": "not_run",
        "reason": "schema_not_current",
    }
    assert "synthetic_secret" not in captured.out + captured.err
