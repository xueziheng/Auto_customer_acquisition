import { createApp, h, nextTick, type App } from "vue";
import { createMemoryHistory, createRouter } from "vue-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import WorkspaceNavigation from "../src/components/WorkspaceNavigation.vue";
import applicationRouter from "../src/router";
import { clearAuthenticatedIdentity, configureControlledIdentity, createApiClient, type WebIdentityProvider } from "../src/api/client";

let app: App | undefined;
afterEach(() => { app?.unmount(); app = undefined; clearAuthenticatedIdentity(); document.body.replaceChildren(); vi.unstubAllEnvs(); });

async function mountNavigation(path: string, client?: ReturnType<typeof createApiClient>) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: applicationRouter.getRoutes().map(route => ({ path: route.path, component: { template: "<div />" } })),
  });
  await router.replace(path);
  const host = document.createElement("div");
  document.body.append(host);
  app = createApp({ render: () => h("div", [h(WorkspaceNavigation, { level: "primary" }), h(WorkspaceNavigation, { level: "secondary" })]) });
  if (client) app.provide("tradeos-api-client", client);
  app.use(router).mount(host);
  await nextTick();
  return { host, router };
}

describe("统一工作区导航", () => {
  it.each([
    ["/knowledge", "产品资料", "企业资料库"],
    ["/crm/handoffs/hf_synthetic", "工作台", "待跟进"],
    ["/approvals?approval_id=apr_synthetic", "工作台", "待审批"],
    ["/demand/needs/need_synthetic", "客户", "需求证据"],
    ["/sourcing/case_synthetic", "客户", "寻源"],
    ["/costing-quotes/quotes/quote_synthetic", "客户", "成本与报价"],
    ["/inbox/mailbox", "消息", "我的邮箱"],
    ["/runs?run=run_synthetic", "企业设置", "运行记录"],
  ])("详情深链 %s 保留所属导航和唯一当前项", async (path, primary, secondary) => {
    const { host } = await mountNavigation(path);
    expect([...host.querySelectorAll('.primary a[aria-current="page"]')].map(link => link.textContent)).toEqual([primary]);
    expect([...host.querySelectorAll('.secondary a[aria-current="page"]')].map(link => link.textContent)).toEqual([secondary]);
  });

  it("用户可切换五组并通过二级入口打开实际注册的页面", async () => {
    const { host, router } = await mountNavigation("/crm/handoffs");
    const primaryLinks = [...host.querySelectorAll<HTMLAnchorElement>(".primary a")];
    expect(primaryLinks).toHaveLength(5);
    for (const primary of primaryLinks) {
      primary.click();
      await new Promise(resolve => setTimeout(resolve, 0));
      await nextTick();
      expect(router.currentRoute.value.path).toBe(primary.getAttribute("href"));
      for (const secondary of host.querySelectorAll<HTMLAnchorElement>(".secondary a")) {
        expect(applicationRouter.resolve(secondary.getAttribute("href")!).matched.length).toBeGreaterThan(0);
      }
    }
    const runs = host.querySelector<HTMLAnchorElement>('.secondary a[href="/runs"]')!;
    runs.click();
    await new Promise(resolve => setTimeout(resolve, 0));
    await nextTick();
    expect(router.currentRoute.value.path).toBe("/runs");
    expect(runs.getAttribute("aria-current")).toBe("page");
  });
});

it.each([["boss", true], ["sales", false]])("企业资料入口共享，供应卡只对已核实的内部角色 %s 显示", async (role, visible) => {
  let generation = 1;
  let active = true;
  const listeners = new Set<() => void>();
  const provider: WebIdentityProvider = {
    generation: () => generation,
    current: () => active ? { mode: "authenticated", tenantId: "tenant_fixture", employeeId: "employee_fixture" } : null,
    subscribe: listener => { listeners.add(listener); return () => { listeners.delete(listener); }; },
  };
  const client = createApiClient({
    baseUrl: "https://tradeos.test",
    fetch: async () => new Response(JSON.stringify({
      employee: { tenant_id: "tenant_fixture", employee_id: "employee_fixture", role, is_active: true },
    }), { status: 200, headers: { "Content-Type": "application/json" } }),
  }, provider);
  const { host } = await mountNavigation("/knowledge", client);
  await new Promise(resolve => setTimeout(resolve, 0)); await nextTick();
  expect(host.querySelector('.primary a[href="/knowledge"]')?.textContent).toBe("产品资料");
  expect(host.querySelector('.secondary a[href="/knowledge"]')).not.toBeNull();
  expect(host.querySelector('.secondary a[href="/products"]') !== null).toBe(visible);
  active = false; generation += 1; for (const listener of listeners) listener(); await nextTick();
  expect(host.querySelector('.secondary a[href="/products"]')).toBeNull();
});


it("受控开发导航不读取登录会话，角色切换保留当前员工且不反复失效", async () => {
  vi.stubEnv("DEV", true);
  vi.stubEnv("PROD", false);
  vi.stubEnv("VITE_TENANT_ID", "tenant_controlled");
  vi.stubEnv("VITE_EMPLOYEE_ID", "employee_first");
  vi.stubEnv("VITE_CONTROLLED_CONFIG", JSON.stringify({
    owner: "a".repeat(32),
    tenantId: "tenant_controlled",
    identities: [
      { employeeId: "employee_first", label: "第一位演练员工" },
      { employeeId: "employee_second", label: "第二位演练员工" },
    ],
  }));
  let requests = 0;
  const client = createApiClient({
    baseUrl: "https://tradeos.test",
    fetch: async () => {
      requests += 1;
      return new Response(null, { status: requests === 1 ? 401 : 503 });
    },
  });
  const { host, router } = await mountNavigation("/settings", client);
  configureControlledIdentity("employee_second");
  const selected = client.identitySnapshot();
  for (let attempt = 0; attempt < 3; attempt++) {
    await new Promise(resolve => setTimeout(resolve, 0));
    await nextTick();
  }
  expect(requests).toBe(0);
  expect(client.identitySnapshot()).toEqual(selected);
  expect(selected.identity?.employeeId).toBe("employee_second");
  await router.push("/knowledge");
  await nextTick();
  expect(host.querySelector('.secondary a[href="/knowledge"]')).not.toBeNull();
  expect(host.querySelector('.secondary a[href="/products"]')).toBeNull();
});
