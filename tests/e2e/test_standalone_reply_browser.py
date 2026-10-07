"""同一 standalone 回复链的真实网页接受与撤权清屏；模型和 Gmail 网络受控。"""

import asyncio
import json
import re
import socket
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import uvicorn
from playwright.async_api import async_playwright, expect
from sqlalchemy import text

from infra.standalone.settings import StandaloneModelSettings
from shared.schemas.identifiers import new_id
from tests.integration.test_reply_completion import configure_playbook
from tests.integration.test_standalone_reply_chain import (
    BODY,
    NoModelCredentials,
    ReplyProvider,
    locked,
    probe,
    receive,
    rows,
    standalone_reply_chain,
)
from tests.integration.test_standalone_reply_chain import (
    reply_storage as _reply_storage,
)
from tests.unit.test_standalone_model_settings import settings

pytestmark = pytest.mark.e2e
reply_storage = _reply_storage


async def test_standalone_reply_browser_accepts_and_clears_revoked_content(
    reply_storage, tmp_path
):
    evidence = Path("/tmp/tradeos-reply-browser-20261005") / new_id("proof")
    evidence.mkdir(parents=True, mode=0o700)
    proof = {
        "status": "incomplete",
        "real_model": False,
        "gmail": "controlled",
        "viewports": [1440, 390],
        "pageerrors": [],
        "console": [],
    }
    model = StandaloneModelSettings.model_validate(
        {
            **settings(),
            "reply_enabled": True,
            "limits": {
                **settings()["limits"],
                "tenant_calls": 20,
                "employee_calls": 20,
                "max_output_tokens": 2048,
                "max_input_bytes": 65536,
            },
        }
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        profile = reply_storage.model_copy(update={"api_port": port})
        async with standalone_reply_chain(
            profile,
            tmp_path,
            model=model,
            model_resolver=NoModelCredentials(),
            model_provider=ReplyProvider(),
        ) as chain:
            origin = f"http://127.0.0.1:{port}"
            # 真 lifespan 已由同一 fixture 持有；HTTP 服务不启动第二组 API 心跳。
            server = uvicorn.Server(
                uvicorn.Config(
                    chain["app"], lifespan="off", access_log=False, log_config=None
                )
            )
            serving = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                for _ in range(100):
                    if serving.done():
                        await serving
                    if server.started:
                        break
                    await asyncio.sleep(0.05)
                assert server.started

                async def exercise(worker):
                    await configure_playbook(chain, worker)
                    await probe(chain, worker)
                    await receive(chain, worker)
                    records = await rows(
                        chain,
                        "SELECT n.need_id,n.quantity,o.opportunity_id,h.handoff_id,h.customer_verbatim "
                        "FROM handoffs h JOIN opportunities o ON o.tenant_id=h.tenant_id AND o.opportunity_id=h.opportunity_id "
                        "JOIN validated_needs n ON n.tenant_id=o.tenant_id AND n.need_id=o.need_id "
                        "WHERE h.tenant_id=:t AND o.tenant_id=:t AND n.tenant_id=:t",
                    )
                    assert len(records) == 1
                    record = records[0]
                    assert str(record.quantity["value"]) == "5000"
                    assert record.customer_verbatim
                    proof.update(
                        {
                            "origin": origin,
                            "need_id": record.need_id,
                            "opportunity_id": record.opportunity_id,
                            "handoff_id": record.handoff_id,
                        }
                    )
                    route = f"{origin}/crm/handoffs/{record.handoff_id}"
                    async with async_playwright() as playwright:
                        browser = await playwright.chromium.launch()
                        proof["browser"] = browser.version
                        context = await browser.new_context(
                            viewport={"width": 1440, "height": 1000}
                        )
                        try:
                            desktop = await context.new_page()
                            mobile = await context.new_page()
                            await mobile.set_viewport_size(
                                {"width": 390, "height": 844}
                            )
                            for page in (desktop, mobile):
                                page.on(
                                    "pageerror",
                                    lambda error: proof["pageerrors"].append(
                                        str(error)
                                    ),
                                )
                                page.on(
                                    "console",
                                    lambda message: (
                                        proof["console"].append(
                                            {
                                                "type": message.type,
                                                "text": message.text[:400],
                                            }
                                        )
                                        if message.type in {"error", "warning"}
                                        else None
                                    ),
                                )
                            await desktop.goto(route)
                            await desktop.get_by_label("邮箱或用户名", exact=True).fill(
                                "synthetic"
                            )
                            await desktop.get_by_label("密码", exact=True).fill(
                                chain["login_password"].get_secret_value()
                            )
                            await desktop.get_by_role(
                                "button", name="登录", exact=True
                            ).click()
                            await expect(
                                desktop.get_by_role("article", name="完整接管包")
                            ).to_be_visible()
                            await mobile.goto(route)
                            for name, page in (
                                ("desktop", desktop),
                                ("mobile", mobile),
                            ):
                                await expect(page).to_have_title(re.compile("TradeOS"))
                                assert page.url == route
                                packet = page.get_by_role("article", name="完整接管包")
                                await expect(packet).to_be_visible()
                                for identifier in (
                                    record.need_id,
                                    record.opportunity_id,
                                    record.handoff_id,
                                ):
                                    await expect(packet).to_contain_text(identifier)
                                await expect(
                                    packet.locator("dd.verbatim")
                                ).to_have_text(record.customer_verbatim)
                                await expect(packet).to_contain_text("hinges")
                                await expect(packet).to_contain_text("5000")
                                await expect(
                                    page.get_by_role(
                                        "button", name="接受接管", exact=True
                                    )
                                ).to_be_enabled()
                                assert (
                                    await page.locator("vite-error-overlay").count()
                                    == 0
                                )
                                assert await page.evaluate(
                                    "document.documentElement.scrollWidth <= innerWidth"
                                )
                                await page.screenshot(
                                    path=str(evidence / f"{name}-packet.png"),
                                    full_page=True,
                                )
                                await packet.locator(
                                    "dd.verbatim"
                                ).scroll_into_view_if_needed()
                                await page.screenshot(
                                    path=str(evidence / f"{name}-evidence.png"),
                                    full_page=True,
                                )

                            async with (
                                desktop.expect_download() as pending_download,
                                desktop.expect_response(
                                    lambda response: (
                                        "/api/inbox/messages/" in response.url
                                        and response.url.endswith("/evidence")
                                        and response.request.method == "GET"
                                    )
                                ) as original,
                            ):
                                await desktop.get_by_role(
                                    "button", name="下载邮件原件", exact=True
                                ).first.click()
                                original_response = await original.value
                                proof["original_status"] = original_response.status
                                assert original_response.status == 200
                            download = await pending_download.value
                            await download.save_as(str(evidence / "message.eml"))
                            assert (
                                BODY.encode() in (evidence / "message.eml").read_bytes()
                            )
                            proof["original_message_verified"] = True

                            async with (
                                mobile.expect_response(
                                    lambda response: (
                                        urlsplit(response.url).path
                                        == "/api/crm/handoffs"
                                        and response.request.method == "GET"
                                    )
                                ) as refresh,
                                mobile.expect_response(
                                    lambda response: (
                                        response.url.endswith(
                                            f"/api/crm/handoffs/{record.handoff_id}/accept"
                                        )
                                        and response.request.method == "POST"
                                    )
                                ) as acceptance,
                            ):
                                await mobile.get_by_role(
                                    "button", name="接受接管", exact=True
                                ).click()
                            accept_response = await acceptance.value
                            assert accept_response.status == 204
                            final_status = mobile.get_by_role("status").filter(
                                has_text="已接受接管"
                            )
                            await expect(final_status).to_have_text(
                                "已接受接管；已按后端等待顺序刷新队列。"
                            )
                            queue_response = await refresh.value
                            assert queue_response.status == 200
                            queue_json = await queue_response.json()
                            accepted_by = (
                                await rows(
                                    chain,
                                    "SELECT accepted_by FROM handoffs WHERE tenant_id=:t",
                                )
                            )[0][0]
                            assert (
                                queue_json == []
                                and accepted_by == chain["staff"][0].employee_id
                            )
                            proof.update(
                                {
                                    "accept_status": accept_response.status,
                                    "queue": queue_json,
                                    "accepted_by": accepted_by,
                                }
                            )
                            await mobile.screenshot(
                                path=str(evidence / "mobile-accepted.png"),
                                full_page=True,
                            )

                            # 宽屏仍持有此前合法读取的接管内容；撤权后走原会话恢复流程。
                            await expect(
                                desktop.get_by_role("article", name="完整接管包")
                            ).to_be_visible()
                            async with chain["factory"].begin() as db:
                                await db.execute(
                                    text(
                                        "UPDATE employees SET is_active=false WHERE tenant_id=:t AND employee_id=:e"
                                    ),
                                    {
                                        "t": chain["route"].tenant_id,
                                        "e": chain["staff"][0].employee_id,
                                    },
                                )
                            await desktop.reload()
                            await expect(
                                desktop.get_by_role("heading", name="登录内部运营台")
                            ).to_be_visible()
                            content = await desktop.locator("body").inner_text()
                            for private in (
                                record.customer_verbatim,
                                "hinges",
                                "5000",
                                record.need_id,
                                record.opportunity_id,
                                record.handoff_id,
                            ):
                                assert private not in content
                            assert (
                                await desktop.get_by_role(
                                    "article", name="完整接管包"
                                ).count()
                                == 0
                            )
                            await desktop.screenshot(
                                path=str(evidence / "desktop-revoked.png"),
                                full_page=True,
                            )
                            assert proof["pageerrors"] == []
                            assert len(chain["model_provider"].replies) == 1
                            proof.update(
                                {
                                    "revoked_content_cleared": True,
                                    "model_reply_calls": 1,
                                    "status": "passed",
                                }
                            )
                        finally:
                            await browser.close()

                await locked(chain, exercise)
            finally:
                server.should_exit = True
                await asyncio.wait_for(serving, timeout=10)
                (evidence / "proof.json").write_text(
                    json.dumps(proof, ensure_ascii=False, indent=2) + "\n"
                )
