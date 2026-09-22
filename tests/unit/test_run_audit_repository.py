"""Run 审计仓储只执行显式 tenant-bound 查询并裁剪敏感列。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Self

from infra.db.run_audit import PostgresRunAuditRepository
from shared.schemas.identifiers import RunId, TenantId

TENANT = TenantId("tn_01K39P9M5D6K4A91YEQ80EJZ0X")
RUN_ID = RunId("run_01K39P9M5D6K4A91YEQ80EJZ0X")
NOW = datetime(2026, 8, 23, 9, tzinfo=UTC)


class _Result:
    def __init__(self, values: list[Any], *, scalar: bool = False) -> None:
        self._values = values
        self._scalar = scalar

    def all(self) -> list[Any]:
        return self._values

    def first(self) -> Any | None:
        return self._values[0] if self._values else None

    def scalars(self) -> _Result:
        return _Result(self._values, scalar=True)


class _Session:
    def __init__(self, results: list[_Result]) -> None:
        self._results = results
        self.tenant_bound_statements = 0

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def execute(self, statement: Any) -> _Result:
        compiled = statement.compile()
        assert str(TENANT) in compiled.params.values()
        assert "tenant_id" in str(compiled)
        self.tenant_bound_statements += 1
        return self._results.pop(0)


def _run_row() -> SimpleNamespace:
    return SimpleNamespace(
        run_id=str(RUN_ID),
        tenant_id=str(TENANT),
        workflow_type="demand_discovery",
        workflow_version=1,
        subject_ref="schedule:daily",
        current_step="discover",
        status="running",
        created_at=NOW,
        last_activity_at=NOW,
        next_poll_at=None,
        retry_count=0,
        context={"customer_email": "must-not-leak@example.test"},
        last_error=None,
    )


def test_list_runs_maps_only_safe_summary_and_binds_tenant() -> None:
    session = _Session([_Result([_run_row()])])
    repository = PostgresRunAuditRepository(lambda: session)

    result = asyncio.run(
        repository.list_runs(
            TENANT,
            workflow_type="demand_discovery",
            status="running",
            limit=20,
        )
    )

    assert result[0].run_id == RUN_ID
    assert "context" not in result[0].model_dump()
    assert "must-not-leak" not in result[0].model_dump_json()
    assert session.tenant_bound_statements == 1


def test_get_run_queries_every_audit_source_with_tenant_and_omits_payload_columns() -> None:
    step = SimpleNamespace(
        step_id="wfs_01K39P9M5D6K4A91YEQ80EJZ0X",
        step_name="discover",
        status="completed",
        data={"page_body": "must-not-leak"},
        attempt=1,
        error=None,
        due_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    tool_call = SimpleNamespace(
        tool_call_id="tc_01K39P9M5D6K4A91YEQ80EJZ0X",
        tool_id="web.search",
        tool_version="1",
        risk_level="low",
        cost_class="low",
        status="succeeded",
        attempt_count=1,
        error_category=None,
        created_at=NOW,
        completed_at=NOW,
        provider_ref="must-not-leak",
    )
    artifact = SimpleNamespace(
        artifact_id="art_01K39P9M5D6K4A91YEQ80EJZ0X",
        kind="email_draft",
        mime_type="application/vnd.tradeos.email-draft+json",
        subject_ref="enr_01K39P9M5D6K4A91YEQ80EJZ0X",
        generated_by="outreach-agent",
        generated_at=NOW,
        object_key="must-not-leak",
    )
    approval = SimpleNamespace(
        approval_id="apr_01K39P9M5D6K4A91YEQ80EJZ0X",
        approval_type="campaign_activation",
        state="approved",
        created_at=NOW,
        expires_at=NOW,
        decided_at=NOW,
        proposed_change={"secret": "must-not-leak"},
        decision_note="must-not-leak",
    )
    session = _Session([
        _Result([_run_row()]),
        _Result([step]),
        _Result([tool_call]),
        _Result([artifact]),
        _Result([approval]),
        _Result([]),
    ])
    repository = PostgresRunAuditRepository(lambda: session)

    result = asyncio.run(repository.get_run(TENANT, RUN_ID))

    assert result is not None
    serialized = result.model_dump_json()
    assert result.steps[0].step_name == "discover"
    assert result.tool_calls[0].tool_id == "web.search"
    assert result.artifacts[0].kind == "email_draft"
    assert result.approvals[0].state == "approved"
    assert "must-not-leak" not in serialized
    assert session.tenant_bound_statements == 6
