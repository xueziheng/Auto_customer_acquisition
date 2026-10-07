import { afterEach, beforeEach, expect, it, vi } from "vitest";

beforeEach(() => {
  vi.resetModules();
  vi.stubGlobal("navigator", { locks: { request: async (_name: string, _options: unknown, callback: () => Promise<void>) => callback() } });
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
  const fetcher = vi.fn().mockResolvedValueOnce(new Response(null, { status: 401 })).mockResolvedValueOnce(Response.json(dto)).mockResolvedValueOnce(Response.json(dto)).mockResolvedValueOnce(new Response(null, { status: 204 }));
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

it("生产根节点恢复前不挂业务，身份变化重建页面，失效卸载", async () => {
  const { createApp, h, nextTick } = await import("vue");
  const { createRouter, createMemoryHistory } = await import("vue-router");
  let pages = 0;
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
  expect(pages).toBe(1);
  configureAuthenticatedIdentity("tn_other", "emp_other"); await flush();
  expect(pages).toBe(2);
  clearAuthenticatedIdentity(); await flush();
  expect(host.textContent).not.toContain("合成业务页面");
  app.unmount(); host.remove();
});

it("I1 首次退出网络失败后直接重试重新获取CSRF并撤销服务器会话", async () => {
  const { auth, dto, csrf } = await setup();
  let revoked = false; let first = true; let retrying = false; let restoredDuringRetry = false;
  const { apiClient } = await import("./client");
  const unsubscribe = apiClient.subscribeIdentity(() => { if (retrying && auth.currentIdentity()) restoredDuringRetry = true; });
  vi.stubGlobal("fetch", vi.fn(async (url: string, options?: RequestInit) => {
    if (url.endsWith("/auth/login")) return Response.json(dto);
    if (url.endsWith("/auth/session")) return revoked ? new Response(null, { status: 401 }) : Response.json(dto);
    if (url.endsWith("/auth/logout")) {
      if (first) { first = false; throw new TypeError("synthetic_network_failure"); }
      if (new Headers(options?.headers).get("X-CSRF-Token") !== csrf) return new Response(null, { status: 403 });
      revoked = true;
      return new Response(null, { status: 204 });
    }
    return new Response(null, { status: 404 });
  }));
  await auth.login("synthetic", crypto.randomUUID());
  await expect(auth.logout()).rejects.toThrow("退出未完成");
  expect(auth.currentIdentity()).toBeNull();
  retrying = true;
  await auth.logout();
  expect(revoked).toBe(true);
  expect(restoredDuringRetry).toBe(false);
  await auth.restoreSession(); expect(auth.currentIdentity()).toBeNull();
  unsubscribe();
});

it.each([204, 503])("I2 挂起退出%s期间禁止新登录/恢复，迟到结果不清新generation或广播", async (status) => {
  const messages: unknown[] = [];
  vi.stubGlobal("BroadcastChannel", class { postMessage(value: unknown) { messages.push(value); } close() {} });
  const { auth, dto } = await setup();
  const { clearAuthenticatedIdentity, configureAuthenticatedIdentity } = await import("./client");
  let complete!: (response: Response) => void;
  let loginCalls = 0;
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    if (url.endsWith("/auth/logout")) return new Promise<Response>(resolve => { complete = resolve; });
    if (url.endsWith("/auth/login")) loginCalls++;
    return Response.json(dto);
  }));
  await auth.login("synthetic", crypto.randomUUID());
  const pending = auth.logout().catch(() => undefined);
  await new Promise(resolve => setTimeout(resolve, 0));
  clearAuthenticatedIdentity();
  await expect(auth.login("synthetic", crypto.randomUUID())).rejects.toThrow("处理中");
  await expect(auth.restoreSession()).rejects.toThrow("处理中");
  expect(loginCalls).toBe(1);
  // 模拟同进程其他身份发布者；旧logout的finally也不得修改此generation。
  configureAuthenticatedIdentity("tn_new", "emp_new");
  const before = messages.length;
  complete(new Response(null, { status })); await pending;
  expect(auth.currentIdentity()?.employeeId).toBe("emp_new");
  expect(messages.length).toBe(before);
  await auth.login("synthetic", crypto.randomUUID());
  expect(loginCalls).toBe(2);
});

it("I2 没有Web Locks时登录/退出安全拒绝且不发送认证请求", async () => {
  vi.stubGlobal("navigator", {});
  const { auth } = await setup();
  const fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
  await expect(auth.login("synthetic", crypto.randomUUID())).rejects.toThrow("Web Locks");
  await expect(auth.logout()).rejects.toThrow("Web Locks");
  expect(fetcher.mock.calls.length).toBe(0);
});

it("I2 同源Web Lock持有到退出响应头完成，另一个标签的新登录随后执行", async () => {
  let queue = Promise.resolve();
  vi.stubGlobal("navigator", { locks: { request: (_name: string, _options: unknown, callback: () => Promise<void>) => {
    const result = queue.then(callback); queue = result.catch(() => undefined); return result;
  } } });
  const first = await setup();
  let release!: (response: Response) => void;
  let cookieState = "";
  const order: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, options?: RequestInit) => {
    if (url.endsWith("/auth/login")) {
      const username = JSON.parse(options?.body as string).username as string;
      cookieState = username; order.push(username);
      return Response.json(first.dto);
    }
    if (url.endsWith("/auth/session")) return Response.json(first.dto);
    order.push("logout_started");
    return new Promise<Response>(resolve => { release = response => { cookieState = ""; order.push("logout_headers_received"); resolve(response); }; });
  }));
  await first.auth.login("synthetic-a", crypto.randomUUID());
  const logout = first.auth.logout();
  await new Promise(resolve => setTimeout(resolve, 0));
  vi.resetModules();
  const second = await import("./authentication");
  const login = second.login("synthetic-b", crypto.randomUUID());
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(order).toEqual(["synthetic-a", "logout_started"]);
  release(new Response(null, { status: 204 }));
  await Promise.all([logout, login]);
  expect(order).toEqual(["synthetic-a", "logout_started", "logout_headers_received", "synthetic-b"]);
  expect(cookieState).toBe("synthetic-b");
  expect(second.currentIdentity()).not.toBeNull();
});

it("I2 生产App在退出挂起且身份先失效时隐藏登录与业务入口", async () => {
  const { createApp, h, nextTick } = await import("vue");
  const { createRouter, createMemoryHistory } = await import("vue-router");
  vi.doMock("../components/NotificationBadge.vue", () => ({ default: { render: () => h("span", "合成通知") } }));
  const { auth, dto } = await setup();
  let release!: (response: Response) => void;
  vi.stubGlobal("fetch", vi.fn(async (url: string) => url.endsWith("/auth/logout") ? new Promise<Response>(resolve => { release = resolve; }) : Response.json(dto)));
  await auth.login("synthetic", crypto.randomUUID());
  const { default: App } = await import("../App.vue");
  const { clearAuthenticatedIdentity } = await import("./client");
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/:pathMatch(.*)*", component: { render: () => h("p", "合成业务页面") } }] });
  await router.push("/commands");
  const host = document.createElement("div"); document.body.append(host);
  const app = createApp(App); app.use(router); app.mount(host);
  const flush = async () => { await nextTick(); await new Promise(resolve => setTimeout(resolve, 0)); };
  await flush(); expect(host.textContent).toContain("合成业务页面");
  expect(host.textContent).toContain("合成通知");
  const pending = auth.logout(); await flush();
  clearAuthenticatedIdentity(); await flush();
  expect(host.textContent).toContain("正在完成退出");
  expect(host.textContent).not.toContain("登录内部运营台");
  expect(host.textContent).not.toContain("合成业务页面");
  expect(host.textContent).not.toContain("合成通知");
  release(new Response(null, { status: 204 })); await pending; await flush();
  expect(host.textContent).toContain("登录内部运营台");
  app.unmount(); host.remove(); vi.doUnmock("../components/NotificationBadge.vue");
});

it("I2 匿名恢复挂起时收到跨标签失效事件，迟到旧会话不得重新发布", async () => {
  let receive!: (event: MessageEvent) => void;
  vi.stubGlobal("BroadcastChannel", class {
    set onmessage(value: (event: MessageEvent) => void) { receive = value; }
    close() {}
  });
  const { auth, dto } = await setup();
  let release!: (response: Response) => void;
  vi.stubGlobal("fetch", () => new Promise<Response>(resolve => { release = resolve; }));
  const stop = auth.listenForSessionInvalidation();
  const pending = auth.restoreSession();
  receive(new MessageEvent("message", { data: "invalidate" }));
  release(Response.json(dto)); await pending;
  expect(auth.currentIdentity()).toBeNull();
  stop();
});
