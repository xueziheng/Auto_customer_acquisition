"""真实栈验证国家政策候选、独立审批、激活与就绪状态。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from playwright.async_api import Page, async_playwright, expect
from sqlalchemy import text

from tests.e2e.conftest import E2EStack

_COUNTRY = "Synthetic Policy Market"
_DECISION_FIELDS = (
    "public_research_allowed",
    "contact_enrichment_allowed",
    "cold_b2b_email_allowed",
    "personal_data_basis_required",
    "subject_type_affects_judgment",
    "contact_type_affects_judgment",
    "opt_out_deadline_days",
    "local_representative_required",
    "requirements",
)


async def _assert_no_horizontal_overflow(page: Page) -> None:
    metrics = await page.evaluate(
        """() => ({
            viewport: window.innerWidth,
            document: document.documentElement.scrollWidth,
        })"""
    )
    assert metrics["document"] <= metrics["viewport"]


async def _refresh_history_until(page: Page, label: str) -> None:
    deadline = asyncio.get_running_loop().time() + 15
    while asyncio.get_running_loop().time() < deadline:
        await page.get_by_role("button", name="查询国家历史").click()
        try:
            await expect(page.get_by_text(label, exact=True)).to_be_visible(timeout=800)
        except AssertionError:
            await asyncio.sleep(0.2)
        else:
            return
    raise AssertionError(f"国家政策历史未进入预期状态：{label}")


@pytest.mark.e2e
@pytest.mark.asyncio(loop_scope="session")
async def test_real_country_policy_settings_approval_and_activation(
    e2e_stack: E2EStack,
) -> None:
    """生产变更若绕过 scheduler、独立审批或 fail-closed 就绪，本测试必须失败。"""
    assert not e2e_stack.scheduler_task.done(), "真实 scheduler/outbox 循环未运行"
    screenshot_dir_value = os.environ.get("TRADEOS_E2E_SCREENSHOT_DIR")
    screenshot_dir = (
        Path(screenshot_dir_value) if screenshot_dir_value is not None else None
    )
    if screenshot_dir is not None:
        screenshot_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await context.new_page()
        browser_errors: list[str] = []
        hunter_requests: list[str] = []
        page.on(
            "console",
            lambda message: (
                browser_errors.append(f"console:{message.type}")
                if message.type == "error"
                else None
            ),
        )
        page.on("pageerror", lambda error: browser_errors.append(type(error).__name__))
        page.on(
            "request",
            lambda request: (
                hunter_requests.append(request.url)
                if "hunter.io" in request.url.lower()
                else None
            ),
        )
        try:
            await page.goto(f"{e2e_stack.web_origin}/settings", wait_until="networkidle")
            await expect(page.get_by_text("系统不提供国家法律默认值。", exact=True)).to_be_visible()
            await expect(
                page.get_by_label("国家政策包").get_by_text(
                    "尚无任何已激活国家政策，联系人补全保持阻断。",
                    exact=True,
                )
            ).to_be_visible()
            await _assert_no_horizontal_overflow(page)

            await page.get_by_label("国家显示名", exact=True).fill(_COUNTRY)
            for field in (
                "public_research_allowed",
                "contact_enrichment_allowed",
                "cold_b2b_email_allowed",
            ):
                await page.locator(f'select[name="{field}"]').select_option("true")
            for field in (
                "personal_data_basis_required",
                "subject_type_affects_judgment",
                "contact_type_affects_judgment",
                "local_representative_required",
            ):
                await page.locator(f'select[name="{field}"]').select_option("false")
            await page.get_by_label("退订期限（天） opt_out_deadline_days").fill("10")
            await page.get_by_label("附加要求代码 requirements").fill(
                "synthetic_verified_requirement"
            )
            await page.get_by_label("核验说明").fill(
                "Synthetic facts verified by an authorized E2E employee."
            )
            for index, field in enumerate(_DECISION_FIELDS, start=1):
                await page.locator(f'input[name="source_{field}"]').fill(
                    f"assessment:e2e:{index}"
                )

            await page.get_by_role(
                "button", name="提交国家政策审批候选"
            ).click()
            await expect(
                page.get_by_text("国家政策候选已创建，等待审批", exact=True)
            ).to_be_visible()
            await _refresh_history_until(page, "待审批")

            pending = await context.request.get(
                f"{e2e_stack.api_origin}/approvals/pending",
                headers={
                    "X-Tenant-Id": str(e2e_stack.tenant_id),
                    "X-Employee-Id": str(e2e_stack.employees.manager),
                },
            )
            assert pending.status == 200
            approvals = await pending.json()
            country_approvals = [
                item
                for item in approvals
                if item["approval_type"] == "country_policy_change"
                and item["change_set_ref"].startswith("country_policy:")
            ]
            assert len(country_approvals) == 1
            approval_id = country_approvals[0]["approval_id"]
            decided = await context.request.post(
                f"{e2e_stack.api_origin}/approvals/{approval_id}/decide",
                headers={
                    "X-Tenant-Id": str(e2e_stack.tenant_id),
                    "X-Employee-Id": str(e2e_stack.employees.manager),
                },
                data={"decision": "approve"},
            )
            assert decided.status == 200
            assert (await decided.json())["state"] == "approved"

            await _refresh_history_until(page, "已应用")
            await page.get_by_role("button", name="刷新").click()
            active_country = page.get_by_label("生效国家政策").get_by_text(
                _COUNTRY, exact=True
            )
            await expect(active_country).to_be_visible()
            await expect(
                page.get_by_label("国家政策包").get_by_text(
                    (
                        "Hunter / Provider 生产组合尚未完成；即使已有允许政策，"
                        "联系人补全仍保持阻断。"
                    ),
                    exact=True,
                )
            ).to_be_visible()
            await expect(page.get_by_text("生效国家政策 1", exact=True)).to_be_visible()
            await expect(page.get_by_text("允许联系人补全 1", exact=True)).to_be_visible()
            await _assert_no_horizontal_overflow(page)
            if screenshot_dir is not None:
                await page.screenshot(
                    path=str(screenshot_dir / "country-policy-settings-desktop.png"),
                    full_page=True,
                )

            await page.set_viewport_size({"width": 390, "height": 844})
            await _assert_no_horizontal_overflow(page)
            await expect(active_country).to_be_visible()
            if screenshot_dir is not None:
                await page.screenshot(
                    path=str(screenshot_dir / "country-policy-settings-mobile.png"),
                    full_page=True,
                )
            assert browser_errors == []
            assert hunter_requests == []
        finally:
            await context.close()
            await browser.close()

    async with e2e_stack.factory() as session:
        activation_count = await session.scalar(
            text(
                "SELECT count(*) FROM country_policy_activations "
                "WHERE tenant_id=:tenant_id AND approved_by=:approved_by"
            ),
            {
                "tenant_id": str(e2e_stack.tenant_id),
                "approved_by": str(e2e_stack.employees.manager),
            },
        )
        hunter_call_count = await session.scalar(
            text(
                "SELECT count(*) FROM tool_calls "
                "WHERE tenant_id=:tenant_id AND tool_id='contact.enrich'"
            ),
            {"tenant_id": str(e2e_stack.tenant_id)},
        )
        dead_outbox_count = await session.scalar(
            text(
                "SELECT count(*) FROM outbox_events "
                "WHERE tenant_id=:tenant_id AND status='dead'"
            ),
            {"tenant_id": str(e2e_stack.tenant_id)},
        )
    assert activation_count == 1
    assert hunter_call_count == 0
    assert dead_outbox_count == 0
