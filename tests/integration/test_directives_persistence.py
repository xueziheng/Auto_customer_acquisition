"""Sourcing admission Directive 的 PostgreSQL 往返与历史 JSON 兼容。"""

from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from domains.directives.schemas import SourcingAdmissionConfigInput
from domains.directives.service_impl import DirectiveServiceImpl
from infra.db.directive_uow import SqlAlchemyDirectiveUnitOfWork
from shared.errors import InvalidStateTransition
from shared.schemas.identifiers import EmployeeId, TenantId

NOW = datetime(2026, 9, 2, 12, tzinfo=UTC)
BOSS = EmployeeId("emp_directive_boss")


class _Employees:
    async def is_active_boss(
        self, tenant_id: TenantId, employee_id: EmployeeId
    ) -> bool:
        return employee_id == BOSS

    async def names_for(
        self, tenant_id: TenantId, employee_ids: tuple[EmployeeId, ...]
    ) -> dict[EmployeeId, str]:
        return {employee_id: str(employee_id) for employee_id in employee_ids}


@pytest_asyncio.fixture
async def directive_persistence(
    db_url: str,
) -> AsyncIterator[tuple[DirectiveServiceImpl, AsyncEngine]]:
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    service = DirectiveServiceImpl(
        lambda tenant_id: SqlAlchemyDirectiveUnitOfWork(
            factory, tenant_id, now=lambda: NOW
        ),
        _Employees(),
        now=lambda: NOW,
    )
    try:
        yield service, engine
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "DELETE FROM boss_directives WHERE tenant_id IN "
                    "('tn_directive_new_policy','tn_directive_legacy_json',"
                    "'tn_directive_concurrent_stale')"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE directive_versions DISABLE TRIGGER trg_directive_versions_guard"
                )
            )
            await connection.execute(
                text(
                    "DELETE FROM directive_versions WHERE tenant_id IN "
                    "('tn_directive_new_policy','tn_directive_legacy_json',"
                    "'tn_directive_concurrent_stale')"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE directive_versions ENABLE TRIGGER trg_directive_versions_guard"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE directive_proposals DISABLE TRIGGER trg_directive_proposals_guard"
                )
            )
            await connection.execute(
                text(
                    "DELETE FROM directive_proposals WHERE tenant_id IN "
                    "('tn_directive_new_policy','tn_directive_legacy_json',"
                    "'tn_directive_concurrent_stale')"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE directive_proposals ENABLE TRIGGER trg_directive_proposals_guard"
                )
            )
            await connection.execute(
                text(
                    "DELETE FROM outbox_deliveries "
                    "WHERE tenant_id = 'tn_directive_concurrent_stale'"
                )
            )
            await connection.execute(
                text(
                    "DELETE FROM outbox_events "
                    "WHERE tenant_id = 'tn_directive_concurrent_stale'"
                )
            )
        await engine.dispose()


async def test_new_sourcing_admission_directive_round_trips_and_rollback_versions(
    directive_persistence: tuple[DirectiveServiceImpl, AsyncEngine],
) -> None:
    service, _engine = directive_persistence
    tenant = TenantId("tn_directive_new_policy")
    proposal_id = await service.submit_sourcing_admission_proposal(
        tenant,
        "Enable bounded cluster-ranked sourcing.",
        SourcingAdmissionConfigInput(
            mode="cluster_ranked",
            automatic_admission_enabled=True,
            batch_limit=3,
        ),
        "Enable cluster-ranked sourcing admission.",
        ["Up to three waiting cases may be admitted per cycle."],
        "directive-parser-v1",
    )
    await service.confirm_proposal(tenant, proposal_id, BOSS)

    active = await service.get_active(tenant)
    assert active is not None
    assert active.version == 1
    assert active.sourcing_admission_mode == "cluster_ranked"
    assert active.automatic_sourcing_admission_enabled is True
    assert active.sourcing_admission_batch_limit == 3

    await service.rollback_to_version(tenant, 1, BOSS)
    rolled_back = await service.get_active(tenant)
    assert rolled_back is not None
    assert rolled_back.version == 2
    assert rolled_back.rollback_of_version == 1
    assert rolled_back.sourcing_admission_batch_limit == 3


async def test_old_directive_json_without_sourcing_section_remains_readable(
    directive_persistence: tuple[DirectiveServiceImpl, AsyncEngine],
) -> None:
    service, engine = directive_persistence
    tenant = TenantId("tn_directive_legacy_json")
    old_content = {
        "objective": "focus_existing_needs",
        "market_assignments": [],
        "discovery": None,
        "demand_discovery": None,
        "outreach": None,
        "handoff": None,
        "paused_markets": [],
        "monthly_budget_credits": None,
        "notes": None,
    }
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO directive_proposals "
                "(tenant_id,proposal_id,raw_text,parsed_content,"
                "interpretation_summary,expected_behavior_changes,parsed_by,state,"
                "created_at,decided_at,decided_by,base_directive_version) VALUES "
                "(:tenant,'dpr_legacy','Legacy directive',CAST(:content AS jsonb),"
                "'Legacy interpretation',CAST(:changes AS jsonb),'parser-v0',"
                "'confirmed',:now,:now,:boss,NULL)"
            ),
            {
                "tenant": tenant,
                "content": json.dumps(old_content),
                "changes": json.dumps(["Legacy behavior change."]),
                "now": NOW,
                "boss": BOSS,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO directive_versions "
                "(tenant_id,directive_id,version,content,source_proposal_id,"
                "activated_at,activated_by,superseded_at,rollback_of) VALUES "
                "(:tenant,'dir_legacy',1,CAST(:content AS jsonb),'dpr_legacy',"
                ":now,:boss,NULL,NULL)"
            ),
            {
                "tenant": tenant,
                "content": json.dumps(old_content),
                "now": NOW,
                "boss": BOSS,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO boss_directives "
                "(tenant_id,directive_id,version,activated_at) VALUES "
                "(:tenant,'dir_legacy',1,:now)"
            ),
            {"tenant": tenant, "now": NOW},
        )

    proposal = await service.get_proposal(tenant, "dpr_legacy")
    active = await service.get_active(tenant)
    assert active is not None
    for view in (proposal, active):
        assert view.sourcing_admission_mode is None
        assert view.automatic_sourcing_admission_enabled is None
        assert view.sourcing_admission_batch_limit is None


async def test_concurrent_generic_confirmations_allow_one_matching_baseline_only(
    directive_persistence: tuple[DirectiveServiceImpl, AsyncEngine],
) -> None:
    service, engine = directive_persistence
    tenant = TenantId("tn_directive_concurrent_stale")
    initial_proposal = await service.submit_sourcing_admission_proposal(
        tenant,
        "Enable bounded cluster-ranked sourcing.",
        SourcingAdmissionConfigInput(
            mode="cluster_ranked",
            automatic_admission_enabled=True,
            batch_limit=3,
        ),
        "Enable cluster-ranked sourcing admission.",
        ["Up to three waiting cases may be admitted per cycle."],
        "directive-parser-v1",
    )
    await service.confirm_proposal(tenant, initial_proposal, BOSS)

    models = importlib.import_module("domains.directives.models")
    first_content = models.DirectiveContent(
        objective=models.DirectiveObjective.FOCUS_EXISTING_NEEDS,
        monthly_budget_credits=1200,
        notes="First concurrent generic proposal.",
    )
    second_content = models.DirectiveContent(
        objective=models.DirectiveObjective.FOCUS_EXISTING_NEEDS,
        monthly_budget_credits=1500,
        notes="Second concurrent generic proposal.",
    )
    first_proposal = await service.submit_proposal(
        tenant,
        "Set the monthly budget to 1200 credits.",
        first_content,
        "Only the ordinary Directive fields change.",
        ["Monthly budget becomes 1200 credits."],
        "directive-parser-v1",
    )
    second_proposal = await service.submit_proposal(
        tenant,
        "Set the monthly budget to 1500 credits.",
        second_content,
        "Only the ordinary Directive fields change.",
        ["Monthly budget becomes 1500 credits."],
        "directive-parser-v1",
    )

    results = await asyncio.gather(
        service.confirm_proposal(tenant, first_proposal, BOSS),
        service.confirm_proposal(tenant, second_proposal, BOSS),
        return_exceptions=True,
    )

    failures = [result for result in results if isinstance(result, BaseException)]
    successes = [result for result in results if not isinstance(result, BaseException)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], InvalidStateTransition)
    assert "陈旧" in str(failures[0])

    async with engine.connect() as connection:
        versions = (
            await connection.execute(
                text(
                    "SELECT version, content FROM directive_versions "
                    "WHERE tenant_id=:tenant ORDER BY version"
                ),
                {"tenant": tenant},
            )
        ).mappings().all()
        proposal_states = (
            await connection.execute(
                text(
                    "SELECT proposal_id, state, base_directive_version, parsed_content "
                    "FROM directive_proposals WHERE tenant_id=:tenant "
                    "AND proposal_id IN (:first,:second) ORDER BY proposal_id"
                ),
                {
                    "tenant": tenant,
                    "first": first_proposal,
                    "second": second_proposal,
                },
            )
        ).mappings().all()
        active = (
            await connection.execute(
                text(
                    "SELECT directive_id, version FROM boss_directives "
                    "WHERE tenant_id=:tenant"
                ),
                {"tenant": tenant},
            )
        ).mappings().one()
        event_count = await connection.scalar(
            text(
                "SELECT count(*) FROM outbox_events "
                "WHERE tenant_id=:tenant AND event_type='DirectiveActivated'"
            ),
            {"tenant": tenant},
        )

    assert [row["version"] for row in versions] == [1, 2]
    assert active["version"] == 2
    assert sorted(row["state"] for row in proposal_states) == [
        "confirmed",
        "pending_confirmation",
    ]
    assert {row["base_directive_version"] for row in proposal_states} == {1}
    assert all(
        row["parsed_content"]["sourcing_admission"]
        == {
            "mode": "cluster_ranked",
            "automatic_admission_enabled": True,
            "batch_limit": 3,
        }
        for row in proposal_states
    )
    assert versions[-1]["content"]["sourcing_admission"] == {
        "mode": "cluster_ranked",
        "automatic_admission_enabled": True,
        "batch_limit": 3,
    }
    assert versions[-1]["content"]["notes"] in {
        "First concurrent generic proposal.",
        "Second concurrent generic proposal.",
    }
    assert event_count == 2
