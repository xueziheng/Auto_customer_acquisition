"""研究 UI 正式跨源验收；真实 HTTP/Chromium，业务数据明确为合成夹具。"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from playwright.async_api import async_playwright, expect

from tests.e2e.conftest import (
    _free_port,
    _minimal_process_env,
    _start_process,
    _wait_for_http,
)
from tests.unit.test_runs_router import TENANT

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.e2e
@pytest.mark.parametrize("width,height", [(1280, 900), (390, 844)])
async def test_research_confirm_refresh_and_radar_tabs_are_operable_across_origins(
    width: int,
    height: int,
) -> None:
    """抓住研究确认/摘要挤压标签、窄屏不可点击与跨源 POST 接线退化。"""
    api_origin = f"http://127.0.0.1:{_free_port()}"
    web_origin = f"http://127.0.0.1:{_free_port()}"
    assert api_origin != web_origin
    with (
        TemporaryDirectory(prefix="tradeos-research-browser-") as temporary,
        (Path(temporary) / "api.stdout").open("wb") as api_out,
        (Path(temporary) / "api.stderr").open("wb") as api_err,
        (Path(temporary) / "web.stdout").open("wb") as web_out,
        (Path(temporary) / "web.stderr").open("wb") as web_err,
    ):
        api = _start_process(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "tests.research_browser_app:app",
                "--host",
                "127.0.0.1",
                "--port",
                api_origin.rsplit(":", 1)[1],
                "--no-access-log",
            ],
            cwd=ROOT,
            env={**_minimal_process_env(), "RESEARCH_TEST_ORIGIN": web_origin},
            stdout=api_out,
            stderr=api_err,
        )
        web = _start_process(
            [
                "node",
                "node_modules/vite/bin/vite.js",
                "--host",
                "127.0.0.1",
                "--port",
                web_origin.rsplit(":", 1)[1],
                "--strictPort",
            ],
            cwd=ROOT / "apps/web",
            env={
                **_minimal_process_env(),
                "VITE_API_BASE_URL": api_origin,
                "VITE_TENANT_ID": str(TENANT),
                "VITE_EMPLOYEE_ID": "emp_test",
            },
            stdout=web_out,
            stderr=web_err,
        )
        try:
            await _wait_for_http(
                f"{api_origin}/openapi.json", api, headers={"X-Tenant-Id": str(TENANT)}
            )
            await _wait_for_http(f"{web_origin}/commands", web)
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch()
                context = await browser.new_context(
                    viewport={"width": width, "height": height}
                )
                page = await context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on(
                    "console",
                    lambda message: (
                        errors.append(message.text)
                        if message.type in {"error", "warning"}
                        else None
                    ),
                )
                await page.goto(f"{web_origin}/commands")
                assert page.url == f"{web_origin}/commands"
                assert await page.title()
                assert await page.evaluate("window.innerWidth") == width
                await expect(
                    page.get_by_role("heading", name="指挥中心", exact=True)
                ).to_be_visible()
                await page.locator("#boss-command").fill(
                    "只研究美国铰链进口商、分销商、电商，最多4个检索式、6页、6信号、3假设。"
                )
                await page.get_by_role("button", name="生成待确认提案").click()
                await expect(page.get_by_label("计划来源方向")).to_contain_text(
                    "公开官网与店铺 / 行业企业名录"
                )
                await expect(page.get_by_label("免费研究账户状态")).to_contain_text(
                    "不发送、不报价"
                )
                await page.get_by_role("button", name="拒绝，不执行").click()
                await expect(
                    page.get_by_text("提案已拒绝；没有启动工作流。", exact=True)
                ).to_be_visible()
                await expect(page.get_by_label("工作流启动回执")).to_have_count(0)
                await page.get_by_role("button", name="新建指令").click()
                await page.get_by_role("button", name="生成待确认提案").click()
                await page.get_by_role("button", name="确认并启动", exact=True).click()
                await expect(page.get_by_label("工作流启动回执")).to_be_visible()
                await page.get_by_role("button", name="刷新提案状态").click()
                await expect(page.get_by_label("工作流启动回执")).to_contain_text(
                    "受限工作流已启动"
                )
                await page.get_by_role(
                    "link", name="查看需求雷达 →", exact=True
                ).click()
                await expect(page.get_by_label("研究执行摘要")).to_contain_text(
                    "尝试次数不等于额度消耗"
                )
                await expect(page.get_by_label("计划来源方向")).to_contain_text(
                    "行业企业名录"
                )
                await expect(page.get_by_label("已搜索来源方向")).to_contain_text(
                    "公开官网与店铺"
                )
                await expect(page.get_by_label("已搜索来源方向")).not_to_contain_text(
                    "行业企业名录"
                )
                await expect(page.get_by_label("已留证来源方向")).not_to_contain_text(
                    "行业企业名录"
                )
                tabs = page.get_by_role("navigation", name="需求雷达数据层")
                for label in ("需求假设", "已验证需求", "需求簇", "需求信号"):
                    button = tabs.get_by_role("button", name=label)
                    await button.scroll_into_view_if_needed()
                    bounds = await button.bounding_box()
                    assert bounds and bounds["width"] > 0 and bounds["height"] >= 24
                    assert 0 <= bounds["x"] and bounds["x"] + bounds["width"] <= width
                    await button.click()
                    await expect(button).to_have_attribute("aria-current", "page")
                await expect(page.get_by_label("研究来源证据")).to_have_count(3)
                await page.get_by_role("button", name="刷新", exact=True).click()
                await expect(page.get_by_label("研究执行摘要")).to_contain_text(
                    "触达入组 0"
                )
                await expect(
                    page.get_by_role("button", name="刷新", exact=True)
                ).to_be_enabled()
                await expect(page.get_by_label("研究来源证据")).to_have_count(3)
                await expect(
                    page.get_by_text("正在读取需求证据链", exact=False)
                ).to_have_count(0)
                await expect(
                    page.get_by_text("正在刷新研究运行摘要", exact=False)
                ).to_have_count(0)
                assert await page.locator("vite-error-overlay").count() == 0
                assert await page.evaluate(
                    "document.documentElement.scrollWidth <= window.innerWidth"
                )
                assert not errors
                screenshot_dir = Path(
                    os.environ.get("TRADEOS_E2E_SCREENSHOTS", temporary)
                )
                screenshot_dir.mkdir(parents=True, exist_ok=True)
                await page.screenshot(
                    path=str(screenshot_dir / f"research-{width}.png")
                )
                await browser.close()
        finally:
            web.stop()
            api.stop()
