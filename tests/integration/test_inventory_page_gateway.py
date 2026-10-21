"""真实Postgres页面失败账本与task私有诊断交接。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from connectors.web_search.client import WebSearchResult
from infra.db.tables import ToolCallRow
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from shared.schemas.identifiers import new_id
from shared.schemas.page_failure import PageReadFailure
from tests.unit.country_policy_gateway_support import PreflightStage
from tool_gateway.checks.web_discovery import WebResearchPreflight
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.web_read_page import (
    MANIFEST,
    ToolGatewayWebPageReader,
    WebReadPageHandler,
)
from tool_gateway.handlers.web_slots import (
    WebPageFailureSlot,
    WebPageSnapshotSlot,
    WebSearchResultSlot,
)
from tool_gateway.manifest import ToolRegistry
from tool_gateway.pipeline import ToolGateway


@pytest.mark.parametrize(
    "stage,reason,category",
    [
        ("page_robots", "http_error", "provider_transient"),
        ("page_http", "network_unknown", "provider_transient"),
        ("browser_nav", "access_denied", "page_access_forbidden"),
        ("browser_init", "failed", "unexpected"),
    ],
)
async def test_inventory_page_failure_private_handoff_and_durable_classification(
    integration_engine, stage, reason, category
):
    sessions = async_sessionmaker(integration_engine, expire_on_commit=False)
    tenant, run, user = new_id("tn"), new_id("run"), new_id("usr")
    searches, pages, failures = (
        WebSearchResultSlot(new_id, maximum_batches=2),
        WebPageSnapshotSlot(new_id),
        WebPageFailureSlot(),
    )
    batch = searches.put(
        tenant,
        "KE",
        "tricycles",
        (
            WebSearchResult(
                title="Public company", url="https://public.example/", description=""
            ),
        ),
    )

    class Reader:
        async def read_page(self, *args):
            raise PageReadFailure(stage, reason)

    registry = ToolRegistry()
    registry.register(
        replace(MANIFEST, checks=("playbook",)),
        WebReadPageHandler(
            Reader(),
            searches,
            pages,
            HmacFingerprintProvider("v1", b"k" * 32),
            failure_slot=failures,
        ),
    )
    gateway = ToolGateway(
        registry,
        {"playbook": PreflightStage(WebResearchPreflight(tenant, "KE", "tricycles"))},
        lambda selected: SqlAlchemyToolGatewayUnitOfWork(sessions, selected),
        lease_duration=timedelta(seconds=30),
        lease_owner="page-test",
        now=lambda: datetime.now(UTC),
        id_factory=new_id,
    )
    adapter = ToolGatewayWebPageReader(gateway, pages, user, failure_slot=failures)
    with pytest.raises(PageReadFailure) as raised:
        await adapter.read_page(tenant, run, batch, 0)
    assert raised.value.stage == stage and raised.value.reason == reason
    async with sessions() as session:
        rows = (
            await session.scalars(
                select(ToolCallRow).where(
                    ToolCallRow.tenant_id == tenant, ToolCallRow.run_id == run
                )
            )
        ).all()
    assert len(rows) == 1 and rows[0].error_category == category
    assert "public.example" not in str(
        {
            column.name: getattr(rows[0], column.name)
            for column in ToolCallRow.__table__.columns
        }
    )
