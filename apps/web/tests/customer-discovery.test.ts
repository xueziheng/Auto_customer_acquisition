import { createApp, nextTick, type App } from "vue";
import { afterEach, expect, it } from "vitest";
import { createApiClient } from "../src/api/client";
import CustomerDiscovery from "../src/views/customer-discovery/CustomerDiscovery.vue";
let app: App | undefined;
afterEach(() => {
  app?.unmount();
  document.body.replaceChildren();
});
async function mount(role: string, status = 200) {
  const paths: string[] = [];
  const client = createApiClient(
    {
      baseUrl: "https://tradeos.test",
      fetch: async (input) => {
        const path = new URL((input as Request).url).pathname;
        paths.push(path);
        return new Response(
          JSON.stringify(
            path.endsWith("/access")
              ? {
                  role,
                  can_search: role === "boss",
                  can_assign: role === "boss",
                }
              : [],
          ),
          { status, headers: { "Content-Type": "application/json" } },
        );
      },
    },
    {
      generation: () => 0,
      current: () => ({
        mode: "fixed-dev",
        tenantId: "tn_a",
        employeeId: "emp_a",
      }),
      subscribe: () => () => {},
    },
  );
  const host = document.createElement("div");
  document.body.append(host);
  app = createApp(CustomerDiscovery);
  app.provide("tradeos-api-client", client);
  app.mount(host);
  for (let i = 0; i < 8; i++) {
    await nextTick();
    await new Promise((r) => setTimeout(r, 0));
  }
  return { host, paths };
}
it.each(["manager", "sales"])(
  "%s 直达找客户只读取access并解释权限",
  async (role) => {
    const { host, paths } = await mount(role);
    expect(paths).toEqual(["/prospects/customers/access"]);
    expect(host.querySelector('[aria-label="候选库存"]')).toBeNull();
    expect(host.textContent).toContain("管理员");
  },
);
it("access失败不挂载公司搜索面板", async () => {
  const { host, paths } = await mount("boss", 503);
  expect(paths).toEqual(["/prospects/customers/access"]);
  expect(host.querySelector('[aria-label="候选库存"]')).toBeNull();
  expect(host.querySelector('[role="alert"]')).not.toBeNull();
});
it("boss的搜索页不再读取旧潜在企业面板或来源诊断", async () => {
  const { host, paths } = await mount("boss");
  expect(host.querySelector('[aria-label="候选库存"]')).not.toBeNull();
  expect(paths).not.toContain("/prospects/accounts");
  expect(paths).not.toContain("/prospects/inventory/sources");
  expect(host.querySelector('[aria-label="潜在企业与联系人"]')).toBeNull();
});
