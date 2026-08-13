"""真实 PostgreSQL、Uvicorn、Vite 与 Chromium 的 Phase 1 行为闭环。"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import pytest
from playwright.async_api import Locator, async_playwright, expect
from pydantic import TypeAdapter
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.composition.runtime import build_phase1_dependencies
from domains.opportunities.permissions import (
    Actor as OpportunityActor,
)
from domains.opportunities.permissions import OpportunityScope, ScopeLevel
from domains.opportunities.schemas import (
    HandoffCreateRequest,
    OpportunityView,
)
from infra.db.tables import (
    HandoffRow,
    OpportunityRow,
    OutboxEventRow,
    OwnershipLockRow,
    ProvenanceRecordRow,
    ScoreSnapshotRow,
)
from infra.secrets import EnvironmentSecretResolver
from shared.schemas.identifiers import HandoffId, OpportunityId, TenantId
from shared.schemas.provenance import Provenance, SourceType

if TYPE_CHECKING:
    from conftest import E2EStack

_OPPORTUNITY_ADAPTER = TypeAdapter(OpportunityView)
_CUSTOMER_VERBATIM = "Please prepare a formal quotation for our team."


class _Clock:
    def __init__(self, value: datetime) -> None:
        self._value = value

    def now(self) -> datetime:
        return self._value

    def advance(self, delta: timedelta) -> None:
        self._value += delta


@dataclass(frozen=True)
class E2EScenario:
    opportunity_ids: tuple[OpportunityId, ...]
    oldest_handoff: HandoffId
    second_handoff: HandoffId
    provenance_extracted_at: str


def _serialize_utc(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _opportunity_body(
    index: int,
    country: str,
    category: str,
    *,
    extracted_at: datetime,
) -> dict[str, object]:
    source_id = f"e2e-message-{index}"
    provenance = {
        "source_type": "conversation",
        "source_id": source_id,
        "extracted_by": "human",
        "extracted_at": _serialize_utc(extracted_at),
    }
    return {
        "request": {
            "need_id": f"e2e-need-{index}",
            "account_id": f"e2e-account-{index}",
            "account_name": f"E2E 企业 {index}",
            "country": country,
            "product_category": category,
            "evidence_tier": "customer_interest_reply",
            "has_verified_contact": True,
            "category_allowed": True,
            "minimum_order_value": {"amount": "1000.00", "currency": "USD"},
            "supply_available": True,
            "estimated_order_value": {"amount": "5000.00", "currency": "USD"},
            "field_provenance": {
                "account_name": provenance,
                "country": provenance,
            },
        },
        "evidence": {
            "level": "customer_interest_reply",
            "provenance": provenance,
        },
    }


def _post_json(request: Request) -> tuple[int, bytes]:
    try:
        with urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except HTTPError as exc:
        return exc.code, b""


async def _post_opportunity(
    stack: E2EStack,
    index: int,
    country: str,
    category: str,
    *,
    extracted_at: datetime,
) -> OpportunityView:
    request = Request(
        f"{stack.api_origin}/crm/opportunities",
        data=json.dumps(
            _opportunity_body(
                index,
                country,
                category,
                extracted_at=extracted_at,
            )
        ).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Tenant-Id": str(stack.tenant_id),
            "X-Employee-Id": str(stack.employees.boss),
        },
        method="POST",
    )
    status, response_bytes = await asyncio.to_thread(_post_json, request)
    assert status == 201
    return _OPPORTUNITY_ADAPTER.validate_json(response_bytes)


async def _count_rows(
    session: AsyncSession,
    model: Any,
    tenant_id: TenantId,
    event_type: str | None = None,
) -> int:
    statement = select(func.count()).select_from(model).where(
        model.tenant_id == str(tenant_id)
    )
    if event_type is not None:
        statement = statement.where(model.event_type == event_type)
    return int(await session.scalar(statement) or 0)


async def _opportunity_owners(
    session: AsyncSession,
    tenant_id: TenantId,
) -> list[str]:
    return list(
        (
            await session.scalars(
                select(OpportunityRow.owner).where(
                    OpportunityRow.tenant_id == str(tenant_id)
                )
            )
        ).all()
    )


async def _conversation_provenance_count(
    session: AsyncSession,
    tenant_id: TenantId,
) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(ProvenanceRecordRow)
            .where(
                ProvenanceRecordRow.tenant_id == str(tenant_id),
                ProvenanceRecordRow.entity_type == "opportunity",
                ProvenanceRecordRow.source_type == "conversation",
            )
        )
        or 0
    )


async def _conversation_provenance_times(
    session: AsyncSession,
    tenant_id: TenantId,
) -> list[datetime]:
    return list(
        (
            await session.scalars(
                select(ProvenanceRecordRow.extracted_at).where(
                    ProvenanceRecordRow.tenant_id == str(tenant_id),
                    ProvenanceRecordRow.entity_type == "opportunity",
                    ProvenanceRecordRow.source_type == "conversation",
                )
            )
        ).all()
    )


async def _score_evidence_tiers(
    session: AsyncSession,
    tenant_id: TenantId,
) -> list[str | None]:
    return list(
        (
            await session.scalars(
                select(ScoreSnapshotRow.evidence_tier).where(
                    ScoreSnapshotRow.tenant_id == str(tenant_id)
                )
            )
        ).all()
    )


async def _assert_created_rows(
    stack: E2EStack,
    created: list[OpportunityView],
    scenario_started_at: datetime,
) -> None:
    assert len(created) == 5
    assert [item.owner for item in created] == [
        str(stack.employees.sales_a),
        str(stack.employees.sales_b),
        str(stack.employees.sales_a),
        str(stack.employees.sales_b),
        str(stack.employees.sales_a),
    ]
    async with stack.factory() as session:
        assert await _count_rows(session, OpportunityRow, stack.tenant_id) == 5
        assert await _count_rows(session, ScoreSnapshotRow, stack.tenant_id) == 5
        assert set(await _score_evidence_tiers(session, stack.tenant_id)) == {
            "mid_high"
        }
        assert await _count_rows(session, OwnershipLockRow, stack.tenant_id) == 5
        assert (
            await _count_rows(
                session,
                OutboxEventRow,
                stack.tenant_id,
                "OpportunityQualified",
            )
            == 5
        )
        assert set(await _opportunity_owners(session, stack.tenant_id)) == {
            str(stack.employees.sales_a),
            str(stack.employees.sales_b),
        }
        assert await _conversation_provenance_count(session, stack.tenant_id) == 10
        provenance_times = await _conversation_provenance_times(
            session,
            stack.tenant_id,
        )
        assert len(provenance_times) == 10
        assert set(provenance_times) == {scenario_started_at}


def _handoff_request(
    opportunity: OpportunityView,
    source_id: str,
) -> HandoffCreateRequest:
    return HandoffCreateRequest(
        opportunity_id=opportunity.opportunity_id,
        trigger="quote_requested",
        account_name=opportunity.account_name,
        country=opportunity.country,
        why_valuable="客户已明确请求报价并具备可供应条件",
        customer_verbatim=_CUSTOMER_VERBATIM,
        customer_verbatim_provenance=Provenance(
            source_type=SourceType.CONVERSATION,
            source_id=source_id,
            extracted_by="human",
            extracted_at=datetime(2026, 8, 10, 2, tzinfo=UTC),
        ),
        validated_need_summary="客户明确表达采购需求",
        suggested_next_step="真人核对规格后准备报价",
        evidence_links=[f"/crm/opportunities/{opportunity.opportunity_id}"],
    )


async def _event_count(
    session: AsyncSession,
    tenant_id: TenantId,
    event_type: str,
    subject_id: str,
) -> int:
    subject_field = {
        "OpportunityLost": "opportunity_id",
        "HandoffRequested": "handoff_id",
        "HandoffAccepted": "handoff_id",
    }[event_type]
    return int(
        await session.scalar(
            select(func.count())
            .select_from(OutboxEventRow)
            .where(
                OutboxEventRow.tenant_id == str(tenant_id),
                OutboxEventRow.event_type == event_type,
                OutboxEventRow.event_payload[subject_field].as_string() == subject_id,
            )
        )
        or 0
    )


async def _assert_handoff_order(
    stack: E2EStack,
    oldest: HandoffId,
    second: HandoffId,
) -> None:
    async with stack.factory() as session:
        rows = (
            await session.scalars(
                select(HandoffRow)
                .where(
                    HandoffRow.tenant_id == str(stack.tenant_id),
                    HandoffRow.state == "requested",
                )
                .order_by(HandoffRow.requested_at.asc())
            )
        ).all()
        assert [HandoffId(row.handoff_id) for row in rows] == [oldest, second]
        assert rows[1].requested_at - rows[0].requested_at == timedelta(seconds=10)
        assert (
            await _event_count(
                session,
                stack.tenant_id,
                "HandoffRequested",
                str(oldest),
            )
            == 1
        )
        assert (
            await _event_count(
                session,
                stack.tenant_id,
                "HandoffRequested",
                str(second),
            )
            == 1
        )
        assert (
            await _count_rows(
                session,
                OutboxEventRow,
                stack.tenant_id,
                "HandoffRequested",
            )
            == 2
        )


async def _prepare_real_scenario(stack: E2EStack) -> E2EScenario:
    scenario_started_at = datetime.now(UTC)
    combinations = [
        ("US", "hinges"),
        ("CA", "fasteners"),
        ("US", "hinges"),
        ("CA", "fasteners"),
        ("US", "hinges"),
    ]
    created = [
        await _post_opportunity(
            stack,
            index,
            country,
            category,
            extracted_at=scenario_started_at,
        )
        for index, (country, category) in enumerate(combinations, start=1)
    ]
    await _assert_created_rows(stack, created, scenario_started_at)

    clock = _Clock(scenario_started_at - timedelta(minutes=5))
    dependencies = build_phase1_dependencies(
        stack.runtime_settings,
        stack.factory,
        now=clock.now,
        secret_resolver=EnvironmentSecretResolver(
            {"UNSUBSCRIBE_HMAC_2026": "u" * 32}
        ),
    )
    boss_scope = OpportunityScope(level=ScopeLevel.TENANT)
    boss_actor = OpportunityActor(
        str(stack.employees.boss),
        boss_scope,
        "boss",
    )
    oldest = await dependencies.opportunities.request_handoff(
        stack.tenant_id,
        _handoff_request(created[0], "e2e-handoff-message-1"),
        actor=boss_actor,
    )
    clock.advance(timedelta(seconds=10))
    second = await dependencies.opportunities.request_handoff(
        stack.tenant_id,
        _handoff_request(created[1], "e2e-handoff-message-2"),
        actor=boss_actor,
    )
    await _assert_handoff_order(stack, oldest, second)
    return E2EScenario(
        opportunity_ids=tuple(
            OpportunityId(item.opportunity_id) for item in created
        ),
        oldest_handoff=oldest,
        second_handoff=second,
        provenance_extracted_at=_serialize_utc(scenario_started_at),
    )


async def _assert_durable_browser_results(
    stack: E2EStack,
    scenario: E2EScenario,
) -> None:
    async with stack.factory() as session:
        transitioned = await session.scalar(
            select(OpportunityRow).where(
                OpportunityRow.tenant_id == str(stack.tenant_id),
                OpportunityRow.opportunity_id == str(scenario.opportunity_ids[0]),
            )
        )
        lost = await session.scalar(
            select(OpportunityRow).where(
                OpportunityRow.tenant_id == str(stack.tenant_id),
                OpportunityRow.opportunity_id == str(scenario.opportunity_ids[2]),
            )
        )
        oldest = await session.scalar(
            select(HandoffRow).where(
                HandoffRow.tenant_id == str(stack.tenant_id),
                HandoffRow.handoff_id == str(scenario.oldest_handoff),
            )
        )
        second = await session.scalar(
            select(HandoffRow).where(
                HandoffRow.tenant_id == str(stack.tenant_id),
                HandoffRow.handoff_id == str(scenario.second_handoff),
            )
        )
        assert transitioned is not None and transitioned.state == "assigned"
        assert lost is not None and lost.state == "lost"
        assert lost.loss_reason == "price_too_high"
        assert lost.died_at_state == "qualified"
        assert lost.closed_by == str(stack.employees.boss)
        assert lost.closed_at is not None
        assert oldest is not None and oldest.state == "accepted"
        assert oldest.accepted_by == str(stack.employees.boss)
        assert second is not None and second.state == "requested"
        assert second.accepted_by is None
        assert (
            await _event_count(
                session,
                stack.tenant_id,
                "OpportunityLost",
                str(scenario.opportunity_ids[2]),
            )
            == 1
        )
        assert (
            await _event_count(
                session,
                stack.tenant_id,
                "HandoffAccepted",
                str(scenario.oldest_handoff),
            )
            == 1
        )


def _logs_are_safe(stack: E2EStack) -> bool:
    database_url = stack.runtime_settings.database_url.get_secret_value()
    password_fragment = urlsplit(database_url).password
    forbidden = [database_url, _CUSTOMER_VERBATIM, "Traceback"]
    if password_fragment:
        forbidden.append(password_fragment)
    contents = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in (
            stack.api_process.stdout_path,
            stack.api_process.stderr_path,
            stack.vite_process.stdout_path,
            stack.vite_process.stderr_path,
        )
    )
    return all(value not in contents for value in forbidden)


async def _expect_provenance_field(
    dialog: Locator,
    *,
    label: str,
    value: str,
) -> None:
    term = dialog.locator("dt", has_text=re.compile(rf"^{re.escape(label)}$"))
    await expect(term).to_have_count(1)
    await expect(term).to_have_text(label)
    await expect(term.locator("xpath=following-sibling::dd")).to_have_text(value)


@pytest.mark.e2e
@pytest.mark.asyncio(loop_scope="session")
async def test_real_opportunity_board_and_handoff_queue(
    e2e_stack: E2EStack,
) -> None:
    scenario = await _prepare_real_scenario(e2e_stack)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await context.new_page()
        browser_errors: list[str] = []
        page.on(
            "console",
            lambda message: (
                browser_errors.append("console:error")
                if message.type == "error"
                else None
            ),
        )
        page.on(
            "pageerror",
            lambda error: browser_errors.append(type(error).__name__),
        )
        try:
            await page.goto(
                f"{e2e_stack.web_origin}/crm/opportunities",
                wait_until="networkidle",
            )
            await expect(
                page.locator('ol[aria-label="机会列表"] > li > button')
            ).to_have_count(5)
            await page.get_by_role("button", name=re.compile("E2E 企业 1")).click()
            provenance_trigger = page.get_by_role(
                "button",
                name="查看来源",
            ).first
            await provenance_trigger.click()
            provenance_dialog = page.get_by_role("dialog")
            for label, value in (
                ("来源类型", "conversation"),
                ("来源标识", "e2e-message-1"),
                ("提取者", "human"),
                ("提取时间", scenario.provenance_extracted_at),
                ("确认人", "尚未确认"),
                ("确认时间", "尚未确认"),
            ):
                await _expect_provenance_field(
                    provenance_dialog,
                    label=label,
                    value=value,
                )
            await page.keyboard.press("Escape")
            await expect(provenance_dialog).to_be_hidden()
            await expect(provenance_trigger).to_be_focused()

            await page.get_by_label("选择合法目标状态").select_option("assigned")
            await page.get_by_role("button", name="推进状态").click()
            await expect(page.get_by_label("机会详情")).to_contain_text("已分配")

            await page.get_by_role("button", name=re.compile("E2E 企业 3")).click()
            await page.locator("summary", has_text="标记为流失").click()
            await page.get_by_label("流失原因").select_option("price_too_high")
            await page.get_by_role("button", name="确认标记流失").click()
            await expect(page.get_by_label("机会详情")).to_contain_text("已流失")

            await page.goto(
                f"{e2e_stack.web_origin}/crm/handoffs",
                wait_until="networkidle",
            )
            cards = page.locator(
                'ol[aria-label="最久等待接管队列"] > li > button'
            )
            await expect(cards).to_have_count(2)
            await expect(cards.first).to_have_attribute(
                "data-handoff-id",
                str(scenario.oldest_handoff),
            )
            await cards.first.click()
            await expect(page.get_by_label("接管包详情")).to_contain_text(
                "E2E 企业 1"
            )
            await page.get_by_role("button", name="接受接管").click()
            await expect(cards).to_have_count(1)
            await expect(cards.first).to_be_focused()
            assert browser_errors == []
        finally:
            await context.close()
            await browser.close()
    await _assert_durable_browser_results(e2e_stack, scenario)
    assert _logs_are_safe(e2e_stack) is True
