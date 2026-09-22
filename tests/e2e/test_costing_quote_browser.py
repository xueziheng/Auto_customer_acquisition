"""T10真实Linux API/worker、三Vite和Chromium；只受控SDK，无HTTP桩。"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import socket
import sys
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import httpx
import pytest
from docker.errors import NotFound
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright, expect
from pypdf import PdfReader

from tests.e2e.costing_quote_lifecycle import run_supervised


async def fill(page, values):
    for name, value in values.items():
        await page.locator(f'[name="{name}"]').fill(value)


async def action(page, name, path, *, method="POST", status=200):
    """只观察实际HTTP响应，不route、不修改请求或业务数据。"""
    observed = []
    responses = []
    def requested(request):
        if urlsplit(request.url).path == path:
            observed.append("request")
    def failed(request):
        if urlsplit(request.url).path == path:
            observed.append("failed:" + (request.failure or "unknown"))
    def responded(response):
        if urlsplit(response.url).path == path:
            responses.append(response.status)
    page.on("request", requested)
    page.on("requestfailed", failed)
    page.on("response", responded)
    try:
        async with page.expect_response(
            lambda response: response.url.endswith(path) and response.request.method == method
        ) as pending:
            await page.get_by_role("button", name=name, exact=True).click()
    except Exception:
        if name == "确认客户单位":
            print("t10_unit_diag_requests=" + str(observed.count("request")), flush=True)
            print("t10_unit_diag_failed=" + str(sum(item.startswith("failed:") for item in observed)), flush=True)
            print("t10_unit_diag_responses=" + str(len(responses)), flush=True)
            try:
                async with asyncio.timeout(1):
                    disabled = await page.get_by_role("button", name=name, exact=True).is_disabled()
                    selected = await page.locator('[name="unit-preview"]').evaluate(
                        "e => [e.selectionStart, e.selectionEnd, e.value.length]")
                    print("t10_unit_diag_disabled=" + str(int(disabled)), flush=True)
                    for key, value in zip(("selection_start", "selection_end", "text_length"), selected, strict=True):
                        print("t10_unit_diag_" + key + "=" + str(value), flush=True)
            except (TimeoutError, PlaywrightError):
                pass
        else:
            print("browser_action_failure=" + name + ";observed=" + repr(observed))
        if name == "定位价格选区":
            print("selection_after_click=" + repr(await page.locator('[name="price-preview"]').evaluate(
                "e => ({start:e.selectionStart,end:e.selectionEnd,length:e.value.length})")))
        raise
    finally:
        page.remove_listener("request", requested)
        page.remove_listener("requestfailed", failed)
        page.remove_listener("response", responded)
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
    await expect(page.locator('[name="opportunity-id"]')).to_have_value(manifest["opportunity"])
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
            submitted = await action(product, "提交此版本审批", f'/costing-quotes/quotes/{second["quote_id"]}/submit', status=202)
            pending = await wait_quote(client, stack, second["quote_id"])
            run_link = product.get_by_role("link", name="查看本次报价审批 Run", exact=True)
            await expect(run_link).to_have_attribute("href", "/runs?run=" + submitted["run_id"])
            await run_link.click()
            await expect(product.get_by_text("只有老板可以查看 Run 审计记录", exact=False).first).to_be_visible()
            await boss.goto(origins["boss"] + "/runs?run=" + submitted["run_id"])
            approval_link = boss.get_by_role("link", name=pending["approval_id"], exact=True)
            await expect(approval_link).to_have_attribute("href", "/approvals?approval_id=" + pending["approval_id"])
            await approval_link.click()
            await expect(boss.locator(".approval-packet")).to_contain_text(pending["approval_id"])
            await product.goto(origins["actor"] + "/approvals")
            await expect(product.get_by_role("button", name="批准此精确变更", exact=True)).to_have_count(0)
            denied = await client.post(stack.api_origin + f'/approvals/{pending["approval_id"]}/decide',
                headers={"X-Tenant-Id": m["tenant"], "X-Employee-Id": m["actor"]}, json={"decision": "approve"})
            assert denied.status_code == 403
            await decider.goto(origins["decider"] + "/approvals?approval_id=" + pending["approval_id"])
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
            for width, height in ((1440, 1000), (390, 844)):
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
                cost_link = boss.get_by_role("link", name="核对此报价的成本表与需求依据", exact=True)
                await expect(cost_link).to_have_attribute("href", "/costing-quotes?opportunity_id=" + m["opportunity"] + "&cost_sheet_id=" + second["cost_sheet_id"])
                await cost_link.click()
                await expect(boss.locator(".item-panel")).to_contain_text(second["cost_sheet_id"])
                print("t10_relay_reload_cost=1", flush=True)
                await boss.reload()
                await expect(boss.locator(".item-panel")).to_contain_text(second["cost_sheet_id"])
                await boss.locator(".item-panel").scroll_into_view_if_needed()
                await boss.screenshot(path=str(artifacts / f"exact-cost-{width}.png"), full_page=False)
                await boss.goto(origins["boss"] + "/runs?run=" + submitted["run_id"])
                await expect(boss.get_by_role("link", name=pending["approval_id"], exact=True)).to_be_visible()
                await boss.get_by_role("link", name=pending["approval_id"], exact=True).scroll_into_view_if_needed()
                await boss.screenshot(path=str(artifacts / f"run-approval-{width}.png"), full_page=False)
            assert not errors
            result = {**m, "origins": origins, "api_origin": stack.api_origin,
                      "quote_id": second["quote_id"], "older_quote_id": first["quote_id"],
                      "run_id": submitted["run_id"], "approval_id": pending["approval_id"],
                      "quote_url": origins["boss"] + "/costing-quotes/quotes/" + second["quote_id"],
                      "file": file, "pdf_pages": len(pdf.pages), "pdf_metadata": str(pdf.metadata),
                      "pdf_sha256": hashlib.sha256(content).hexdigest(), "image": stack.image}
            (artifacts / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
            return result
        except Exception:
            for label, page in (("boss", boss), ("product", product), ("decider", decider)):
                try:
                    await page.screenshot(path=str(artifacts / f"failure-{label}.png"), timeout=2000)
                except PlaywrightError:
                    pass
            raise
        finally:
            await browser.close()


@pytest.mark.e2e
async def test_real_costing_quote_browser():
    result = await run_supervised("browser")
    assert result.code == 0, result.output
    assert result.cleanup_verified
    print(result.output)
    print("T10浏览器产物：" + str(result.artifacts))


@pytest.mark.e2e
@pytest.mark.parametrize("failure", ["runtime_kill", "host_stall"])
async def test_real_killed_linux_runtime_fails_and_cleans_only_owned_stack(monkeypatch, failure):
    """仅测试边界替换固定子入口；真实被杀API不能被退出门当作成功。"""
    from tests.e2e import costing_quote_lifecycle as lifecycle

    script = """
import signal, sys, time
from pathlib import Path
from tests.e2e import costing_quote_lifecycle as lifecycle
from tests.e2e.costing_quote_stack import linux_stack
owner, work, finish, parent, failure = sys.argv[1:]
lifecycle._ACTIVE = lifecycle.OwnedRun(owner, float(work), float(finish),
    lifecycle.OUTPUT_ROOT / ('t10-' + owner), int(parent))
stage_path = lifecycle._ACTIVE.artifacts / 'runtime-stage.json'
def record_stage(stage):
    stage_path.write_text('{"stage":"' + stage + '"}')
record_stage('child_started')
try:
    with linux_stack(mode='browser') as stack:
        record_stage('stack_ready')
        stack.runner.reload()
        assert stack.runner.attrs['Name'] == '/' + lifecycle.resource_names(owner)['api']
        assert stack.runner.attrs['Config']['Labels'][lifecycle.OWNER_LABEL] == owner
        record_stage('owner_verified')
        if failure == 'host_stall':
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            print('fixed_exception_type=HostStalledAfterApiCreate', flush=True)
            time.sleep(60)
        stack.runner.kill(signal='SIGKILL')
        exit_code = stack.runner.wait(timeout=10)['StatusCode']
        assert exit_code == 137
        record_stage('exit137')
        (lifecycle._ACTIVE.artifacts / 'runtime-kill.json').write_text(
            '{"owned":true,"exit_code":137}')
        print('fixed_exception_type=RuntimeKilled137', flush=True)
except AssertionError:
    raise SystemExit(2)
print('t10_child_exit=verified', flush=True)
"""

    def command(mode, owner, work, finish):
        import os
        assert mode == "browser"
        return [sys.executable, "-c", script, owner, str(work), str(finish), str(os.getpid()), failure]

    monkeypatch.setattr(lifecycle, "_child_command", command)
    if failure == "host_stall":
        monkeypatch.setattr(lifecycle, "TOTAL_SECONDS", {**lifecycle.TOTAL_SECONDS, "browser": 35})
        monkeypatch.setattr(lifecycle, "CLEANUP_SECONDS", 12)
        monkeypatch.setattr(lifecycle, "TERM_SECONDS", .3)
    sentinel = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(90)", start_new_session=True,
    )
    try:
        result = await run_supervised("browser")
        assert result.code != 0 and not result.cleanup_verified, result.output
        assert "t10_cleanup=unknown" in result.output
        expected_stage = "exit137" if failure == "runtime_kill" else "owner_verified"
        stage_receipt = result.artifacts / "runtime-stage.json"
        assert stage_receipt.exists(), result.output
        assert json.loads(stage_receipt.read_text()) == {"stage": expected_stage}
        if failure == "runtime_kill":
            runtime_receipt = result.artifacts / "runtime-kill.json"
            assert runtime_receipt.exists(), result.output
            assert json.loads(runtime_receipt.read_text()) == {
                "owned": True, "exit_code": 137,
            }
        assert ("fixed_exception_type=" + (
            "RuntimeKilled137" if failure == "runtime_kill" else "HostStalledAfterApiCreate"
        )) in result.output
        if failure == "host_stall":
            assert "t10_deadline=expired" in result.output
        assert "t10_child_exit=verified" not in result.output
        lifecycle_record = json.loads((result.artifacts / "lifecycle.json").read_text())
        assert sentinel.pid not in {row["pid"] for row in lifecycle_record["processes"]}
        assert sentinel.returncode is None
        os.kill(sentinel.pid, 0)
        for row in lifecycle_record["processes"]:
            with pytest.raises(ProcessLookupError):
                os.kill(row["pid"], 0)
        for port in json.loads((result.artifacts / "ports.json").read_text()):
            with socket.socket() as probe:
                assert probe.connect_ex(("127.0.0.1", port)) != 0
        client = __import__("docker").from_env()
        try:
            names = lifecycle.resource_names(lifecycle_record["owner"])
            for key in ("api", "pg"):
                with pytest.raises(NotFound):
                    client.containers.get(names[key])
            with pytest.raises(NotFound):
                client.networks.get(names["network"])
        finally:
            client.close()
        print(result.output)
        print("t10_independent_sentinel=alive;failure=" + failure)
    finally:
        if sentinel.returncode is None:
            sentinel.terminate()
        await asyncio.wait_for(sentinel.wait(), 2)
        with pytest.raises(ProcessLookupError):
            os.kill(sentinel.pid, 0)


async def probe_initial_unit_read(stack, _artifacts):
    """真实GET发出即输入，读取仍完成且能确认；不route/延迟/写内部状态。"""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        manifest = stack.manifest_data
        path = f'/costing-quotes/needs/{manifest["need"]}/unit'
        responses = []

        def response_seen(response):
            if urlsplit(response.url).path == path:
                responses.append(True)

        page.on("response", response_seen)
        try:
            await page.goto(stack.web_origins["boss"] + "/costing-quotes")
            await page.locator('[name="opportunity-id"]').fill(manifest["opportunity"])
            async with page.expect_request(lambda request: urlsplit(request.url).path == path) as pending:
                await page.get_by_role("button", name="读取成本版本", exact=True).click()
            request = await pending.value
            print("t10_unit_probe_initial_response=" + str(int(bool(responses))), flush=True)
            assert not responses, "真实探针未捕获在途初始GET"
            await fill(page, {"unit-source": manifest["message"], "unit-value": "pieces"})
            async with asyncio.timeout(5):
                response = await request.response()
            panel = page.locator("section.panel").filter(has=page.get_by_role("heading", name="客户单位确认", exact=True))
            assert response is not None and response.status == 200
            prepared = await response.json()
            await expect(panel.get_by_text("数量 hash", exact=False)).to_be_visible()
            summary = await panel.get_by_text("数量 hash", exact=False).is_visible()
            await action(page, "预览客户消息", "/costing-quotes/evidence/preview")
            await select_excerpt(page, "unit-preview", "50 pieces")
            await action(page, "定位客户单位原话", "/costing-quotes/evidence/locator")
            button = panel.get_by_role("button", name="确认客户单位", exact=True)
            await expect(button).to_be_enabled()
            disabled = await button.is_disabled()
            print("t10_unit_probe_failed=" + str(int(request.failure is not None)), flush=True)
            print("t10_unit_probe_response=" + str(int(response is not None)), flush=True)
            print("t10_unit_probe_summary_visible=" + str(int(summary)), flush=True)
            print("t10_unit_probe_disabled=" + str(int(disabled)), flush=True)
            assert request.failure is None and summary and not disabled
            confirmed = await action(page, "确认客户单位", f'/costing-quotes/needs/{manifest["need"]}/unit-confirmations')
            assert confirmed["quantity_fact_hash"] == prepared["quantity_fact_hash"]
        finally:
            await browser.close()


@pytest.mark.e2e
async def test_real_initial_unit_read_probe(monkeypatch):
    from tests.e2e import costing_quote_lifecycle as lifecycle

    original = lifecycle._child_command
    script = """
from tests.e2e import test_costing_quote_browser as browser_case
from tests.e2e.costing_quote_lifecycle import main
browser_case.exercise_browser = browser_case.probe_initial_unit_read
raise SystemExit(main())
"""

    def command(mode, owner, work, finish):
        return [sys.executable, "-c", script, *original(mode, owner, work, finish)[3:]]

    monkeypatch.setattr(lifecycle, "_child_command", command)
    result = await run_supervised("browser")
    assert result.code == 0 and result.cleanup_verified, result.output
    print(result.output)
