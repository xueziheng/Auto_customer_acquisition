"""真实认证请求的浏览器侧因果观察，不依赖不同Page协议事件的交付顺序。"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field

from playwright.async_api import Page


@dataclass
class AuthenticationOrderProbe:
    """仅记录固定事件和布尔值；不读取请求材料、响应体或会话凭证。"""

    logout_page: Page
    login_page: Page
    marker: str = field(default_factory=lambda: "tradeos-e2e-auth-order-" + secrets.token_hex(12))

    async def install(self) -> None:
        """包装原生fetch观察完成点，不替换响应、不增加任何认证锁。"""
        for page in (self.logout_page, self.login_page):
            await page.evaluate(
                """key => {
                  const probe = { original: window.fetch, logins: [], logouts: 0 };
                  window[key] = probe;
                  window.fetch = async function(input, init) {
                    const target = typeof input === "string" ? input
                      : input instanceof URL ? input.href : input.url;
                    const url = new URL(target, window.location.href);
                    const method = (init?.method ?? input?.method ?? "GET").toUpperCase();
                    const sameOrigin = url.origin === window.location.origin;
                    if (sameOrigin && method === "POST" && url.pathname === "/api/auth/login") {
                      probe.logins.push(localStorage.getItem(key) === "logout_response");
                    }
                    const response = await probe.original.call(this, input, init);
                    if (sameOrigin && method === "POST" && url.pathname === "/api/auth/logout"
                        && response.status === 204) {
                      probe.logouts += 1;
                      localStorage.setItem(key, "logout_response");
                    }
                    return response;
                  };
                }""",
                self.marker,
            )

    async def assert_serialized(self) -> None:
        """真实退出fetch完成后才能调用登录fetch，且每类操作必须恰好一次。"""
        logouts = await self.logout_page.evaluate("key => window[key].logouts", self.marker)
        logins = await self.login_page.evaluate("key => window[key].logins", self.marker)
        if logouts != 1 or logins != [True]:
            raise AssertionError("AUTHENTICATION_LOCK_ORDER_INVALID")

    async def close(self) -> None:
        """只恢复本探针的fetch并删除本次随机标记。"""
        for page in (self.logout_page, self.login_page):
            if not page.is_closed():
                await page.evaluate(
                    """key => {
                      const probe = window[key];
                      if (probe) window.fetch = probe.original;
                      delete window[key];
                      localStorage.removeItem(key);
                    }""",
                    self.marker,
                )
