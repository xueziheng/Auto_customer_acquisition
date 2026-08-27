"""免费额度纯策略与 handler 的显式 run-bound 插件边界。"""

from __future__ import annotations

import importlib

import pytest

from connectors.search_contracts import SearchCostStatus, SearchUsage
from shared.errors import ValidationError
from shared.schemas.identifiers import RunId, TenantId, UserId, new_id
from tool_gateway.checks.web_discovery import WebResearchPreflight
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.web_search import WebSearchHandler
from tool_gateway.handlers.web_slots import WebSearchResultSlot
from tool_gateway.pipeline import ToolCallContext


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        (SearchUsage("Researcher", 10, 9, False, SearchCostStatus.FREE), 1),
        (SearchUsage("Researcher", 10, 11, False, SearchCostStatus.FREE), 0),
        (SearchUsage("Researcher", 10, 0, None, SearchCostStatus.FREE), None),
        (SearchUsage("Researcher", 10, 0, True, SearchCostStatus.FREE), None),
        (SearchUsage("Unknown", 10, 0, False, SearchCostStatus.FREE), None),
        (SearchUsage("Researcher", None, 0, False, SearchCostStatus.FREE), None),
        (SearchUsage("Researcher", 10, 0, False, SearchCostStatus.UNKNOWN), None),
    ],
)
def test_only_verified_free_usage_has_spendable_credit(usage, expected) -> None:
    try:
        module = importlib.import_module("tool_gateway.free_search_contracts")
    except ModuleNotFoundError:
        pytest.fail("RED：免费成本策略尚未实现")
    assert module.verified_free_remaining(usage) == expected


class BoundReader:
    def __init__(self, run: RunId, calls: list[RunId]) -> None:
        self.run = run
        self.calls = calls

    async def search(self, tenant_id, query, country, limit):
        self.calls.append(self.run)
        return ()


class ReaderFactory:
    def __init__(self) -> None:
        self.calls: list[RunId] = []

    def for_run(self, tenant_id, run_id, request_key):
        return BoundReader(run_id, self.calls)


async def test_handler_binds_run_to_prepared_payload_without_mutable_current_run() -> (
    None
):
    factory = ReaderFactory()
    try:
        handler = WebSearchHandler(
            None,
            WebSearchResultSlot(new_id, maximum_batches=10),
            HmacFingerprintProvider("v1", b"x" * 32),
            reader_factory=factory,
        )
    except TypeError:
        pytest.fail("RED：handler 尚未提供 run-bound reader 注入")
    tenant = TenantId("tn_test")
    preflight = WebResearchPreflight(tenant, "US", "hinges")
    prepared = []
    for run in ("run_first", "run_second"):
        prepared.append(
            await handler.prepare(
                ToolCallContext(
                    tenant,
                    UserId("usr_test"),
                    "web.search",
                    {
                        "query": "factory",
                        "country": "US",
                        "category": "hinges",
                        "limit": 1,
                    },
                    run_id=RunId(run),
                ),
                preflight,
            )
        )
    await handler.execute(tenant, prepared[0])
    await handler.execute(tenant, prepared[1])
    assert factory.calls == ["run_first", "run_second"]
    with pytest.raises(ValidationError):
        await handler.prepare(
            ToolCallContext(
                tenant,
                UserId("usr_test"),
                "web.search",
                {"query": "factory", "country": "US", "category": "hinges", "limit": 1},
            ),
            preflight,
        )


@pytest.mark.parametrize("reader_fails", [True, False])
async def test_gateway_uncertain_without_readable_run_state_stays_typed_and_nonretryable(
    reader_fails,
):
    from tool_gateway.errors import ToolErrorCategory, ToolGatewayError
    from tool_gateway.free_search_contracts import FreeSearchError
    from tool_gateway.handlers.free_search import FreeSearchGatewaySearcher

    class FailedGateway:
        async def search(self, *args):
            raise ToolGatewayError(ToolErrorCategory.RECONCILIATION_REQUIRED)

    class StateReader:
        async def run_state(self, run_id):
            if reader_fails:
                raise RuntimeError("sensitive-database-detail")

    adapter = FreeSearchGatewaySearcher(
        FailedGateway(), StateReader(), TenantId("tn_test")
    )
    with pytest.raises(FreeSearchError) as failure:
        await adapter.search(
            TenantId("tn_test"), RunId("run_test"), "factory", "US", "hinges", 1
        )
    assert failure.value.reason == "request_uncertain"
    assert failure.value.is_retryable is False
    assert "sensitive" not in str(failure.value)
