import importlib
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from connectors.web_search.client import PageSnapshot
from shared.schemas.identifiers import ArtifactId, new_id
from tests.unit.test_inventory_acquisition_driver import JOB


async def test_collector_only_accepts_pages_obtained_by_tool_and_keeps_extra_candidates():
    try:
        cls = importlib.import_module(
            "apps.scheduler_worker.inventory_collector"
        ).CodexInventoryCollector
    except ModuleNotFoundError:
        pytest.fail("RED: 真实工具驱动collector尚未实现")

    class Searcher:
        async def search(self, *args, **kwargs):
            return SimpleNamespace(
                results=(SimpleNamespace(description="", url="https://acme.example/", title="Acme"),)
            )

    class Reader:
        async def read_page(self, *args):
            return PageSnapshot(
                "Acme\nAddress: Kenya\ntricycles sales@acme.example",
                "https://acme.example/",
                datetime.now(UTC),
                "a" * 64,
                ArtifactId(new_id("art")),
            )

    tools = SimpleNamespace(
        searcher=Searcher(), page_reader=Reader(), browser_reader=Reader()
    )

    async def generate(job, source, session, payload):
        result = await session.call(
            "source_search", {"source_id": source, "query": "tricycles Kenya"}
        )
        page_id = result["results"][0]["page_id"]
        await session.call("read_public_page", {"page_id": page_id})
        return {
            "candidates": [
                {
                    "page_id": page_id,
                    "name": "Acme",
                    "country": "KE",
                    "public_emails": ["sales@acme.example"],
                },
                {
                    "page_id": "invented",
                    "name": "Fake",
                    "country": "KE",
                    "public_emails": [],
                },
            ]
        }

    async def factory(*args):
        return tools

    collector = cls(factory, generate)

    async def guard():
        pass

    candidates = (await collector.collect(JOB, "company_websites", guard)).candidates
    assert len(candidates) == 1 and candidates[0].name == "Acme"


async def test_directory_candidate_requires_official_followup_and_changes_query_strategy():
    from apps.scheduler_worker.inventory_collector import CodexInventoryCollector

    queries = []

    class Searcher:
        async def search(self, tenant, run, query, *args, **kwargs):
            queries.append(query)
            return SimpleNamespace(
                results=(SimpleNamespace(description="", url="https://acme.example/", title="Acme"),)
            )

    class Reader:
        async def read_page(self, *args):
            return PageSnapshot(
                "Acme\nAddress: Kenya\ntricycles sales@acme.example",
                "https://acme.example/",
                datetime.now(UTC),
                "a" * 64,
                ArtifactId(new_id("art")),
            )

    tools = SimpleNamespace(
        searcher=Searcher(), page_reader=Reader(), browser_reader=Reader()
    )

    async def generate(job, source, session, payload):
        found = await session.call(
            "source_search", {"source_id": source, "query": "tricycles Kenya"}
        )
        discovery = found["results"][0]["page_id"]
        await session.call("read_public_page", {"page_id": discovery})
        official = await session.call(
            "source_search", {"source_id": "company_websites", "query": "Acme Kenya"}
        )
        page = official["results"][0]["page_id"]
        await session.call("read_public_page", {"page_id": page})
        return {
            "candidates": [
                {"page_id": p, "name": "Acme", "country": "KE", "public_emails": []}
                for p in (discovery, page)
            ]
        }

    async def factory(*args):
        return tools

    async def guard():
        pass

    candidates = (await CodexInventoryCollector(factory, generate).collect(
        JOB, "trade_associations", guard
    )).candidates
    assert len(candidates) == 1
    assert "trade association members" in queries[0]
    assert "official company website contact" in queries[1]


@pytest.mark.parametrize(
    "category",
    [
        "page_access_forbidden",
        "provider_permanent",
        "login_or_captcha",
        "unsafe_redirect",
    ],
)
async def test_collector_keeps_evidence_after_known_page_failure(category):
    from apps.scheduler_worker.inventory_collector import CodexInventoryCollector
    from tool_gateway.errors import ToolErrorCategory, ToolGatewayError

    class Searcher:
        async def search(self, *args, **kwargs):
            return SimpleNamespace(
                results=(
                    SimpleNamespace(description="", url="https://acme.example", title="Acme"),
                    SimpleNamespace(description="", url="https://blocked.example", title="Blocked"),
                )
            )

    class Reader:
        async def read_page(self, tenant, run, batch, index):
            if index == 1:
                raise ToolGatewayError(ToolErrorCategory(category))
            return PageSnapshot(
                "Acme\nAddress: Kenya\ntricycles",
                "https://acme.example",
                datetime.now(UTC),
                "a" * 64,
                ArtifactId(new_id("art")),
            )

    async def factory(*args):
        return SimpleNamespace(
            searcher=Searcher(), page_reader=Reader(), browser_reader=Reader()
        )

    async def generate(job, source, session, payload):
        found = await session.call(
            "source_search", {"source_id": source, "query": "tricycles Kenya"}
        )
        good, bad = [entry["page_id"] for entry in found["results"]]
        await session.call("read_public_page", {"page_id": good})
        result = await session.call("read_public_page", {"page_id": bad})
        assert result["error"] == {"code": "page_unavailable", "retryable": False}
        assert result["meta"] == {"remaining_tools": 4, "remaining_searches": 1}
        return {
            "candidates": [
                {"page_id": good, "name": "Acme", "country": "KE", "public_emails": []}
            ]
        }

    async def guard():
        pass

    candidates = (await CodexInventoryCollector(factory, generate).collect(
        JOB, "company_websites", guard
    )).candidates
    assert len(candidates) == 1


async def test_directory_order_rejection_allows_correction_without_external_bypass():
    from apps.scheduler_worker.inventory_collector import CodexInventoryCollector
    queries = []

    class Searcher:
        async def search(self, tenant, run, query, *args, **kwargs):
            queries.append(query)
            return SimpleNamespace(results=(SimpleNamespace(description="", url='https://acme.example/', title='Acme'),))

    class Reader:
        async def read_page(self, *args):
            return PageSnapshot('Acme\nAddress: Kenya\ntricycles', 'https://acme.example/',
                                datetime.now(UTC), 'a' * 64, ArtifactId(new_id('art')))

    async def factory(*args):
        return SimpleNamespace(searcher=Searcher(), page_reader=Reader(), browser_reader=Reader())

    async def generate(job, source, session, payload):
        # 不合序动作消耗会话工具次数，但不能发生外部查询。
        rejected = await session.call('source_search', {'source_id': 'company_websites', 'query': 'premature'})
        assert rejected['error'] == {'code': 'source_read_required', 'retryable': False}
        assert rejected['meta'] == {'remaining_tools': 6, 'remaining_searches': 2}
        assert queries == []
        directory = await session.call('source_search', {'source_id': source, 'query': 'directory'})
        await session.call('read_public_page', {'page_id': directory['results'][0]['page_id']})
        official = await session.call('source_search', {'source_id': 'company_websites', 'query': 'Acme'})
        page_id = official['results'][0]['page_id']
        await session.call('read_public_page', {'page_id': page_id})
        return {'candidates': [{'page_id': page_id, 'name': 'Acme', 'country': 'KE', 'public_emails': []}]}

    async def guard():
        pass

    result = (await CodexInventoryCollector(factory, generate).collect(JOB, 'dealer_networks', guard)).candidates
    assert len(result) == 1 and result[0].name == 'Acme'
    assert len(queries) == 2


async def test_repeated_directory_order_rejection_consumes_existing_tool_budget():
    from apps.scheduler_worker.inventory_collector import CodexInventoryCollector

    async def factory(*args):
        return SimpleNamespace()  # 不存在外部端口，任何误派发必然失败。

    async def generate(job, source, session, payload):
        for _ in range(7):
            result = await session.call('source_search', {'source_id': 'company_websites', 'query': 'premature'})
            assert result['error']['code'] == 'source_read_required'
        assert (await session.call('source_search', {'source_id': source, 'query': 'directory'}))['error']['code'] == 'budget_exhausted'
        return {'candidates': []}

    async def guard():
        pass

    with pytest.raises(ValueError, match='来源未实际搜索'):
        await CodexInventoryCollector(factory, generate).collect(JOB, 'dealer_networks', guard)


@pytest.mark.parametrize("retry_query", ["steel Kenya", "different products Kenya"])
async def test_source_unknown_blocks_new_query_and_tool_call(retry_query):
    from apps.scheduler_worker.inventory_collector import CodexInventoryCollector
    from shared.errors import TransientError
    calls = []
    async def search(*args, **kwargs):
        calls.append(1)
        raise TransientError("unknown")
    async def factory(*args):
        return SimpleNamespace(searcher=SimpleNamespace(search=search))
    async def generate(job, source, session, payload):
        for query in ("steel Kenya", retry_query):
            try:
                await session.call("source_search", {"source_id": source, "query": query})
            except ValueError:
                assert len(calls) == 1
        return {"candidates": []}
    async def guard():
        pass
    await CodexInventoryCollector(factory, generate).collect(JOB, "company_websites", guard)
    assert len(calls) == 1
