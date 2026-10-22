"""手动入组迁移保留旧自动语义，拒绝把手动记录降级为自动发送。"""

import asyncio

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from infra.db.session import create_engine_from
from shared.schemas.identifiers import (
    ApprovalId,
    CampaignId,
    SendingIdentityId,
    TenantId,
    new_id,
)
from tests.integration.test_migrations import _run_alembic
from tests.integration.test_outreach_enrollment_lifecycle import _seed_active_campaign
from tests.migration_database_fixtures import (
    migration_database_url as migration_database_url,  # noqa: PLC0414
)


async def test_manual_enrollment_migration_preserves_legacy_and_rejects_lossy_downgrade(
    migration_database_url,
):
    _run_alembic(migration_database_url, "downgrade", "0075")
    engine = create_engine_from(migration_database_url)
    tenant, campaign, sender, approval = (
        TenantId(new_id("tn")),
        CampaignId(new_id("cmp")),
        SendingIdentityId(new_id("sid")),
        ApprovalId(new_id("apr")),
    )
    params = {
        "tenant": tenant,
        "campaign": campaign,
        "sender": sender,
        "enrollment": new_id("enr"),
        "account": new_id("acc"),
        "contact": new_id("cp"),
    }
    try:
        await _seed_active_campaign(
            async_sessionmaker(engine), tenant, campaign, (sender,), approval
        )
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO outreach_enrollments (tenant_id,enrollment_id,campaign_id,campaign_version,"
                    "account_id,contact_point_id,sending_identity_id,state,current_step,next_send_at,enrolled_at,"
                    "idempotency_key) VALUES (:tenant,:enrollment,:campaign,1,:account,:contact,:sender,"
                    "'enrolled',0,now(),now(),'legacy')"
                ),
                params,
            )
        _run_alembic(migration_database_url, "upgrade", "head")
        async with engine.connect() as conn:
            columns = await conn.run_sync(
                lambda c: inspect(c).get_columns("outreach_enrollments")
            )
            assert "execution_mode" in {c["name"] for c in columns}
            assert (
                await conn.scalar(
                    text(
                        "SELECT execution_mode FROM outreach_enrollments WHERE tenant_id=:tenant"
                    ),
                    params,
                )
                == "automatic"
            )
        async with engine.connect() as conn:
            with pytest.raises(IntegrityError):
                await conn.execute(
                    text(
                        "UPDATE outreach_enrollments SET execution_mode='unknown' WHERE tenant_id=:tenant"
                    ),
                    params,
                )
            await conn.rollback()
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO outreach_enrollments (tenant_id,enrollment_id,campaign_id,campaign_version,"
                    "account_id,contact_point_id,sending_identity_id,state,current_step,next_send_at,enrolled_at,"
                    "idempotency_key,execution_mode) VALUES (:tenant,:enrollment,:campaign,1,:account,:contact,"
                    ":sender,'enrolled',0,now(),now(),'manual','manual')"
                ),
                {**params, "enrollment": new_id("enr"), "account": new_id("acc")},
            )
        with pytest.raises(AssertionError):
            _run_alembic(migration_database_url, "downgrade", "0075")
        async with engine.begin() as conn:
            assert sorted(
                (
                    await conn.scalars(
                        text(
                            "SELECT execution_mode FROM outreach_enrollments WHERE tenant_id=:tenant"
                        ),
                        params,
                    )
                ).all()
            ) == ["automatic", "manual"]
            await conn.execute(
                text(
                    "DELETE FROM outreach_enrollments WHERE tenant_id=:tenant AND execution_mode='manual'"
                ),
                params,
            )
        _run_alembic(migration_database_url, "downgrade", "0075")
        _run_alembic(migration_database_url, "upgrade", "head")
        async with engine.connect() as conn:
            assert (
                await conn.scalar(
                    text(
                        "SELECT execution_mode FROM outreach_enrollments WHERE tenant_id=:tenant"
                    ),
                    params,
                )
                == "automatic"
            )
    finally:
        await engine.dispose()


async def test_downgrade_waits_for_inflight_manual_insert_before_deciding(
    migration_database_url,
):
    """降级不能在未提交manual写入前读空、等待DDL锁后静默丢失人工意图。"""
    engine = create_engine_from(migration_database_url)
    tenant, campaign, sender, approval = (
        TenantId(new_id("tn")),
        CampaignId(new_id("cmp")),
        SendingIdentityId(new_id("sid")),
        ApprovalId(new_id("apr")),
    )
    pending = None
    try:
        await _seed_active_campaign(
            async_sessionmaker(engine), tenant, campaign, (sender,), approval
        )
        async with engine.connect() as writer:
            transaction = await writer.begin()
            await writer.execute(
                text(
                    "INSERT INTO outreach_enrollments (tenant_id,enrollment_id,campaign_id,campaign_version,"
                    "account_id,contact_point_id,sending_identity_id,state,current_step,next_send_at,enrolled_at,"
                    "idempotency_key,execution_mode) VALUES (:tenant,:enrollment,:campaign,1,:account,:contact,"
                    ":sender,'enrolled',0,now(),now(),'inflight-manual','manual')"
                ),
                {
                    "tenant": tenant,
                    "campaign": campaign,
                    "sender": sender,
                    "enrollment": new_id("enr"),
                    "account": new_id("acc"),
                    "contact": new_id("cp"),
                },
            )
            pending = asyncio.create_task(
                asyncio.to_thread(
                    _run_alembic, migration_database_url, "downgrade", "0075"
                )
            )
            async with engine.connect() as observer:
                for _ in range(100):
                    waiting = await observer.scalar(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE datname=current_database() "
                            "AND wait_event_type='Lock' AND query LIKE '%outreach_enrollments%')"
                        )
                    )
                    await observer.commit()
                    if waiting:
                        break
                    await asyncio.sleep(0.03)
                else:
                    pytest.fail("降级没有等待实际在途manual事务")
            assert not pending.done()
            await transaction.commit()
        with pytest.raises(AssertionError):
            await asyncio.wait_for(pending, 10)
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    text(
                        "SELECT execution_mode FROM outreach_enrollments WHERE tenant_id=:tenant"
                    ),
                    {"tenant": tenant},
                )
                == "manual"
            )
            assert (
                await connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "0076"
            )
    finally:
        if pending is not None and not pending.done():
            await asyncio.wait_for(pending, 10)
        await engine.dispose()
