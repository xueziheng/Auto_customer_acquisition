"""最终浏览器验收反向消费原launcher，研究与触达严格分开。"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import psutil
import pytest
from playwright.async_api import async_playwright, expect
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from apps.api.composition.runtime import build_phase1_dependencies
from apps.api.controlled import CONTROLLED_RESEARCH_MESSAGE, ControlledModelClient
from apps.api.runtime_config import Phase1RuntimeSettings
from infra.controlled.config import ControlledConfig
from infra.controlled.contacts import ControlledContactProvider
from infra.controlled.providers import ControlledGmailTransport
from infra.controlled.reply_model import ControlledReplyModelClient
from infra.db.session import create_engine_from
from infra.db.tables import EmployeeRow
from shared.schemas.email_inbound import InboundRoute
from shared.schemas.identifiers import TenantId
from tests.e2e.web_core_contacts import prepare_verified_send
from tests.unit.test_email_inbound import mime

pytestmark = pytest.mark.e2e
ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "output/acceptance/task12"


async def eventually(operation, predicate, *, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = await operation()
        if predicate(value):
            return value
        await asyncio.sleep(0.25)
    raise AssertionError("受控状态未在期限内达到预期")


async def approve(client, path, ids):
    versions = await eventually(
        lambda: client.get(path),
        lambda r: (
            r.status_code == 200
            and any(
                x["approval_id"] and x["approval_state"] == "pending" for x in r.json()
            )
        ),
    )
    approval = next(
        x["approval_id"]
        for x in versions.json()
        if x["approval_id"] and x["approval_state"] == "pending"
    )
    assert (
        await client.post(f"/approvals/{approval}/decide", json={"decision": "approve"})
    ).status_code == 400
    response = await client.post(
        f"/approvals/{approval}/decide",
        headers={"X-Employee-Id": ids[1]["employee_id"]},
        json={"decision": "approve"},
    )
    assert response.status_code == 200
    replay = await client.post(
        f"/approvals/{approval}/decide",
        headers={"X-Employee-Id": ids[1]["employee_id"]},
        json={"decision": "approve"},
    )
    assert replay.status_code == 200
    return approval


async def screenshot(page, directory, name, evidence):
    assert await page.locator("vite-error-overlay").count() == 0
    assert await page.evaluate(
        "document.documentElement.scrollWidth <= window.innerWidth"
    )
    await page.screenshot(path=str(directory / (name + ".png")), full_page=True)
    evidence.append(
        {
            "name": name,
            "path": str(directory / (name + ".png")),
            "url": page.url,
            "width": page.viewport_size["width"],
        }
    )


async def register_sender(page, web):
    await page.goto(web + "/crm/sending-identities")
    await page.get_by_label("发件地址", exact=True).fill(
        "task12@tradeos-controlled.example.com"
    )
    await page.get_by_label("发件域名", exact=True).fill(
        "tradeos-controlled.example.com"
    )
    await page.get_by_role("button", name="登记发件身份", exact=True).click()
    await page.get_by_role("button", name="确认登记", exact=True).click()
    await expect(page.get_by_text("已核对登记身份，请继续认证检查")).to_be_visible(
        timeout=15000
    )
    await page.get_by_role(
        "button", name="对 tradeos-controlled.example.com 重新检查认证"
    ).click()
    await expect(
        page.get_by_text("认证检查已提交；请刷新读取实际认证结果")
    ).to_be_visible(timeout=15000)
    for _ in range(30):
        await page.get_by_role("button", name="刷新状态", exact=True).click()
        await asyncio.sleep(0.5)
        if await page.get_by_text("SPF 通过", exact=True).count():
            break
    await expect(page.get_by_text("SPF 通过", exact=True)).to_be_visible()
    await page.get_by_label("预热目标日量", exact=True).fill("15")
    await page.get_by_role("button", name="启动预热", exact=True).click()
    await page.get_by_role("button", name="确认启动预热", exact=True).click()
    await expect(
        page.get_by_text("预热已启动，日期与每日限额由原域固定曲线决定")
    ).to_be_visible(timeout=15000)
    await page.get_by_role("button", name="绑定本机入站邮箱", exact=True).click()
    await page.get_by_role("button", name="确认绑定", exact=True).click()
    await expect(page.get_by_text("入站绑定已记录；处理状态待核对")).to_be_visible(
        timeout=15000
    )


async def test_original_launcher_browser_research_and_independent_reply_chain(
    tmp_path, monkeypatch
):
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    install_path = EVIDENCE / "install.json"
    interpreter = (
        json.loads(install_path.read_text())["environment"] + "/bin/python3"
        if install_path.exists()
        else sys.executable
    )
    process = await asyncio.to_thread(
        subprocess.Popen,
        [
            interpreter,
            str(ROOT / "scripts/run_web_core_controlled.py"),
            "--directory",
            str(tmp_path),
        ],
        cwd=ROOT,
        env={
            "PATH": str(Path(interpreter).parent) + ":" + os.environ["PATH"],
            "PYTHON_DOTENV_DISABLED": "1",
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    engine = deps = browser = None
    state = {}
    pages = []
    try:

        async def read_ready():
            paths = list(tmp_path.glob("tradeos-controlled-*/status.json"))
            assert process.poll() is None, "原launcher启动失败；只读取安全status"
            return json.loads(paths[0].read_text()) if paths else {}

        state = await eventually(
            read_ready, lambda v: v.get("status") == "ready", timeout=120
        )
        directory = EVIDENCE / state["owner"]
        directory.mkdir()
        private = tmp_path / ("tradeos-controlled-" + state["owner"])
        assert (
            psutil.Process(state["supervisor"]["pid"]).create_time()
            == state["supervisor"]["born"]
        )
        config = ControlledConfig.read(private / "config.json")
        assert config.owner == state["owner"]
        (directory / "startup.json").write_text(
            json.dumps(state, ensure_ascii=False, indent=2)
        )
        engine = create_engine_from(config.database_url.get_secret_value())
        factory = async_sessionmaker(engine, expire_on_commit=False)
        provider = ControlledGmailTransport(
            private / "mail.sqlite", tenant_id=config.tenant_id
        )
        deps = build_phase1_dependencies(
            Phase1RuntimeSettings.from_environ(config.runtime_environment()),
            factory,
            now=lambda: datetime.now(UTC),
            secret_resolver=config,
            gmail_transport=provider,
            model_client=ControlledModelClient(),
        )
        ids = state["identities"]
        async with (
            httpx.AsyncClient(
                base_url=state["api_url"],
                headers={
                    "X-Tenant-Id": state["tenant_id"],
                    "X-Employee-Id": ids[0]["employee_id"],
                },
                trust_env=False,
                timeout=15,
            ) as client,
            async_playwright() as pw,
        ):
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(type(error).__name__))
            await page.goto(state["web_url"] + "/settings")
            await expect(
                page.get_by_text("尚未配置 Company Playbook", exact=True)
            ).to_be_visible()
            await screenshot(page, directory, "settings-empty-1440", pages)
            from infra.controlled.research import ControlledResearchCalls

            research_calls = ControlledResearchCalls(private / "mail.sqlite", tenant_id=config.tenant_id)
            unconfigured = await client.post(
                "/commands/discovery-proposals", json={"message": CONTROLLED_RESEARCH_MESSAGE}
            )
            assert unconfigured.status_code == 200
            queued = await client.post("/commands/discovery-proposals/" + unconfigured.json()["proposal_id"] + "/confirm")
            assert queued.status_code == 200
            rejected_run = await eventually(
                lambda: client.get("/runs/" + queued.json()["run_id"]),
                lambda r: r.status_code == 200 and r.json()["summary"]["status"] == "failed",
            )
            assert (await client.get("/settings/playbook")).json()["configured"] is False
            assert (await client.get("/settings/country-policies")).json()["active_policies"] == []
            for route in ("/demand/signals", "/demand/hypotheses", "/crm/campaigns"):
                assert (await client.get(route)).json() == []
            assert not any(call in {"research.search", "research.page", "research.model"} for call in research_calls.list_calls())
            assert not any(call.operation == "send" for call in await provider.list_calls())
            (directory / "research-unconfigured.json").write_text(json.dumps({
                "run_id": queued.json()["run_id"], "status": rejected_run.json()["summary"]["status"],
                "calls": research_calls.list_calls(), "signals": 0, "hypotheses": 0,
                "campaigns": 0, "send_calls": 0,
            }, indent=2))

            for name, value in [
                ("company_type", "trading_company"),
                ("minimum_deal_amount", "1000"),
                ("minimum_deal_currency", "USD"),
                ("monthly_budget_credits", "3"),
                ("sourcing_regions", "controlled"),
            ]:
                await page.locator(f'[name="{name}"]').fill(value)
            await page.locator(
                '[aria-label="Playbook 候选表单"] button[type="submit"]'
            ).click()
            await expect(page.get_by_text("候选版本已创建", exact=True)).to_be_visible(
                timeout=15000
            )
            await approve(client, "/settings/playbook/versions", ids)
            await eventually(
                lambda: client.get("/settings/playbook"),
                lambda r: r.status_code == 200 and r.json()["configured"],
            )
            policy = {
                "country": "KE",
                "public_research_allowed": True,
                "contact_enrichment_allowed": False,
                "cold_b2b_email_allowed": False,
                "personal_data_basis_required": True,
                "subject_type_affects_judgment": True,
                "contact_type_affects_judgment": True,
                "opt_out_deadline_days": 1,
                "local_representative_required": False,
                "requirements": ["honor_opt_out"],
                "notes": "合成政策，不代表真实法律判断。",
            }
            policy["field_sources"] = {
                key: {
                    "source_type": "employee_input",
                    "source_id": "task12-controlled:" + key,
                }
                for key in policy
                if key not in {"country", "notes"}
            }
            assert (
                await client.post(
                    "/settings/country-policies/proposals",
                    headers={"Idempotency-Key": "task12-policy"},
                    json=policy,
                )
            ).status_code == 202
            await approve(client, "/settings/country-policies/versions?country=KE", ids)
            await eventually(
                lambda: client.get("/settings/country-policies"),
                lambda r: (
                    r.status_code == 200 and len(r.json()["active_policies"]) == 1
                ),
            )
            await page.goto(state["web_url"] + "/commands")
            await page.locator("#boss-command").fill(CONTROLLED_RESEARCH_MESSAGE)
            await page.get_by_role("button", name="生成待确认提案", exact=True).click()
            confirm_button = page.get_by_role("button", name="确认并启动", exact=True)
            await expect(confirm_button).to_be_visible(timeout=15000)
            research_access = (await client.get("/settings/research")).json()
            (directory / "research-access.json").write_text(
                json.dumps(research_access, ensure_ascii=False)
            )
            research_ready = await confirm_button.is_enabled()
            if research_ready:
                async with page.expect_response(
                    lambda r: "/confirm" in r.url and r.request.method == "POST"
                ) as confirmed:
                    await confirm_button.click()
                confirmation = await (await confirmed.value).json()
                (directory / "research-confirmation.json").write_text(
                    json.dumps(confirmation, ensure_ascii=False)
                )
                hypotheses = await eventually(
                    lambda: client.get("/demand/hypotheses"),
                    lambda r: r.status_code == 200 and len(r.json()) >= 3,
                )
                assert len((await client.get("/demand/signals")).json()) == 3
                assert len(hypotheses.json()) == 3
            assert (await client.get("/crm/campaigns")).json() == []
            assert not any(c.operation == "send" for c in await provider.list_calls())
            await screenshot(page, directory, "research-state-1440", pages)
            await register_sender(page, state["web_url"])
            senders = (await client.get("/crm/sending-identities")).json()
            sender_id = next(
                x["identity_id"] for x in senders if x["address"].startswith("task12@")
            )
            runtime = {
                "config": config,
                "factory": factory,
                "deps": deps,
                "provider": provider,
                "staff": config.identities,
                "sender_prepared": True,
                "route": InboundRoute(
                    tenant_id=TenantId(config.tenant_id),
                    mailbox_alias="primary",
                    configured_identity_id=sender_id,
                    route_id="controlled",
                    config_version="v1",
                ),
            }
            contact_provider = ControlledContactProvider(
                private / "mail.sqlite",
                tenant_id=config.tenant_id,
                now=lambda: datetime.now(UTC),
            )
            assert contact_provider.list_calls() == ()
            policy["contact_enrichment_allowed"] = True
            policy["cold_b2b_email_allowed"] = True
            assert (
                await client.post(
                    "/settings/country-policies/proposals",
                    headers={"Idempotency-Key": "task12-policy-outreach"},
                    json=policy,
                )
            ).status_code == 202
            await approve(client, "/settings/country-policies/versions?country=KE", ids)
            await prepare_verified_send(runtime, client, eventually)
            assert contact_provider.list_calls() == ("contact.enrich", "contact.verify")
            assert sum(c.operation == "send" for c in await provider.list_calls()) == 1
            body = "We need hinges for cabinet doors. We need 5000 units at USD 2 per unit."
            fields = [
                {
                    "field": "product_category",
                    "value": "hinges",
                    "quote": "We need hinges",
                },
                {
                    "field": "application",
                    "value": "cabinet doors",
                    "quote": "for cabinet doors",
                },
                {"field": "quantity", "value": "5000", "quote": "5000 units"},
                {
                    "field": "target_price",
                    "value": '{"amount":"2","currency":"USD"}',
                    "quote": "USD 2 per unit",
                },
            ]
            model = ControlledReplyModelClient(
                private / "reply-model.sqlite", tenant_id=config.tenant_id
            )
            model.set_response(
                {"subject": "(current reply)", "body": body},
                json.dumps(
                    {"category": "provides_specification", "candidate_fields": fields}
                ),
            )
            raw = mime(
                body=body,
                message_id="<task12-filled@example.test>",
                reply=runtime["outbound"],
                date=datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S +0000"),
            )
            await provider.receive_inbound(raw, internal_date=datetime.now(UTC))
            handoffs = await eventually(
                lambda: client.get("/crm/handoffs"),
                lambda r: r.status_code == 200 and len(r.json()) == 1,
                timeout=60,
            )
            handoff_id = handoffs.json()[0]["handoff_id"]
            packet = (await client.get("/crm/handoffs/" + handoff_id)).json()
            opportunity_id = packet["opportunity_id"]
            opportunity = (
                await client.get("/crm/opportunities/" + opportunity_id)
            ).json()
            need_id = opportunity["need_id"]
            conversations = (await client.get("/inbox/conversations")).json()
            conversation_id = next(
                x["conversation_id"]
                for x in conversations
                if x["account_id"] == opportunity["account_id"]
            )
            detail = (
                await client.get("/inbox/conversations/" + conversation_id)
            ).json()
            message_id = next(
                x["message_id"]
                for x in detail["messages"]
                if x["effective_category"] == "provides_specification"
            )
            proof = {
                "owner": state["owner"],
                "need_id": need_id,
                "opportunity_id": opportunity_id,
                "handoff_id": handoff_id,
                "conversation_id": conversation_id,
                "message_id": message_id,
                "research_ready": research_ready,
                "research_signals": 3 if research_ready else 0,
                "research_hypotheses": 3 if research_ready else 0,
                "research_external_calls": research_calls.list_calls(),
                "send_calls": 1,
                "contact_calls": list(contact_provider.list_calls()),
                "contact_run_id": str(runtime["contact_run_id"]),
                "contact_point_id": str(runtime["contact_point_id"]),
                "verification_provider": runtime["verification_provider"],
                "verification_status": runtime["verification_status"],
                "unverified_enrollment_denied": True,
                "model_calls": model.call_count(),
                "quotation": "not_configured_on_mac",
            }
            # 重放外部同消息并重启四应用；PG仍是唯一业务状态来源。
            await provider.receive_inbound(raw, internal_date=datetime.now(UTC))
            initial_pids = {x["pid"] for x in state["processes"]}
            process.send_signal(signal.SIGHUP)
            await eventually(
                read_ready,
                lambda s: (
                    s.get("status") == "ready"
                    and initial_pids.isdisjoint(
                        {p["pid"] for p in s.get("processes", [])}
                    )
                ),
                timeout=90,
            )
            await asyncio.sleep(2)
            assert contact_provider.list_calls() == ("contact.enrich", "contact.verify")
            assert len((await client.get("/crm/handoffs")).json()) == 1
            assert sum(c.operation == "send" for c in await provider.list_calls()) == 1
            for width, height in [(1440, 1000), (390, 844)]:
                await page.set_viewport_size({"width": width, "height": height})
                for name, path, heading in [
                    ("need", "/demand/needs/" + need_id, None),
                    ("handoff", "/crm/handoffs/" + handoff_id, None),
                    (
                        "opportunity",
                        "/crm/opportunities?opportunity_id=" + opportunity_id,
                        None,
                    ),
                    ("cost", "/costing-quotes?opportunity_id=" + opportunity_id, None),
                    ("runs", "/runs", "Run 全景"),
                ]:
                    await page.goto(state["web_url"] + path)
                    if heading:
                        await expect(
                            page.get_by_role("heading", name=heading, exact=True)
                        ).to_be_visible()
                    else:
                        await page.wait_for_load_state("networkidle")
                    await screenshot(page, directory, f"{name}-{width}", pages)
            await page.goto(state["web_url"] + "/demand/needs/" + need_id)
            await expect(
                page.locator("blockquote").filter(has_text="We need hinges")
            ).to_be_visible()
            async with page.expect_download() as pending_download:
                await page.get_by_role(
                    "button", name="下载邮件原件", exact=True
                ).first.click()
            download = await pending_download.value
            await download.save_as(str(directory / "message.eml"))
            assert body.encode() in (directory / "message.eml").read_bytes()
            assert (
                await client.get("/inbox/messages/" + message_id + "/evidence")
            ).status_code == 200
            await page.goto(state["web_url"] + "/crm/handoffs/" + handoff_id)
            await page.get_by_role("button", name="接受接管", exact=True).click()
            await expect(page.get_by_text("已接受", exact=False).first).to_be_visible(
                timeout=15000
            )
            await screenshot(page, directory, "handoff-accepted-390", pages)
            proof["handoff_accepted"] = True
            sales = next(x for x in ids if x["role"] == "sales")
            from domains.employees.permissions import Actor, EmployeeScope
            from shared.schemas.identifiers import EmployeeId, ProspectAccountId

            async with deps.employees(TenantId(config.tenant_id)) as employees:
                await employees.transfer(
                    TenantId(config.tenant_id),
                    ProspectAccountId(opportunity["account_id"]),
                    EmployeeId(ids[0]["employee_id"]),
                    actor=Actor(ids[0]["employee_id"], EmployeeScope.TENANT, "boss"),
                    transferred_by=EmployeeId(ids[0]["employee_id"]),
                    reason="本owner受控权限撤销验收，转交当前老板",
                )
            # 员工停用是明确的受控授权撤销注入，不改变业务结果或伪造审批。
            async with factory.begin() as session:
                await session.execute(
                    update(EmployeeRow)
                    .where(
                        EmployeeRow.tenant_id == config.tenant_id,
                        EmployeeRow.employee_id == sales["employee_id"],
                    )
                    .values(is_active=False)
                )
            await page.get_by_label("演练角色").select_option(sales["employee_id"])
            await expect(
                page.locator("blockquote").filter(has_text="We need hinges")
            ).to_have_count(0)
            for path in [
                "/demand/needs/" + need_id,
                "/crm/handoffs/" + handoff_id,
                "/runs",
                "/inbox/messages/" + message_id + "/evidence",
            ]:
                response = await client.get(
                    path, headers={"X-Employee-Id": sales["employee_id"]}
                )
                assert response.status_code in (401, 403, 404), (
                    path,
                    response.status_code,
                )
            await screenshot(page, directory, "need-sales-denied-390", pages)
            assert not errors
            proof["pages"] = pages
            proof["pageerrors"] = errors
            (directory / "proof.json").write_text(
                json.dumps(proof, ensure_ascii=False, indent=2)
            )
            assert research_ready, "A2原launcher研究依赖未装配，其他场景证据独立保留"
    finally:
        if browser:
            await browser.close()
        if deps:
            for resource in [deps.model_lifecycle, deps.object_store_lifecycle]:
                if resource:
                    await resource.aclose()
        if engine:
            await engine.dispose()
        if process.poll() is None:
            process.terminate()
            await asyncio.to_thread(process.wait, 45)
        paths = list(tmp_path.glob("tradeos-controlled-*/status.json"))
        if paths:
            final = json.loads(paths[0].read_text())
            directory = EVIDENCE / final["owner"]
            directory.mkdir(exist_ok=True)
            (directory / "cleanup.json").write_text(
                json.dumps(final, ensure_ascii=False, indent=2)
            )
            assert (
                final["status"] in ("stopped", "failed")
                and final["cleanup_errors"] == []
            )
            for stem in ("mail.sqlite", "reply-model.sqlite"):
                assert all(
                    not (paths[0].parent / (stem + suffix)).exists()
                    for suffix in ("", "-journal", "-wal", "-shm")
                )
            assert not (paths[0].parent / "config.json").exists()
