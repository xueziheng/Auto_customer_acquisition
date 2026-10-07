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
        (SearchUsage("Researcher", 10, 0, False, SearchCostStatus.PAID), None),
        (SearchUsage("Researcher", 10, None, False, SearchCostStatus.FREE), None),
    ],
)
def test_only_verified_free_usage_has_spendable_credit(usage, expected) -> None:
    try:
        module = importlib.import_module("tool_gateway.free_search_contracts")
    except ModuleNotFoundError:
        pytest.fail("RED：免费成本策略尚未实现")
    assert module.verified_free_remaining(usage) == expected


@pytest.mark.parametrize(("limit", "used", "expected"), [(10, 9, 1), (10, 10, 0), (10, 11, 0)])
def test_included_free_credits_remain_spendable_with_unknown_paygo(
    limit: int, used: int, expected: int,
) -> None:
    """独立的套餐免费事实可证明余额，不能将 null 按量配置误判为余额未知。"""
    from tool_gateway.free_search_contracts import verified_free_remaining

    usage = SearchUsage(
        "Researcher", limit, used, None, SearchCostStatus.UNKNOWN,
        included_credits_free=True,
    )

    assert verified_free_remaining(usage) == expected
    assert usage.paygo_enabled is None
    assert usage.cost_status is SearchCostStatus.UNKNOWN


@pytest.mark.parametrize("value", [None, 0, 1, "true"])
def test_included_free_credit_fact_rejects_non_boolean_values(value: object) -> None:
    """如果允许 truthy 值充当事实，反序列化的字符串可绕过免费额度门禁。"""
    with pytest.raises(ValidationError):
        SearchUsage(
            "Researcher", 10, 0, None, SearchCostStatus.UNKNOWN,
            included_credits_free=value,
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"plan": "Unknown"},
        {"plan": "researcher"},
        {"plan": None},
        {"limit": None},
        {"used": None},
        {"cost_status": SearchCostStatus.PAID},
        {"paygo_enabled": True},
    ],
)
def test_included_free_credit_fact_cannot_contradict_usage(changes: dict[str, object]) -> None:
    """可信事实不能与未知套餐、缺失计数或明确付费状态同时成立。"""
    fields = {
        "plan": "Researcher", "limit": 10, "used": 0,
        "paygo_enabled": None, "cost_status": SearchCostStatus.UNKNOWN,
        "included_credits_free": True,
    }
    fields.update(changes)

    with pytest.raises(ValidationError):
        SearchUsage(**fields)


def test_legacy_usage_does_not_infer_included_free_credit_fact() -> None:
    """旧调用者的 FREE 分类继续可用，但新事实不能由构造默认值虚构。"""
    from tool_gateway.free_search_contracts import verified_free_remaining

    usage = SearchUsage("Researcher", 10, 0, False, SearchCostStatus.FREE)

    assert usage.included_credits_free is False
    assert verified_free_remaining(usage) == 10


def test_legacy_snapshot_defaults_to_unproven_included_credits() -> None:
    """新增事实不能让老快照反序列化后自动获得免费资格。"""
    from tool_gateway.free_search_contracts import SearchQuotaSnapshot

    snapshot = SearchQuotaSnapshot(
        TenantId("tn_test"), "tavily", 9, 1, SearchCostStatus.UNKNOWN,
        10, 0, None, None,
    )

    assert snapshot.included_credits_free is False


@pytest.mark.parametrize("paygo_enabled", [1, "false"])
def test_snapshot_included_free_fact_rejects_nonboolean_paygo_state(
    paygo_enabled: object,
) -> None:
    """快照不能把整数或字符串按量状态当作免费资格的兼容证明。"""
    from tool_gateway.free_search_contracts import SearchQuotaSnapshot

    with pytest.raises(ValidationError):
        SearchQuotaSnapshot(
            TenantId("tn_test"), "tavily", 9, 1, SearchCostStatus.UNKNOWN,
            10, 0, paygo_enabled, None, included_credits_free=True,
        )


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
        self.keys: list[str] = []

    def for_run(self, tenant_id, run_id, request_key, *, fingerprint_version):
        assert fingerprint_version == "v1"
        self.keys.append(request_key)
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


async def test_free_handler_uses_explicit_sourcing_quota_key_without_changing_fingerprint() -> (
    None
):
    key = "a" * 64
    tenant = TenantId("tn_test")
    factory = ReaderFactory()
    fingerprints = HmacFingerprintProvider("v1", b"x" * 32)
    handler = WebSearchHandler(
        None,
        WebSearchResultSlot(new_id, maximum_batches=10),
        fingerprints,
        reader_factory=factory,
    )
    params = {
        "query": "factory",
        "country": "US",
        "category": "hinges",
        "limit": 1,
    }
    ordinary = await handler.prepare(
        ToolCallContext(
            tenant,
            UserId("usr_test"),
            "web.search",
            params,
            run_id=RunId("run_ordinary"),
        ),
        WebResearchPreflight(tenant, "US", "hinges"),
    )
    sourcing = await handler.prepare(
        ToolCallContext(
            tenant,
            UserId("usr_test"),
            "web.search",
            {**params, "quota_request_key": key},
            run_id=RunId("run_sourcing"),
        ),
        WebResearchPreflight(tenant, "US", "hinges"),
    )

    assert sourcing.request_fingerprint == ordinary.request_fingerprint
    assert factory.keys == [ordinary.request_fingerprint, key]


@pytest.mark.parametrize("value", ["short", "g" * 64, 7, None])
async def test_free_handler_rejects_malformed_explicit_quota_key(value: object) -> None:
    tenant = TenantId("tn_test")
    handler = WebSearchHandler(
        None,
        WebSearchResultSlot(new_id, maximum_batches=10),
        HmacFingerprintProvider("v1", b"x" * 32),
        reader_factory=ReaderFactory(),
    )

    with pytest.raises(ValidationError):
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
                    "quota_request_key": value,
                },
                run_id=RunId("run_sourcing"),
            ),
            WebResearchPreflight(tenant, "US", "hinges"),
        )


async def test_non_free_handler_rejects_explicit_quota_key() -> None:
    tenant = TenantId("tn_test")

    class Searcher:
        async def search(self, *args):
            return ()

    handler = WebSearchHandler(
        Searcher(),
        WebSearchResultSlot(new_id, maximum_batches=10),
        HmacFingerprintProvider("v1", b"x" * 32),
    )

    with pytest.raises(ValidationError):
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
                    "quota_request_key": "a" * 64,
                },
                run_id=RunId("run_sourcing"),
            ),
            WebResearchPreflight(tenant, "US", "hinges"),
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


@pytest.mark.parametrize("category", ["rate_limited", "provider_transient"])
@pytest.mark.parametrize("state_kind", ["uncertain", "unreadable"])
async def test_gateway_transient_with_uncertain_or_unreadable_quota_cannot_retry(
    category: str, state_kind: str
) -> None:
    from datetime import UTC, datetime

    from tool_gateway.errors import ToolErrorCategory, ToolGatewayError
    from tool_gateway.free_search_contracts import (
        FreeSearchError,
        FreeSearchStopReason,
        SearchQuotaRunState,
    )
    from tool_gateway.handlers.free_search import FreeSearchGatewaySearcher

    tenant, run = TenantId("tn_test"), RunId("run_test")

    class FailedGateway:
        async def search(self, *args):
            raise ToolGatewayError(ToolErrorCategory(category), retry_after_seconds=17)

    class StateReader:
        async def run_state(self, run_id):
            if state_kind == "unreadable":
                raise RuntimeError("sensitive-database-detail")
            return SearchQuotaRunState(
                tenant, run_id, FreeSearchStopReason.REQUEST_UNCERTAIN,
                datetime(2026, 9, 26, tzinfo=UTC),
            )

    adapter = FreeSearchGatewaySearcher(FailedGateway(), StateReader(), tenant)
    with pytest.raises(FreeSearchError) as failure:
        await adapter.search(tenant, run, "factory", "US", "hinges", 1)

    assert failure.value.reason is FreeSearchStopReason.REQUEST_UNCERTAIN
    assert failure.value.is_retryable is False
    assert failure.value.retry_after_seconds is None
    assert "sensitive" not in str(failure.value)


@pytest.mark.parametrize("category", ["rate_limited", "provider_transient"])
@pytest.mark.parametrize("state_kind", ["missing", "clear"])
async def test_gateway_transient_without_pending_reservation_keeps_original_error(
    category: str, state_kind: str
) -> None:
    from datetime import UTC, datetime

    from tool_gateway.errors import ToolErrorCategory, ToolGatewayError
    from tool_gateway.free_search_contracts import SearchQuotaRunState
    from tool_gateway.handlers.free_search import FreeSearchGatewaySearcher

    tenant, run = TenantId("tn_test"), RunId("run_test")
    original = ToolGatewayError(ToolErrorCategory(category), retry_after_seconds=17)

    class FailedGateway:
        async def search(self, *args):
            raise original

    class StateReader:
        async def run_state(self, run_id):
            if state_kind == "missing":
                return None
            return SearchQuotaRunState(
                tenant, run_id, None, datetime(2026, 9, 26, tzinfo=UTC)
            )

    adapter = FreeSearchGatewaySearcher(FailedGateway(), StateReader(), tenant)
    with pytest.raises(ToolGatewayError) as failure:
        await adapter.search(tenant, run, "factory", "US", "hinges", 1)

    assert failure.value is original
    assert failure.value.is_retryable is True
    assert failure.value.retry_after_seconds == 17


@pytest.mark.parametrize(
    ("provider_error", "expected"),
    [
        ("rate", "rate_limited"),
        ("transient", "provider_transient"),
        ("unknown", "reconciliation_required"),
    ],
)
async def test_free_reader_preserves_determinate_tavily_dispatch_failures(
    provider_error: str, expected: str
) -> None:
    from connectors.search_contracts import SearchCostStatus, SearchUsage
    from connectors.tavily.transport import TavilyRateLimitedError, TavilyTransientError
    from tool_gateway.errors import ToolGatewayError
    from tool_gateway.handlers.free_search import FreeSearchReader

    class Quota:
        async def check_available(self, *args, **kwargs):
            pass

        async def reserve(self, *args, **kwargs):
            pass

        async def mark_dispatched(self, *args, **kwargs):
            pass

        async def consume(self, *args, **kwargs):
            pass

        async def record_unavailable(self, *args, **kwargs):
            pass

    class Connector:
        async def configure(self, resolver):
            pass

        async def usage(self):
            return SearchUsage("Researcher", 10, 0, False, SearchCostStatus.FREE)

        async def search(self, *args, **kwargs):
            if provider_error == "rate":
                raise TavilyRateLimitedError(17)
            if provider_error == "transient":
                raise TavilyTransientError()
            raise RuntimeError("sensitive-provider-body")

    reader = FreeSearchReader(
        TenantId("tn_test"),
        RunId("run_test"),
        "a" * 64,
        "v1",
        Quota(),
        lambda: Connector(),
        object(),
    )
    with pytest.raises(ToolGatewayError) as caught:
        await reader.search(TenantId("tn_test"), "factory", "US", 1)
    assert caught.value.category.value == expected
    assert "sensitive" not in str(caught.value)
