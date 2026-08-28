"""T10真实Linux API/worker、三Vite和Chromium；只受控SDK，无HTTP桩。"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest
from playwright.async_api import async_playwright, expect
from pypdf import PdfReader

from tests.e2e.costing_quote_stack import browser_stack


async def fill(page, values):
    for name, value in values.items():
        await page.locator(f'[name="{name}"]').fill(value)


async def action(page, name, path, *, method="POST", status=200):
    """只观察实际HTTP响应，不route、不修改请求或业务数据。"""
    observed = []
    def requested(request):
        if urlsplit(request.url).path == path:
            observed.append("request")
    def failed(request):
        if urlsplit(request.url).path == path:
            observed.append("failed:" + (request.failure or "unknown"))
    page.on("request", requested)
    page.on("requestfailed", failed)
    try:
        async with page.expect_response(
            lambda response: response.url.endswith(path) and response.request.method == method
        ) as pending:
            await page.get_by_role("button", name=name, exact=True).click()
    except Exception:
        print("browser_action_failure=" + name + ";observed=" + repr(observed))
        if name == "定位价格选区":
            print("selection_after_click=" + repr(await page.locator('[name="price-preview"]').evaluate(
                "e => ({start:e.selectionStart,end:e.selectionEnd,length:e.value.length})")))
        raise
    finally:
        page.remove_listener("request", requested)
        page.remove_listener("requestfailed", failed)
    response = await pending.value
    assert response.status == status, (path, response.status, await response.text())
    return await response.json() if status != 204 else None


async def load_opportunity(page, origin, manifest):
    await page.goto(origin + "/costing-quotes")
    await page.locator('[name="opportunity-id"]').fill(manifest["opportunity"])
    await page.get_by_role("button", name="读取成本版本", exact=True).click()
    await expect(page.get_by_role("heading", name="价格与费用证据", exact=True)).to_be_visible()


async def select_excerpt(page, name, excerpt):
    """原生鼠标拖选首行；JS只读取布局/字宽/坐标，不写选区。"""
    area = page.locator(f'[name="{name}"]')
    text = await area.input_value()
    start = text.index(excerpt)
    await page.bring_to_front()
    await area.scroll_into_view_if_needed()
    box = await area.bounding_box()
    metrics = await area.evaluate("""(e, positions) => {
      const style = getComputedStyle(e);
      const measure = new OffscreenCanvas(1, 1).getContext('2d');
      measure.font = style.font;
      const inset = parseFloat(style.borderLeftWidth) + parseFloat(style.paddingLeft);
      return {x: positions.map(i => inset + measure.measureText(e.value.slice(0, i)).width),
        y: parseFloat(style.borderTopWidth) + parseFloat(style.paddingTop) + parseFloat(style.fontSize) / 2,
        width: e.clientWidth};
    }""", [start, start + len(excerpt)])
    assert box is not None and "\n" not in text[:start + len(excerpt)]
    assert metrics["x"][1] < metrics["width"]
    await page.mouse.move(box["x"] + metrics["x"][0], box["y"] + metrics["y"])
    await page.mouse.down()
    await page.mouse.move(box["x"] + metrics["x"][1], box["y"] + metrics["y"], steps=20)
    await page.mouse.up()
    actual = await area.evaluate("e => e.value.slice(e.selectionStart,e.selectionEnd)")
    assert actual == excerpt


async def create_sheet_quote(page, manifest, evidence, locator, *, revision=False):
    """同一DOM工作流确认成本/22项/适用性/报价，不用预制冻结状态。"""
    new = page.locator("article.panel").filter(has=page.get_by_role("heading", name="创建成本表版本"))
    await new.get_by_label("版本类型").select_option("quoted")
    for label, value in (("数量", "50"), ("核算币种", "USD"), ("报价币种", "USD"), ("汇率快照 ID", "controlled-cost-fx")):
        await new.get_by_label(label, exact=True).fill(value)
    sheet = await action(page, "创建新版本", "/costing-quotes/opportunities/" + manifest["opportunity"] + "/cost-sheets", status=201)
    await fill(page, {"item-amount": "2.00", "item-source": evidence["evidence_id"]})
    await page.locator(".item-form").get_by_label("币种", exact=True).fill("USD")
    await action(page, "保存成本项", f'/costing-quotes/cost-sheets/{sheet["cost_sheet_id"]}/items', status=204)
    await page.locator('[name="coverage-mode"]').select_option("detail")
    choices = page.locator('select[name^="coverage-"]').filter(has_not=page.locator('option[value="detail"]'))
    for select in await choices.all():
        name = await select.get_attribute("name")
        kind = name.removeprefix("coverage-")
        await select.select_option("yes" if kind == "product_purchase" else "no")
        await page.locator(f'[name="reason-{kind}"]').fill("受控范围逐项核对")
    assert await choices.count() == 22
    await fill(page, {"binding-evidence-1": evidence["evidence_id"],
                      "binding-line-1": locator, "binding-allocation-1": "order:t10"})
    await action(page, "确认成本适用清单", f'/costing-quotes/cost-sheets/{sheet["cost_sheet_id"]}/coverage')
    until = (datetime.fromisoformat(manifest["now"]) + timedelta(hours=12)).isoformat()
    await fill(page, {"quote-price": "4.00", "quote-currency-exact": "USD", "quote-unit-places": "2",
                      "quote-total-places": "2", "quote-rounding": "ROUND_HALF_UP", "quote-valid-until": until,
                      "scope-note-" + evidence["evidence_id"]: "人工核对规格包装目的地数量适用"})
    await action(page, "确认成本适用性", f'/costing-quotes/cost-sheets/{sheet["cost_sheet_id"]}/scope-confirmations')
    if revision:
        await page.locator('[name="revision-acknowledged"]').check()
    path = ("/costing-quotes/quotes/" + page.url.rsplit("/", 1)[1] + "/revisions") if revision else (
        "/costing-quotes/opportunities/" + manifest["opportunity"] + "/quotes")
    quote = await action(page, "确认修订此版本" if revision else "确认创建新报价", path)
    await expect(page.locator(".internal-quote h3")).to_contain_text(f'指定版本 V{quote["version"]}')
    return quote


async def wait_quote(client, stack, quote_id, state=None):
    manifest = stack.manifest_data
    async with asyncio.timeout(25):
        while True:
            result = await client.get(stack.api_origin + "/costing-quotes/quotes/" + quote_id,
                headers={"X-Tenant-Id": manifest["tenant"], "X-Employee-Id": manifest["actor"]})
            assert result.status_code == 200
            data = result.json()
            if state is not None and data["state"] == state:
                return data
            if state is None:
                packages = await client.get(stack.api_origin + "/approvals/pending",
                    headers={"X-Tenant-Id": manifest["tenant"], "X-Employee-Id": manifest["decider"]})
                assert packages.status_code == 200
                found = [p for p in packages.json() if p["proposed_change_display"].get("报价编号") == quote_id]
                if len(found) == 1:
                    return found[0]
            await asyncio.sleep(0.1)


async def exercise_browser(stack, artifacts):
    """一次真实表单闭环；两个实际版本证明深链不会误选列表首条。"""
    m, origins = stack.manifest_data, stack.web_origins
    async with async_playwright() as playwright, httpx.AsyncClient() as client:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(viewport={"width": 1280, "height": 900}, accept_downloads=True)
        boss, product, decider = [await context.new_page() for _ in range(3)]
        errors = []
        for page in (boss, product, decider):
            page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            await load_opportunity(boss, origins["boss"], m)
            await fill(boss, {"unit-source": m["message"], "unit-value": "pieces"})
            unit_preview = await action(boss, "预览客户消息", "/costing-quotes/evidence/preview")
            assert unit_preview["source_ref"] == "message:" + m["message"]
            await select_excerpt(boss, "unit-preview", "50 pieces")
            await action(boss, "定位客户单位原话", "/costing-quotes/evidence/locator")
            unit = await action(boss, "确认客户单位", f'/costing-quotes/needs/{m["need"]}/unit-confirmations')
            assert unit["source_message_id"] == m["message"] and unit["confirmed_by"] == m["boss"]
            await boss.get_by_text("老板配置：定价政策与本公司抬头", exact=True).click()
            await fill(boss, {"policy-target": "0.20", "policy-minimum": "0.10",
                              "policy-source": m["policy_source"], "policy-effective": m["now"]})
            groups = boss.locator('select[name^="policy-group-"]')
            assert await groups.count() == 22
            for group in await groups.all():
                await group.select_option("goods")
            policy = await action(boss, "老板确认政策", "/costing-quotes/policies")
            assert policy["confirmed_by"] == m["boss"]
            await load_opportunity(product, origins["actor"], m)
            await fill(product, {"price-source": m["source"], "price-page": "1"})
            await action(product, "预览价格原文", "/costing-quotes/evidence/preview")
            preview = product.locator('[name="price-preview"]')
            await product.bring_to_front()
            await preview.focus()
            length = await preview.evaluate("e => e.value.length")
            assert 0 < length < 1024
            await preview.press("Meta+A" if sys.platform == "darwin" else "Control+A")
            selection = await preview.evaluate("e => ({start:e.selectionStart,end:e.selectionEnd,length:e.value.length})")
            assert selection["end"] > selection["start"], selection
            await expect(product.get_by_role("button", name="定位价格选区", exact=True)).to_be_enabled()
            located = await action(product, "定位价格选区", "/costing-quotes/evidence/locator")
            assert m["statement"] in located["excerpt"]
            until = (datetime.fromisoformat(m["now"]) + timedelta(hours=12)).isoformat()
            await fill(product, {"price-amount": "2.00", "price-currency": "USD", "price-unit": "pieces",
                "price-moq": "1", "price-min": "50", "price-max": "50", "price-supplier": "controlled:supplier",
                "price-spec": "hardware steel 50 mm cartons", "price-destination": "DE",
                "price-observed": m["now"], "price-valid": until})
            evidence = await action(product, "确认价格依据", "/costing-quotes/price-evidence")
            assert evidence["confirmed_by"] == m["actor"]
            first = await create_sheet_quote(product, m, evidence, located["locator"])
            # 真修订建立第二版本；旧版仍可深链读取，未直接seed版本列表。
            second = await create_sheet_quote(product, m, evidence, located["locator"], revision=True)
            assert second["replaces_quote_id"] == first["quote_id"]
            await action(product, "提交此版本审批", f'/costing-quotes/quotes/{second["quote_id"]}/submit', status=202)
            pending = await wait_quote(client, stack, second["quote_id"])
            await product.goto(origins["actor"] + "/approvals")
            await expect(product.get_by_role("button", name="批准此精确变更", exact=True)).to_have_count(0)
            denied = await client.post(stack.api_origin + f'/approvals/{pending["approval_id"]}/decide',
                headers={"X-Tenant-Id": m["tenant"], "X-Employee-Id": m["actor"]}, json={"decision": "approve"})
            assert denied.status_code == 403
            await decider.goto(origins["decider"] + "/approvals")
            await expect(decider.get_by_role("button", name="批准此精确变更", exact=True)).to_be_visible()
            await decider.screenshot(path=str(artifacts / "approval-1280.png"), full_page=True)
            await action(decider, "批准此精确变更", f'/approvals/{pending["approval_id"]}/decide')
            approved = await wait_quote(client, stack, second["quote_id"], "approved")
            await boss.goto(origins["boss"] + "/costing-quotes/quotes/" + second["quote_id"])
            file = await action(boss, "请求生成客户文件（后端最终授权）", f'/costing-quotes/quotes/{second["quote_id"]}/files')
            async with boss.expect_download() as download_event:
                await boss.get_by_role("button", name="下载当前文件", exact=True).last.click()
            download = await download_event.value
            pdf_path = artifacts / "approved-quote.pdf"
            await download.save_as(pdf_path)
            content = pdf_path.read_bytes()
            assert hashlib.sha256(content).hexdigest() == file["content_hash"]
            pdf = PdfReader(io.BytesIO(content))
            text = "\n".join(page.extract_text() for page in pdf.pages)
            assert "200.00" in text and "USD" in text and m["statement"] not in text
            assert approved["calculation"]["displayed_total"]["amount"] == "200.00"
            for width, height in ((1280, 900), (390, 844)):
                await boss.set_viewport_size({"width": width, "height": height})
                await boss.goto(origins["boss"] + "/costing-quotes/quotes/" + first["quote_id"])
                await expect(boss.locator(".internal-quote h3")).to_contain_text("指定版本 V1")
                await boss.reload()
                await expect(boss.locator(".internal-quote h3")).to_contain_text("指定版本 V1")
                assert first["quote_id"] in await boss.locator(".internal-quote").inner_text()
                await boss.goto(origins["boss"] + "/costing-quotes/quotes/" + second["quote_id"])
                await expect(boss.locator(".internal-quote h3")).to_contain_text("指定版本 V2 · 已批准")
                assert await boss.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                assert await boss.locator(".costing-shell").evaluate("e => e.scrollWidth <= e.clientWidth")
                await boss.screenshot(path=str(artifacts / f"quote-{width}.png"), full_page=True)
            assert not errors
            result = {**m, "origins": origins, "api_origin": stack.api_origin,
                      "quote_id": second["quote_id"], "older_quote_id": first["quote_id"],
                      "quote_url": origins["boss"] + "/costing-quotes/quotes/" + second["quote_id"],
                      "file": file, "pdf_pages": len(pdf.pages), "pdf_metadata": str(pdf.metadata),
                      "pdf_sha256": hashlib.sha256(content).hexdigest(), "image": stack.image}
            (artifacts / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
            return result
        finally:
            await browser.close()


@pytest.mark.e2e
async def test_real_costing_quote_browser():
    artifacts = Path(__file__).resolve().parents[2] / "output/playwright" / ("t10-" + uuid4().hex)
    async with browser_stack(artifacts) as stack:
        await exercise_browser(stack, artifacts)
    print("T10浏览器产物：" + str(artifacts))
