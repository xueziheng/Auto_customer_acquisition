"""页面失败只在确定边界可换资源；未知结果不恢复。"""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from connectors.web_search.client import PageSnapshot
from shared.schemas.identifiers import ArtifactId, new_id
from tests.unit.test_inventory_acquisition_driver import JOB


@pytest.mark.parametrize("robots", [False, True])
def test_completed_http_error_is_distinct_from_network_unknown(monkeypatch, robots):
    from connectors.web_search import transport as module
    from shared.schemas.page_failure import PageReadFailure

    reader = module.SafePublicPageHttpTransport()
    monkeypatch.setattr(module, "_validate_public_url_sync", lambda url: url)
    if not robots:
        monkeypatch.setattr(reader, "_read_robots", lambda origin: None)
    monkeypatch.setattr(
        reader,
        "_request_once",
        lambda parsed: (503, None, "text/html", None, b"unavailable"),
    )
    with pytest.raises(PageReadFailure) as raised:
        reader._fetch_sync("https://company.example/")
    assert raised.value.reason == "http_error"
    assert raised.value.stage == ("page_robots" if robots else "page_http")
    assert raised.value.recoverable


async def test_unknown_page_id_can_be_corrected_without_dispatch():
    from apps.scheduler_worker.inventory_collector import CodexInventoryCollector

    reads = []

    class Reader:
        async def read_page(self, tenant, run, batch, index):
            reads.append(batch.results[index].url)
            return PageSnapshot(
                "Acme\nAddress: Kenya\ntricycles",
                "https://acme.example/",
                datetime.now(UTC),
                "a" * 64,
                ArtifactId(new_id("art")),
            )

    async def search(*args, **kwargs):
        return SimpleNamespace(
            results=(SimpleNamespace(description="", url="https://acme.example/", title="Acme"),)
        )

    async def factory(*args):
        return SimpleNamespace(
            searcher=SimpleNamespace(search=search),
            page_reader=Reader(),
            browser_reader=Reader(),
        )

    async def generate(job, source, session, payload):
        result = await session.call("read_public_page", {"page_id": "made-up"})
        assert result["error"]["code"] == "page_id_unknown"
        assert reads == []
        pages = await session.call(
            "source_search", {"source_id": source, "query": "Acme"}
        )
        page_id = pages["results"][0]["page_id"]
        assert page_id == "page_1"
        await session.call("read_public_page", {"page_id": page_id})
        return {
            "candidates": [
                {
                    "page_id": page_id,
                    "name": "Acme",
                    "country": "KE",
                    "public_emails": [],
                }
            ]
        }

    async def guard():
        pass

    assert (
        len(
            (await CodexInventoryCollector(factory, generate).collect(
                JOB, "company_websites", guard
            )).candidates
        )
        == 1
    )
    assert reads == ["https://acme.example/"]


@pytest.mark.parametrize("reason", ["http_error", "access_denied", "network_unknown"])
async def test_failed_origin_is_not_redispatched_by_new_id_browser_or_later_source(
    reason,
):
    from apps.scheduler_worker.inventory_collector import CodexInventoryCollector
    from shared.schemas.page_failure import PageReadFailure

    reads = []

    async def read(*args):
        reads.append(args[2].results[args[3]].url)
        raise PageReadFailure("page_http", reason)

    async def search(*args, **kwargs):
        return SimpleNamespace(
            results=(
                SimpleNamespace(description="", url="https://failed.example/path", title="Failed"),
            )
        )

    async def factory(*args):
        return SimpleNamespace(
            searcher=SimpleNamespace(search=search),
            page_reader=SimpleNamespace(read_page=read),
            browser_reader=SimpleNamespace(read_page=read),
        )

    async def generate(job, source, session, payload):
        pages = await session.call(
            "source_search", {"source_id": source, "query": "company"}
        )
        page_id = pages["results"][0]["page_id"]
        await session.call("read_public_page", {"page_id": page_id})
        await session.call("browse_public_page", {"page_id": page_id})
        pages = await session.call(
            "source_search", {"source_id": source, "query": "company again"}
        )
        await session.call(
            "read_public_page", {"page_id": pages["results"][0]["page_id"]}
        )
        return {"candidates": []}

    async def guard():
        pass

    collector = CodexInventoryCollector(factory, generate)
    if reason == "network_unknown":
        from shared.schemas.discovery_diagnostic import DiscoveryDiagnosticError

        with pytest.raises(DiscoveryDiagnosticError):
            await collector.collect(JOB, "company_websites", guard)
    else:
        assert (await collector.collect(JOB, "company_websites", guard)).candidates == ()
    # 下一来源新会话不能再派发同站点；允许的source_search不是页面重试。
    assert (await collector.collect(JOB, "dealer_networks", guard)).candidates == ()
    assert reads == ["https://failed.example/path"]


async def test_page_failure_slot_is_task_bound_and_consumed_once():
    from shared.schemas.page_failure import PageReadFailure
    from tool_gateway.handlers.web_slots import WebPageFailureSlot

    slot = WebPageFailureSlot()
    key = ("tenant", "run", "user", "https://company.example/")
    slot.begin(key)
    slot.put(key, PageReadFailure("page_http", "http_error"))

    async def child():
        assert slot.take(key) is None
        slot.clear()

    await asyncio.create_task(child())
    assert slot.take(key).reason == "http_error"
    assert slot.take(key) is None


@pytest.mark.parametrize("reason", ["http_error", "network_unknown", "failed"])
async def test_safe_failure_survives_real_gateway_without_stale_next_call(reason):
    from dataclasses import replace
    from datetime import timedelta

    from connectors.web_search.client import WebSearchResult
    from shared.schemas.page_failure import PageReadFailure
    from tests.unit.country_policy_gateway_support import (
        PreflightStage,
        _Ledger,
        _UnitOfWork,
    )
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

    searches, pages, failures = (
        WebSearchResultSlot(new_id, maximum_batches=2),
        WebPageSnapshotSlot(new_id),
        WebPageFailureSlot(),
    )
    batch = searches.put(
        "tenant",
        "KE",
        "tricycles",
        (
            WebSearchResult(
                title="Company", url="https://company.example/", description=""
            ),
        ),
    )

    class Reader:
        count = 0

        async def read_page(self, *args):
            self.count += 1
            if self.count == 1:
                raise PageReadFailure("browser_nav", reason)
            return PageSnapshot(
                "Company Kenya",
                "https://company.example/",
                datetime.now(UTC),
                "a" * 64,
                ArtifactId(new_id("art")),
            )

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
    ledger = _Ledger()
    gateway = ToolGateway(
        registry,
        {"playbook": PreflightStage(WebResearchPreflight("tenant", "KE", "tricycles"))},
        lambda tenant: _UnitOfWork(ledger),
        lease_duration=timedelta(seconds=30),
        lease_owner="test",
        now=lambda: datetime.now(UTC),
        id_factory=new_id,
    )
    adapter = ToolGatewayWebPageReader(gateway, pages, "user", failure_slot=failures)
    with pytest.raises(PageReadFailure) as raised:
        await adapter.read_page("tenant", "run", batch, 0)
    assert raised.value.stage == "browser_nav" and raised.value.reason == reason
    assert (await adapter.read_page("tenant", "run", batch, 0)).text == "Company Kenya"


async def test_definite_page_failure_can_switch_host_and_keep_evidence():
    from apps.scheduler_worker.inventory_collector import CodexInventoryCollector
    from shared.schemas.page_failure import PageReadFailure

    reads = []

    async def read(tenant, run, batch, index):
        url = batch.results[index].url
        reads.append(url)
        if index == 0:
            raise PageReadFailure("page_robots", "http_error")
        return PageSnapshot(
            "Acme\nAddress: Kenya\ntricycles",
            url,
            datetime.now(UTC),
            "a" * 64,
            ArtifactId(new_id("art")),
        )

    async def search(*args, **kwargs):
        return SimpleNamespace(
            results=tuple(
                SimpleNamespace(description="", url=url, title="Acme")
                for url in ("https://bad.example/", "https://acme.example/")
            )
        )

    async def factory(*args):
        return SimpleNamespace(
            searcher=SimpleNamespace(search=search),
            page_reader=SimpleNamespace(read_page=read),
            browser_reader=SimpleNamespace(read_page=read),
        )

    async def generate(job, source, session, payload):
        pages = (
            await session.call(
                "source_search", {"source_id": source, "query": "company"}
            )
        )["results"]
        assert (
            await session.call("read_public_page", {"page_id": pages[0]["page_id"]})
        )["error"]["code"] == "page_unavailable"
        await session.call("read_public_page", {"page_id": pages[1]["page_id"]})
        return {
            "candidates": [
                {
                    "page_id": pages[1]["page_id"],
                    "name": "Acme",
                    "country": "KE",
                    "public_emails": [],
                }
            ]
        }

    async def guard():
        pass

    assert (
        len(
            (await CodexInventoryCollector(factory, generate).collect(
                JOB, "company_websites", guard
            )).candidates
        )
        == 1
    )
    assert reads == ["https://bad.example/", "https://acme.example/"]


async def test_unknown_page_ids_still_consume_all_seven_tool_requests():
    from apps.scheduler_worker.inventory_collector import CodexInventoryCollector

    async def search(*args, **kwargs):
        return SimpleNamespace(results=())

    async def factory(*args):
        return SimpleNamespace(searcher=SimpleNamespace(search=search))

    async def generate(job, source, session, payload):
        await session.call("source_search", {"source_id": source, "query": "company"})
        for _ in range(6):
            assert (await session.call("read_public_page", {"page_id": "unknown"}))[
                "error"
            ]["code"] == "page_id_unknown"
        assert (await session.call("read_public_page", {"page_id": "unknown"}))[
            "error"
        ]["code"] == "budget_exhausted"
        return {"candidates": [{"page_id": "unknown", "name": "invented"}]}

    async def guard():
        pass

    assert (
        (await CodexInventoryCollector(factory, generate).collect(
            JOB, "company_websites", guard
        )).candidates
        == ()
    )


@pytest.mark.parametrize(
    "category", ["permission_denied", "reconciliation_required", "rate_limited"]
)
async def test_private_failure_cannot_override_gateway_authority_or_unknown_result(
    category,
):
    from connectors.web_search.client import WebSearchResult
    from shared.schemas.page_failure import PageReadFailure
    from tool_gateway.errors import ToolCallStatus, ToolErrorCategory, ToolGatewayError
    from tool_gateway.handlers.web_read_page import ToolGatewayWebPageReader
    from tool_gateway.handlers.web_slots import (
        WebPageFailureSlot,
        WebPageSnapshotSlot,
        WebSearchResultSlot,
    )
    from tool_gateway.pipeline import ToolCallResult

    failures = WebPageFailureSlot()
    batch = WebSearchResultSlot(new_id, maximum_batches=1).put(
        "tenant",
        "KE",
        "tricycles",
        (WebSearchResult("Company", "https://company.example/", ""),),
    )
    key = ("tenant", "run", "user", batch.results[0].url)

    class Gateway:
        async def invoke(self, ctx):
            failures.put(key, PageReadFailure("page_http", "http_error"))
            return ToolCallResult(
                "web.read_page",
                ToolCallStatus.FAILED_PERMANENT if category == "permission_denied" else ToolCallStatus.FAILED_TRANSIENT,
                error_category=ToolErrorCategory(category),
            )

    reader = ToolGatewayWebPageReader(
        Gateway(), WebPageSnapshotSlot(new_id), "user", failure_slot=failures
    )
    with pytest.raises(ToolGatewayError) as raised:
        await reader.read_page("tenant", "run", batch, 0)
    assert raised.value.category.value == category
    assert failures.take(key) is None
