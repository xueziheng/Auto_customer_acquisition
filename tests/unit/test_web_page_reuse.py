"""公开页复用必须位于 Gateway 授权之后，且保留每次独立交付与账本。"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from connectors.web_search.client import PageSnapshot, WebSearchResult
from shared.errors import ValidationError
from shared.schemas.identifiers import ArtifactId, RunId, TenantId, UserId, new_id
from tests.unit.test_tool_gateway_pipeline import NOW, _Ledger, _Uow
from tool_gateway.checks.permission import PermissionCheck
from tool_gateway.checks.web_discovery import WebResearchPreflight
from tool_gateway.errors import ToolCallStatus, ToolGatewayError
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.web_read_page import (
    MANIFEST,
    ToolGatewayWebPageReader,
    WebReadPageHandler,
)
from tool_gateway.handlers.web_slots import WebPageSnapshotSlot, WebSearchResultSlot
from tool_gateway.manifest import ToolRegistry
from tool_gateway.pipeline import CheckRejection, ToolGateway


class _Reader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.fail: BaseException | None = None
        self.final_url: str | None = None

    async def read_page(self, tenant_id, url, uploaded_by):
        self.calls.append((tenant_id, url, uploaded_by))
        if self.fail is not None:
            raise self.fail
        return PageSnapshot(
            "Observed public company information.", self.final_url or url, NOW,
            "a" * 64, ArtifactId(new_id("art")),
        )


class _Harness:
    def __init__(self) -> None:
        self.tenant = TenantId(new_id("tn"))
        self.run = RunId(new_id("run"))
        self.actor = UserId(new_id("usr"))
        self.searches = WebSearchResultSlot(new_id, maximum_batches=100)
        self.pages = WebPageSnapshotSlot(new_id)
        self.provider = _Reader()
        self.trace: list[str] = []
        self.ledger = _Ledger(self.trace)
        self.reject_stage: str | None = None
        self.fail_success_commit = False

        async def authorize(ctx, state):
            del ctx, state
            self.trace.append("permission")
            return self.reject_stage != "permission"

        class Check:
            def __init__(stage, name):
                stage.name = name

            async def check(stage, ctx, state):
                self.trace.append(stage.name)
                if self.reject_stage == stage.name:
                    return CheckRejection(stage.name, "research:denied", "研究已禁止")
                if stage.name == "playbook":
                    batch = self.searches.get_batch(ctx.params["search_result_handle"])
                    state.preflight = WebResearchPreflight(
                        ctx.tenant_id, batch.country, batch.category
                    )
                return None

        checks = {
            name: PermissionCheck(authorize) if name == "permission" else Check(name)
            for name in MANIFEST.checks
        }
        self.handler = WebReadPageHandler(
            self.provider, self.searches, self.pages,
            HmacFingerprintProvider("test-v1", bytes(32)),
        )
        registry = ToolRegistry()
        registry.register(MANIFEST, self.handler)
        self.gateway = ToolGateway(
            registry, checks,
            lambda _tenant: _Uow(self.ledger, fail_succeeded_commit=self.fail_success_commit),
            lease_duration=timedelta(minutes=5), lease_owner="page-test",
            now=lambda: NOW, id_factory=new_id,
        )

    async def read(self, url="https://example.com/company", *, tenant=None, run=None, actor=None):
        tenant = tenant or self.tenant
        run = run or self.run
        actor = actor or self.actor
        batch = self.searches.put(
            tenant, "US", "hinges", (WebSearchResult("Company", url, "public"),)
        )
        try:
            return await ToolGatewayWebPageReader(self.gateway, self.pages, actor).read_page(
                tenant, run, batch, 0
            )
        finally:
            self.searches.discard(batch.handle)


async def test_repeated_allowed_url_reuses_snapshot_after_every_gateway_check_and_ledger():
    harness = _Harness()
    first = await harness.read()
    second = await harness.read()
    assert first == second
    assert len(harness.provider.calls) == 1
    for stage in MANIFEST.checks:
        assert harness.trace.count(stage) == 2
    records = tuple(harness.ledger.records.values())
    assert len(records) == 2
    assert all(record.status is ToolCallStatus.SUCCEEDED for record in records)
    assert records[0].provider_ref != records[1].provider_ref
    for record in records:
        with pytest.raises(ValidationError):
            harness.pages.take(record.provider_ref)


@pytest.mark.parametrize("stage", ["permission", "playbook", "country_policy", "rate_limit"])
async def test_revoked_second_read_never_delivers_cached_page_and_clears_reuse(stage):
    harness = _Harness()
    await harness.read()
    harness.reject_stage = stage
    with pytest.raises(ToolGatewayError):
        await harness.read()
    assert len(harness.provider.calls) == 1
    last = tuple(harness.ledger.records.values())[-1]
    assert last.status is ToolCallStatus.REJECTED
    assert last.provider_ref is None
    harness.reject_stage = None
    await harness.read()
    assert len(harness.provider.calls) == 2


@pytest.mark.parametrize("binding", ["tenant", "run", "actor", "task"])
async def test_page_reuse_never_crosses_security_binding(binding):
    harness = _Harness()
    await harness.read()
    if binding == "task":
        await asyncio.create_task(harness.read())
    else:
        value = {
            "tenant": TenantId(new_id("tn")), "run": RunId(new_id("run")),
            "actor": UserId(new_id("usr")),
        }[binding]
        await harness.read(**{binding: value})
    assert len(harness.provider.calls) == 2


async def test_different_handler_with_identical_binding_does_not_reuse_another_reader():
    first = _Harness()
    second = _Harness()
    second.tenant, second.run, second.actor = first.tenant, first.run, first.actor
    original = await first.read()
    independent = await second.read()
    assert independent.snapshot_artifact_ref != original.snapshot_artifact_ref
    assert len(first.provider.calls) == len(second.provider.calls) == 1


async def test_child_failure_clears_only_child_context_not_parent_page_reuse():
    harness = _Harness()
    parent = await harness.read()

    async def child():
        harness.provider.fail = RuntimeError("synthetic")
        try:
            with pytest.raises(ToolGatewayError):
                await harness.read()
        finally:
            harness.provider.fail = None

    await asyncio.create_task(child())
    assert await harness.read() == parent
    assert len(harness.provider.calls) == 2


@pytest.mark.parametrize("error", [RuntimeError("synthetic"), asyncio.CancelledError()])
async def test_provider_exception_or_cancellation_discards_earlier_reuse(error):
    harness = _Harness()
    await harness.read()
    harness.provider.fail = error
    with pytest.raises((ToolGatewayError, asyncio.CancelledError)):
        await harness.read("https://example.com/other")
    harness.provider.fail = None
    await harness.read()
    assert len(harness.provider.calls) == 3


async def test_ledger_completion_failure_does_not_leave_reusable_or_deliverable_page():
    harness = _Harness()
    harness.fail_success_commit = True
    with pytest.raises(ToolGatewayError):
        await harness.read()
    harness.fail_success_commit = False
    await harness.read()
    assert len(harness.provider.calls) == 2


async def test_delivery_slot_failure_clears_previous_reuse_before_retry():
    harness = _Harness()
    first = await harness.read()
    occupied = harness.pages.put(first)
    with pytest.raises(ToolGatewayError):
        await harness.read()
    with pytest.raises(ValidationError):
        harness.pages.take(occupied)
    await harness.read()
    assert len(harness.provider.calls) == 2


async def test_redirect_alias_is_not_reused_until_exact_final_url_is_requested():
    harness = _Harness()
    harness.provider.final_url = "https://example.com/final"
    first = await harness.read("https://example.com/start")
    second = await harness.read("https://example.com/start")
    assert len(harness.provider.calls) == 2
    third = await harness.read("https://example.com/final")
    assert len(harness.provider.calls) == 2
    assert third == second
    assert third.snapshot_artifact_ref != first.snapshot_artifact_ref


async def test_reuse_holds_at_most_fifty_final_urls():
    harness = _Harness()
    for index in range(51):
        await harness.read(f"https://example.com/company/{index}")
    await harness.read("https://example.com/company/50")
    assert len(harness.provider.calls) == 51
    await harness.read("https://example.com/company/0")
    assert len(harness.provider.calls) == 52
