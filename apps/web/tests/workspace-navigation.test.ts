import { createApp, h, nextTick, type App } from "vue";
import { createMemoryHistory, createRouter } from "vue-router";
import { afterEach, expect, it } from "vitest";
import WorkspaceNavigation from "../src/components/WorkspaceNavigation.vue";
import applicationRouter from "../src/router";
import { createApiClient, type WebIdentityProvider } from "../src/api/client";
let app: App | undefined;
afterEach(() => {
  app?.unmount();
  app = undefined;
  document.body.replaceChildren();
});
async function settle() {
  for (let i = 0; i < 8; i++) {
    await nextTick();
    await new Promise((r) => setTimeout(r, 0));
  }
}
async function mount(path: string, role: string, status = 200) {
  let generation = 0,
    employeeId = "emp_a";
  const listeners = new Set<() => void>(),
    requests: string[] = [];
  const provider: WebIdentityProvider = {
    generation: () => generation,
    current: () => ({ mode: "fixed-dev", tenantId: "tn_a", employeeId }),
    subscribe: (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
  };
  const client = createApiClient(
    {
      baseUrl: "https://tradeos.test",
      fetch: async (input) => {
        requests.push(new URL((input as Request).url).pathname);
        return new Response(
          JSON.stringify({
            role,
            can_search: role === "boss",
            can_assign: role === "boss",
          }),
          { status, headers: { "Content-Type": "application/json" } },
        );
      },
    },
    provider,
  );
  const router = createRouter({
    history: createMemoryHistory(),
    routes: applicationRouter
      .getRoutes()
      .map((r) => ({ path: r.path, component: { template: "<div/>" } })),
  });
  await router.replace(path);
  const host = document.createElement("div");
  document.body.append(host);
  app = createApp({
    render: () =>
      h("div", [
        h(WorkspaceNavigation, { level: "primary" }),
        h(WorkspaceNavigation, { level: "secondary" }),
      ]),
  });
  app.provide("tradeos-api-client", client);
  app.use(router).mount(host);
  await settle();
  return {
    host,
    router,
    requests,
    switchIdentity: () => {
      employeeId = "emp_b";
      generation++;
      for (const l of listeners) l();
    },
  };
}
it.each(["manager", "sales"])(
  "%s 的日常入口只有本人客户、消息和产品资料",
  async (role) => {
    const { host, requests } = await mount("/knowledge", role);
    expect(
      [...host.querySelectorAll(".primary a")].map((a) => a.textContent),
    ).toEqual(["我的客户", "消息", "产品资料"]);
    expect(requests).toContain("/prospects/customers/access");
    expect(requests).not.toContain("/auth/session");
    for (const href of [
      "/products",
      "/campaigns",
      "/sourcing",
      "/costing-quotes",
      "/runs",
      "/crm/outreach",
    ])
      expect(host.querySelector(`a[href="${href}"]`)).toBeNull();
  },
);
it("boss 能找客户和管理企业，失败时管理员入口关闭", async () => {
  const { host } = await mount("/prospects/accounts", "boss");
  expect(
    [...host.querySelectorAll(".primary a")].map((a) => a.textContent),
  ).toEqual(["找客户", "我的客户", "消息", "产品资料", "企业管理"]);
  expect(host.querySelector(".secondary")).toBeNull();
  app?.unmount();
  const failed = await mount("/settings", "boss", 503);
  expect(failed.host.querySelector('.primary a[href="/settings"]')).toBeNull();
  expect(
    failed.host.querySelector('.primary a[href="/prospects/accounts"]'),
  ).toBeNull();
});
it.each([
  "/crm/handoffs/hf_a",
  "/approvals?approval_id=apr_a",
  "/demand/needs/need_a",
  "/sourcing/case_a",
  "/costing-quotes/quotes/quote_a",
])("历史深链 %s 仍归属我的客户且没有恢复隐藏入口", async (path) => {
  const { host } = await mount(path, "sales");
  expect(
    host.querySelector('.primary a[aria-current="page"]')?.textContent,
  ).toBe("我的客户");
  expect(host.querySelector(".secondary")).toBeNull();
});
it("切换身份立即清除管理员入口", async () => {
  const { host, switchIdentity } = await mount("/settings", "boss");
  switchIdentity();
  await nextTick();
  expect(host.querySelector('.primary a[href="/settings"]')).toBeNull();
});
