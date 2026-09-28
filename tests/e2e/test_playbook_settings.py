"""真实浏览器验证 Company Playbook 候选提交与持久化。"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import pytest
from playwright.async_api import async_playwright, expect
from sqlalchemy import text

from tests.e2e.conftest import E2EStack


@pytest.mark.e2e
@pytest.mark.asyncio(loop_scope="session")
async def test_real_playbook_settings_creates_candidate_and_run(
    e2e_stack: E2EStack,
) -> None:
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
        page.on(
            "console",
            lambda message: (
                browser_errors.append("console:error")
                if message.type == "error"
                else None
            ),
        )
        page.on("pageerror", lambda error: browser_errors.append(type(error).__name__))
        try:
            await page.goto(
                f"{e2e_stack.web_origin}/settings",
                wait_until="networkidle",
            )
            await expect(page.get_by_text("尚未配置公司业务规则")).to_be_visible()
            readiness_message = await page.get_by_label(
                "公司业务规则联系人补全就绪状态"
            ).locator("strong").inner_text()
            assert readiness_message in {
                "尚无任何已激活国家政策，联系人补全保持阻断。",
                "已激活政策均禁止联系人补全，系统不会调用外部 Provider。",
                "部署尚未声明 Hunter 安全配置版本。",
            }
            desktop_metrics = await page.evaluate(
                """() => ({
                    viewport: window.innerWidth,
                    document: document.documentElement.scrollWidth,
                })"""
            )
            assert desktop_metrics["document"] <= desktop_metrics["viewport"]
            if screenshot_dir is not None:
                await page.screenshot(
                    path=str(screenshot_dir / "playbook-settings-desktop.png"),
                    full_page=True,
                )

            await page.get_by_label("公司类型").fill("trading_company")
            await page.get_by_label("最低成交金额").fill("12500.0010")
            await page.get_by_label("币种").fill("USD")
            await page.get_by_label("寻源区域").fill("guangdong, zhejiang")
            await page.get_by_label("排除国家").fill("north korea")
            await page.get_by_role("button", name="提交审批候选").click()

            await expect(page.get_by_text("候选版本已创建")).to_be_visible()
            candidate_id = await page.locator("[data-candidate-id]").get_attribute(
                "data-candidate-id"
            )
            run_id = await page.locator("[data-run-id]").get_attribute("data-run-id")
            assert candidate_id is not None
            assert run_id is not None
            await expect(page.get_by_role("link", name="前往审批中心")).to_be_visible()
            await expect(page.get_by_role("link", name="查看运行记录")).to_have_attribute(
                "href", f"/runs/{run_id}"
            )
            if screenshot_dir is not None:
                await page.locator(".form-message.success").screenshot(
                    path=str(screenshot_dir / "playbook-settings-success.png")
                )
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.locator(".settings-shell").evaluate(
                "element => { element.scrollTop = 0; }"
            )
            mobile_metrics = await page.evaluate(
                """() => ({
                    viewport: window.innerWidth,
                    document: document.documentElement.scrollWidth,
                })"""
            )
            assert mobile_metrics["document"] <= mobile_metrics["viewport"]
            await expect(page.get_by_role("button", name="提交审批候选")).to_be_visible()
            if screenshot_dir is not None:
                await page.screenshot(
                    path=str(screenshot_dir / "playbook-settings-mobile.png"),
                    full_page=True,
                )
            assert browser_errors == []
        finally:
            await context.close()
            await browser.close()

    session = e2e_stack.factory()
    try:
        version_row = (
            await session.execute(
                text(
                    "SELECT minimum_deal_amount, minimum_deal_currency "
                    "FROM company_playbook_versions "
                    "WHERE tenant_id = :tenant_id "
                    "AND playbook_version_id = :version_id"
                ),
                {
                    "tenant_id": str(e2e_stack.tenant_id),
                    "version_id": candidate_id,
                },
            )
        ).one()
        run_row = (
            await session.execute(
                text(
                    "SELECT workflow_type, subject_ref FROM workflow_runs "
                    "WHERE tenant_id = :tenant_id AND run_id = :run_id"
                ),
                {"tenant_id": str(e2e_stack.tenant_id), "run_id": run_id},
            )
        ).one()
    finally:
        await session.close()

    assert version_row.minimum_deal_amount == Decimal("12500.0010")
    assert version_row.minimum_deal_currency == "USD"
    assert run_row.workflow_type == "playbook_change"
    assert run_row.subject_ref == candidate_id
