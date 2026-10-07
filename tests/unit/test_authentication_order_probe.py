"""真实双Page/loopback验证因果探针：排队通过，提前登录必须拒绝；不访问业务服务。"""

from __future__ import annotations

import asyncio
from contextlib import suppress

import pytest
from playwright.async_api import async_playwright

from tests.e2e.authentication_order import AuthenticationOrderProbe


@pytest.mark.parametrize("serialized", [True, False], ids=["web_lock", "early_login"])
async def test_authentication_probe_detects_actual_overlap_despite_delayed_notifications(
    serialized: bool,
) -> None:
    logout_started = asyncio.Event()
    login_started = asyncio.Event()
    release_logout = asyncio.Event()
    handlers: set[asyncio.Task] = set()

    async def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        handlers.add(task)
        try:
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            request_line = header.split(b"\r\n", 1)[0]
            if request_line == b"POST /api/auth/logout HTTP/1.1":
                logout_started.set()
                await asyncio.wait_for(release_logout.wait(), 10)
                status = b"204 No Content"
            else:
                if request_line == b"POST /api/auth/login HTTP/1.1":
                    login_started.set()
                status = b"200 OK"
            writer.write(
                b"HTTP/1.1 " + status
                + b"\r\nContent-Type: text/html\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass  # 浏览器可关闭尚未发送请求的预连接。
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            handlers.discard(task)

    server = await asyncio.start_server(respond, "127.0.0.1", 0)
    origin = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    actions: list[asyncio.Task] = []
    notifications: list[str] = []
    delayed_responses: list[asyncio.Task] = []
    login_notified = asyncio.Event()
    logout_observed = asyncio.Event()
    try:
        async with server, async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                context = await browser.new_context()
                first = await context.new_page()
                second = await context.new_page()
                await first.goto(origin)
                await second.goto(origin)
                probe = AuthenticationOrderProbe(first, second)
                await probe.install()

                async def deliver_logout_notification() -> None:
                    await asyncio.wait_for(login_notified.wait(), 5)
                    notifications.append("logout_response")

                def record_response(response) -> None:
                    if response.url == origin + "/api/auth/logout":
                        delayed_responses.append(asyncio.create_task(deliver_logout_notification()))
                        logout_observed.set()

                def record_request(request) -> None:
                    if request.url == origin + "/api/auth/login":
                        notifications.append("login_request")
                        login_notified.set()

                first.on("response", record_response)
                second.on("request", record_request)
                logout = asyncio.create_task(first.evaluate(
                    """() => navigator.locks.request("tradeos-authentication", async () => {
                      await fetch("/api/auth/logout", { method: "POST" });
                    })"""
                ))
                actions.append(logout)
                await asyncio.wait_for(logout_started.wait(), 5)
                login = asyncio.create_task(second.evaluate(
                    """serialized => {
                      const send = () => fetch("/api/auth/login", { method: "POST" }).then(() => {});
                      return serialized ? navigator.locks.request("tradeos-authentication", send) : send();
                    }""", serialized,
                ))
                actions.append(login)
                if serialized:
                    await second.wait_for_function(
                        """async () => {
                          const state = await navigator.locks.query();
                          return state.held.some(x => x.name === "tradeos-authentication")
                            && state.pending.some(x => x.name === "tradeos-authentication");
                        }""", timeout=5000,
                    )
                    assert not login_started.is_set()
                else:
                    await asyncio.wait_for(login_started.wait(), 5)
                    await login
                release_logout.set()
                await asyncio.wait_for(asyncio.gather(*actions), 5)
                if serialized:
                    await probe.assert_serialized()
                else:
                    with pytest.raises(AssertionError, match="AUTHENTICATION_LOCK_ORDER_INVALID"):
                        await probe.assert_serialized()
                await asyncio.wait_for(logout_observed.wait(), 5)
                await asyncio.wait_for(asyncio.gather(*delayed_responses), 5)
                assert notifications == ["login_request", "logout_response"]
                await probe.close()
            finally:
                release_logout.set()
                for task in [*actions, *delayed_responses]:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*actions, *delayed_responses, return_exceptions=True)
                await browser.close()
    finally:
        release_logout.set()
        server.close()
        await server.wait_closed()
        for task in tuple(handlers):
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)
