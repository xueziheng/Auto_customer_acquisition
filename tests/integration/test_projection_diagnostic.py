"""诊断与来源同事务绑定租约；保存统计来自实际SQL分支。"""

from datetime import timedelta

import pytest
from sqlalchemy import select

from tests.integration.test_prospect_inventory import NOW
from tests.integration.test_prospect_inventory import (
    inventory as inventory,  # noqa: PLC0414
)
from tests.unit.test_prospect_inventory import candidate


async def test_save_report_and_source_diagnostic_are_fenced(inventory):
    from domains.prospecting.inventory import InventoryQuery
    from domains.prospecting.projection_diagnostic import (
        ProjectionSummary,
        SourceDiagnostic,
    )
    from infra.db.prospect_inventory_tables import AcquisitionAttemptRow

    service, _, factory, tenant, _ = inventory
    query = InventoryQuery(product_tags=("hinges",))
    await service.request(tenant, "emp", query, 1, "diag")
    job = await service.claim_job(tenant, timedelta(minutes=10))
    first = await service.save_collected(
        tenant, job.id, job.lease_token, (candidate(),)
    )
    assert first.inserted_fresh == 1 and first.query_match == 1 and first.fresh == 1
    second = await service.save_collected(
        tenant, job.id, job.lease_token, (candidate(),)
    )
    assert second.existing_refreshed == 1 and second.inserted_fresh == 0
    diagnostic = SourceDiagnostic(
        projection=ProjectionSummary(
            input_shape="valid",
            raw_count=1,
            projection_complete=True,
            projected_count=1,
        ),
        save=second,
    )
    await service.record_source_attempt(
        tenant,
        job.id,
        job.lease_token,
        "company_websites",
        "complete",
        0,
        diagnostic=diagnostic,
    )
    with pytest.raises(ValueError):
        await service.record_source_attempt(
            tenant,
            job.id,
            "wrong",
            "company_websites",
            "complete",
            0,
            diagnostic=diagnostic,
        )
    async with factory() as db:
        rows = (
            await db.scalars(
                select(AcquisitionAttemptRow).where(
                    AcquisitionAttemptRow.tenant_id == tenant,
                    AcquisitionAttemptRow.job_id == job.id,
                )
            )
        ).all()
    assert len(rows) == 1 and rows[0].diagnostic == diagnostic.model_dump(mode="json")
    assert "acme" not in str(rows[0].diagnostic)


async def test_save_report_separates_time_query_and_existing_older(inventory):
    from domains.prospecting.inventory import InventoryQuery

    _, catalog, _, _, _ = inventory
    values = (
        candidate(),
        candidate(
            id="pci_expired",
            canonical_domain="expired.example",
            expires_at=NOW - timedelta(hours=1),
        ),
        candidate(
            id="pci_future",
            canonical_domain="future.example",
            observed_at=NOW + timedelta(days=1),
            expires_at=NOW + timedelta(days=2),
        ),
    )
    report = await catalog.save_public_report(
        values, InventoryQuery(country="KE", product_tags=("PRIVATE",))
    )
    assert (
        report.total_count == 3
        and report.inserted_fresh == 1
        and report.inserted_not_fresh == 2
    )
    assert (
        report.query_mismatch == 3
        and report.expired == 1
        and report.future_observed == 1
    )
    older = candidate(observed_at=NOW - timedelta(days=2))
    report = await catalog.save_public_report(
        (older,), InventoryQuery(product_tags=("hinges",))
    )
    assert report.existing_unchanged == 1


async def test_legacy_null_and_strict_revalidation_before_persist(inventory):
    from sqlalchemy import text

    from domains.prospecting.inventory import InventoryQuery
    from domains.prospecting.projection_diagnostic import (
        ProjectionSummary,
        SourceDiagnostic,
    )

    service, _, factory, tenant, _ = inventory
    await service.request(
        tenant, "emp", InventoryQuery(product_tags=("hinges",)), 1, "legacy"
    )
    job = await service.claim_job(tenant, timedelta(minutes=10))
    await service.record_source_attempt(
        tenant, job.id, job.lease_token, "company_websites", "failed", 0
    )
    valid = SourceDiagnostic(projection=ProjectionSummary(input_shape="not_object"))
    corrupt = valid.model_copy(
        update={"projection": valid.projection.model_copy(update={"raw_count": True})}
    )
    with pytest.raises(ValueError):
        await service.record_source_attempt(
            tenant,
            job.id,
            job.lease_token,
            "company_websites",
            "failed",
            0,
            diagnostic=corrupt,
        )
    with pytest.raises(ValueError):
        await service.record_source_attempt(
            "different",
            job.id,
            job.lease_token,
            "company_websites",
            "failed",
            0,
            diagnostic=valid,
        )
    async with factory() as db:
        assert (
            await db.scalar(
                text(
                    "SELECT count(*) FROM acquisition_attempts WHERE tenant_id=:tenant AND job_id=:job AND diagnostic IS NULL"
                ),
                {"tenant": tenant, "job": job.id},
            )
            == 1
        )


async def test_failed_shape_summary_passes_real_driver_and_job_fence(inventory):
    from types import SimpleNamespace

    from apps.scheduler_worker.inventory_acquisition import InventoryAcquisitionDriver
    from domains.prospecting.inventory import InventoryQuery, SourceState
    from domains.prospecting.projection_diagnostic import (
        ProjectionDiagnosticError,
        ProjectionSummary,
        SourceDiagnostic,
    )
    from infra.db.prospect_inventory_tables import AcquisitionAttemptRow

    service, catalog, factory, tenant, _ = inventory
    await service.request(
        tenant, "emp", InventoryQuery(product_tags=("hinges",)), 1, "shape"
    )

    class Authority:
        async def current_scope(self, t, employee):
            return SimpleNamespace(
                tenant_id=t, employee_id=employee, allowed_owners={employee}
            )

    class Sources:
        async def list_sources(self, t):
            return (
                SourceState(
                    source_id="company_websites",
                    availability="available",
                    cost_class="free",
                    relevant=True,
                ),
            )

    class Collector:
        async def collect(self, job, source, guard, **kwargs):
            await guard()
            raise ProjectionDiagnosticError(
                ProjectionSummary(input_shape="candidates_not_list")
            )

    await InventoryAcquisitionDriver(
        tenant_id=tenant,
        backend=service,
        catalog=catalog,
        authority=Authority(),
        sources=Sources(),
        collector=Collector(),
        now=lambda: NOW,
    ).scan()
    async with factory() as db:
        rows = (
            await db.scalars(
                select(AcquisitionAttemptRow).where(
                    AcquisitionAttemptRow.tenant_id == tenant
                )
            )
        ).all()
    assert len(rows) == 1 and rows[0].outcome == "failed"
    summary = SourceDiagnostic.model_validate(rows[0].diagnostic)
    assert (
        summary.projection.input_shape == "candidates_not_list" and summary.save is None
    )


async def test_tool_usage_jsonb_roundtrip_and_old_missing_field_is_unknown(inventory):
    from sqlalchemy import update

    from domains.prospecting.inventory import InventoryQuery
    from domains.prospecting.projection_diagnostic import (
        ProjectionSummary,
        SaveSummary,
        SourceDiagnostic,
        ToolUsageSummary,
    )
    from infra.db.prospect_inventory_tables import AcquisitionAttemptRow

    service, _, factory, tenant, _ = inventory
    await service.request(
        tenant, "emp", InventoryQuery(product_tags=("hinges",)), 1, "usage"
    )
    job = await service.claim_job(tenant, timedelta(minutes=10))
    usage = ToolUsageSummary(
        tool_limit=7,
        reserved=4,
        source_search=3,
        read_public_page=1,
        terminal=4,
        succeeded=3,
        search_limit_reached=1,
    )
    diagnostic = SourceDiagnostic(
        projection=ProjectionSummary(
            input_shape="valid", raw_count=0, projection_complete=True, tool_usage=usage
        ),
        save=SaveSummary(total_count=0),
    )
    for source in ("company_websites", "dealer_networks"):
        await service.record_source_attempt(
            tenant,
            job.id,
            job.lease_token,
            source,
            "complete",
            0,
            diagnostic=diagnostic,
        )
    old = diagnostic.model_dump(mode="json")
    del old["projection"]["tool_usage"]
    async with factory() as db:
        await db.execute(
            update(AcquisitionAttemptRow)
            .where(
                AcquisitionAttemptRow.tenant_id == tenant,
                AcquisitionAttemptRow.job_id == job.id,
                AcquisitionAttemptRow.source_id == "dealer_networks",
            )
            .values(diagnostic=old)
        )
        await db.commit()
    async with factory() as db:
        rows = (
            await db.scalars(
                select(AcquisitionAttemptRow).where(
                    AcquisitionAttemptRow.tenant_id == tenant,
                    AcquisitionAttemptRow.job_id == job.id,
                )
            )
        ).all()
    parsed = {
        row.source_id: SourceDiagnostic.model_validate(row.diagnostic) for row in rows
    }
    assert parsed["company_websites"].projection.tool_usage == usage
    assert parsed["dealer_networks"].projection.tool_usage is None
    invalid = diagnostic.model_copy(
        update={
            "projection": diagnostic.projection.model_copy(
                update={"tool_usage": usage.model_copy(update={"reserved": True})}
            )
        }
    )
    with pytest.raises(ValueError):
        await service.record_source_attempt(
            tenant,
            job.id,
            job.lease_token,
            "company_news",
            "complete",
            0,
            diagnostic=invalid,
        )
