"""真实准备API的浏览器检查；不截获业务HTTP、不替换领域服务。"""
import asyncio
from pathlib import Path

import uvicorn
from playwright.async_api import async_playwright, expect

from tests.e2e.conftest import (
    _free_port,
    _minimal_process_env,
    _start_process,
    _wait_for_http,
)

ROOT = Path(__file__).resolve().parents[1]


class PreparationBrowserCheck:
    def __init__(self, width, temporary):
        self.width, self.temporary = width, temporary
        self.api_origin = f"http://127.0.0.1:{_free_port()}"
        self.web_origin = f"http://127.0.0.1:{_free_port()}"

    async def check(self, app, tenant, boss, allocation):
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=int(self.api_origin.rsplit(":", 1)[1]), lifespan="off", access_log=False, log_level="error"))
        serving = asyncio.create_task(server.serve())
        with (self.temporary / "web.stdout").open("wb") as out, (self.temporary / "web.stderr").open("wb") as err:
            web = _start_process(["node", "node_modules/vite/bin/vite.js", "--host", "127.0.0.1", "--port", self.web_origin.rsplit(":", 1)[1], "--strictPort"], cwd=ROOT / "apps/web", env={**_minimal_process_env(), "VITE_API_BASE_URL":self.api_origin,"VITE_TENANT_ID":tenant,"VITE_EMPLOYEE_ID":boss}, stdout=out, stderr=err)
            try:
                for _ in range(100):
                    if server.started: break
                    await asyncio.sleep(.05)
                assert server.started
                await _wait_for_http(self.web_origin, web)
                async with async_playwright() as playwright:
                    browser = await playwright.chromium.launch()
                    context = await browser.new_context(viewport={"width":self.width,"height":900})
                    page = await context.new_page()
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    await page.goto(self.web_origin + "/prospects/accounts")
                    picker = page.get_by_label("自动准备批准范围")
                    await expect(picker).to_be_visible()
                    await expect(picker.locator("option")).to_have_count(2)
                    await picker.select_option(index=1)
                    await expect(picker).not_to_have_value("")
                    await page.goto(self.web_origin + "/campaigns")
                    await expect(page.get_by_text("合成准备范围", exact=True).first).to_be_visible()
                    await expect(page.get_by_text("范围已批准", exact=True).first).to_be_visible()
                    await page.goto(self.web_origin + "/customers/" + allocation)
                    progress = page.get_by_label("自动准备进度")
                    await expect(progress).to_contain_text("已验证并入组")
                    await expect(progress).to_contain_text("入组不代表已经发送")
                    await page.reload()
                    await expect(progress).to_contain_text("已验证并入组")
                    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    assert not errors
                    await page.screenshot(path=str(self.temporary / f"preparation-{self.width}.png"), full_page=True)
                    await context.close()
                    await browser.close()
            finally:
                await asyncio.to_thread(web.stop)
                server.should_exit = True
                await asyncio.wait_for(serving, 10)
