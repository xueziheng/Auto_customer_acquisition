import { afterEach, beforeEach, expect, it, vi } from "vitest";

beforeEach(() => {
  vi.resetModules();
  vi.stubEnv("DEV", false); vi.stubEnv("PROD", true);
  vi.stubEnv("VITE_API_BASE_URL", "");
  vi.stubEnv("VITE_TENANT_ID", ""); vi.stubEnv("VITE_EMPLOYEE_ID", "");
});
afterEach(() => { vi.unstubAllGlobals(); vi.unstubAllEnvs(); });

async function setup() {
  const { apiClient } = await import("./client");
  const auth = await import("./authentication");
  const csrf = crypto.randomUUID();
  const dto = { employee: { tenant_id: "tn_test", employee_id: "emp_test", name: "合成员工", role: "sales", is_active: true }, csrf_token: csrf, expires_at: "2099-01-01T00:00:00Z" };
  return { apiClient, auth, csrf, dto };
}

it("无会话恢复为匿名；登录只在内存保留材料，退出为204", async () => {
  const { auth, dto, apiClient } = await setup();
  const fetcher = vi.fn().mockResolvedValueOnce(new Response(null, { status: 401 })).mockResolvedValueOnce(Response.json(dto)).mockResolvedValueOnce(new Response(null, { status: 204 }));
  vi.stubGlobal("fetch", fetcher);
  await auth.restoreSession(); expect(auth.currentIdentity()).toBeNull();
  await auth.login("synthetic", crypto.randomUUID());
  expect(auth.currentIdentity()?.mode).toBe("authenticated");
  expect(apiClient.identitySnapshot().identity?.employeeId).toBe("emp_test");
  expect(localStorage.length + sessionStorage.length).toBe(0);
  await auth.logout(); expect(auth.currentIdentity()).toBeNull();
});

it("认证请求与原始上传同源cookie/CSRF，无开发身份头，403保留登录", async () => {
  const { auth, dto, csrf } = await setup();
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json(dto)));
  await auth.login("synthetic", crypto.randomUUID());
  const requests: Request[] = [];
  const { createApiClient } = await import("./client");
  const client = createApiClient({ baseUrl: "http://localhost:3000/api", fetch: async (r) => { requests.push(r); return new Response(null, { status: 403 }); } });
  await client.uploadWorkArtifact({ artifactKind: "email_raw", body: new Blob(["synthetic"], { type: "message/rfc822" }), customerTimezone: "UTC", occurredAt: "2026-09-07T00:00:00Z", sourceKind: "email_text" });
  const r = requests[0]!;
  const safeHeaders = r.headers.get("X-CSRF-Token") === csrf && !r.headers.has("X-Employee-Id") && !r.headers.has("X-Tenant-Id") && r.headers.get("X-TradeOS-Request") === "1";
  expect(safeHeaders).toBe(true);
  expect(r.headers.get("Content-Type")).toBe("message/rfc822");
  expect(r.credentials).toBe("same-origin");
  expect(auth.currentIdentity()).not.toBeNull();
});

it("401撤销当前状态，旧身份迟到401不得撤销新身份", async () => {
  const { auth, dto } = await setup();
  vi.stubGlobal("fetch", vi.fn().mockImplementation(() => Promise.resolve(Response.json(dto))));
  await auth.login("synthetic", crypto.randomUUID());
  let finish!: (r: Response) => void;
  const { createApiClient } = await import("./client");
  const client = createApiClient({ baseUrl: "http://localhost:3000/api", fetch: () => new Promise((resolve) => { finish = resolve; }) });
  const pending = client.GET("/notifications").catch(() => null);
  await new Promise((resolve) => setTimeout(resolve, 0));
  await auth.login("synthetic", crypto.randomUUID());
  finish(new Response(null, { status: 401 })); await pending;
  expect(auth.currentIdentity()).not.toBeNull();
  const denied = createApiClient({ baseUrl: "http://localhost:3000/api", fetch: async () => new Response(null, { status: 401 }) });
  await denied.GET("/notifications").catch(() => null);
  expect(auth.currentIdentity()).toBeNull();
});

it("登录轮换及退出只广播失效事件，其他标签清理旧身份", async () => {
  const messages: unknown[] = [];
  let receive!: (event: MessageEvent) => void;
  vi.stubGlobal("BroadcastChannel", class {
    set onmessage(value: (event: MessageEvent) => void) { receive = value; }
    postMessage(value: unknown) { messages.push(value); }
    close() {}
  });
  const { auth, dto } = await setup();
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json(dto)));
  const stop = auth.listenForSessionInvalidation();
  await auth.login("synthetic", crypto.randomUUID());
  expect(messages).toEqual(["invalidate"]);
  receive(new MessageEvent("message", { data: "invalidate" }));
  expect(auth.currentIdentity()).toBeNull();
  stop();
});

it("退出失败清空本地状态但明确报告未确认服务器撤销", async () => {
  const { auth, dto } = await setup();
  vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(Response.json(dto)).mockResolvedValueOnce(new Response(null, { status: 503 })));
  await auth.login("synthetic", crypto.randomUUID());
  await expect(auth.logout()).rejects.toThrow("退出未完成");
  expect(auth.currentIdentity()).toBeNull();
});

it("生产根节点恢复前不挂业务，身份变化重建页面与通知，失效卸载", async () => {
  const { createApp, h, nextTick } = await import("vue");
  const { createRouter, createMemoryHistory } = await import("vue-router");
  let badges = 0; let pages = 0;
  vi.doMock("../components/NotificationBadge.vue", () => ({ default: { setup() { badges++; return () => h("span", "合成通知"); } } }));
  const { auth, dto } = await setup();
  vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(new Response(null, { status: 401 })).mockImplementation(() => Promise.resolve(Response.json(dto))));
  const { default: App } = await import("../App.vue");
  const { configureAuthenticatedIdentity, clearAuthenticatedIdentity } = await import("./client");
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/:pathMatch(.*)*", component: { setup() { pages++; return () => h("p", "合成业务页面"); } } }] });
  await router.push("/commands");
  const host = document.createElement("div"); document.body.append(host);
  const app = createApp(App); app.use(router); app.mount(host);
  expect(host.textContent).not.toContain("合成业务页面");
  const flush = async () => { for (let i = 0; i < 5; i++) { await nextTick(); await new Promise(resolve => setTimeout(resolve, 0)); } };
  await flush(); expect(host.textContent).toContain("登录内部运营台");
  await auth.login("synthetic", crypto.randomUUID()); await flush();
  expect(pages).toBe(1); expect(badges).toBe(1);
  configureAuthenticatedIdentity("tn_other", "emp_other"); await flush();
  expect(pages).toBe(2); expect(badges).toBe(2);
  clearAuthenticatedIdentity(); await flush();
  expect(host.textContent).not.toContain("合成业务页面");
  expect(host.textContent).not.toContain("合成通知");
  app.unmount(); host.remove(); vi.doUnmock("../components/NotificationBadge.vue");
});
