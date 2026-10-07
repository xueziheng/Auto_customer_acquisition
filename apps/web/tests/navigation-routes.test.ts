import { createApp, nextTick, type App as VueApp } from "vue";
import { createMemoryHistory, createRouter } from "vue-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createApiClient } from "../src/api/client";
import SettingsCenter from "../src/views/settings/SettingsCenter.vue";

const mountedApps: VueApp[] = [];

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

async function eventually(assertion: () => void): Promise<void> {
  let lastError: unknown;
  for (let attempt = 0; attempt < 50; attempt += 1) {
    await nextTick();
    await new Promise((resolve) => setTimeout(resolve, 0));
    try { assertion(); return; } catch (error) { lastError = error; }
  }
  throw lastError;
}

async function mountSettings(path = "/settings", runId = "run_candidate") {
  const writes: string[] = [];
  const provider = {
    generation: () => 0,
    current: () => ({ employeeId: "emp_test", tenantId: "tn_test", mode: "authenticated" as const }),
    subscribe: () => () => {},
  };
  const client = createApiClient({
    baseUrl: "https://tradeos.test",
    fetch: async (input) => {
      const request = input as Request;
      const pathname = new URL(request.url).pathname;
      if (request.method === "POST") {
        writes.push(pathname);
        if (pathname === "/settings/playbook/proposals") {
          return json({ change_set_ref: "playbook:pbv_test:hash", playbook_version_id: "pbv_test", run_id: runId }, 202);
        }
        return json({}, 500);
      }
      const contactEnrichment = { state: "blocked", reason_code: "COUNTRY_POLICY_NOT_CONFIGURED" };
      if (pathname === "/settings/playbook") return json({ configured: false, active_version: null, contact_enrichment: contactEnrichment });
      if (pathname === "/settings/playbook/versions" || pathname === "/health/capabilities") return json([]);
      if (pathname === "/settings/country-policies") return json({ active_policies: [], contact_enrichment: contactEnrichment, coverage: { active_policy_count: 0, contact_enrichment_allowed_count: 0 } });
      return json({ code: "not_configured", message: "not configured" }, 503);
    },
  }, provider);
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: "/settings", component: SettingsCenter }, { path: "/runs", component: { template: "<div />" } }],
  });
  const host = document.createElement("div");
  document.body.append(host);
  const app = createApp(SettingsCenter);
  app.provide("tradeos-api-client", client);
  app.use(router);
  await router.replace(path);
  app.mount(host);
  mountedApps.push(app);
  await nextTick();
  return { host, router, writes };
}

beforeEach(() => {
  vi.stubEnv("PROD", true);
  vi.stubEnv("DEV", false);
});

afterEach(() => {
  mountedApps.splice(0).forEach((app) => app.unmount());
  document.body.replaceChildren();
  vi.unstubAllEnvs();
});

describe("生产导航与业务深链", () => {
  it("生产审批深链保留同一 approval_id 并加载审批组件", async () => {
    const { default: router } = await import("../src/router");
    const approvalId = "apr_same_case_01";
    await router.replace({ path: "/approvals", query: { approval_id: approvalId } });
    expect(router.currentRoute.value.path).toBe("/approvals");
    expect(router.currentRoute.value.query).toEqual({ approval_id: approvalId });
    expect(router.currentRoute.value.name).toBe("approval-center");
    const component = router.currentRoute.value.matched[0]?.components?.default;
    expect(component).toMatchObject({ __name: "ApprovalCenter" });
  });

  it("默认入口进入现有待跟进工作台", async () => {
    const { default: router } = await import("../src/router");
    await router.replace("/");
    expect(router.currentRoute.value.path).toBe("/crm/handoffs");
    expect(router.currentRoute.value.name).toBe("crm-handoffs");
  });

  it("普通企业设置给出明确入口，不显示固定试验目标或伪造就绪状态", async () => {
    const { host, writes } = await mountSettings();
    await eventually(() => expect(host.querySelector('a[href="/team"]')).not.toBeNull());
    for (const href of ["/team", "/crm/sending-identities", "/runs", "/settings?advanced=1"]) {
      expect(host.querySelector(`a[href="${href}"]`)).not.toBeNull();
    }
    expect(host.textContent).toContain("企业设置");
    expect(host.textContent).not.toMatch(/肯尼亚|太阳能三轮车|安全规则已就绪|自动运行/);
    expect(host.querySelector('[aria-label="公司业务规则候选表单"]')).toBeNull();
    expect(writes).toEqual([]);
  });

  it("点击高级设置后在同一页面展示现有配置区", async () => {
    const { host, router, writes } = await mountSettings();
    const advanced = host.querySelector<HTMLAnchorElement>('a[href="/settings?advanced=1"]');
    expect(advanced).not.toBeNull();
    advanced!.click();
    await eventually(() => {
      expect(router.currentRoute.value.query.advanced).toBe("1");
      expect(host.querySelector('[name="company_type"]')).not.toBeNull();
    });
    expect(writes).toEqual([]);
  });

  it("提交后的运行记录链接使用 query 并保留完整运行标识", async () => {
    const runId = "run_safe&scope=other?#/片段";
    const { host, router, writes } = await mountSettings("/settings?advanced=1", runId);
    await eventually(() => expect(host.textContent).toContain("尚未配置公司业务规则"));
    for (const [name, value] of [["company_type", "trading_company"], ["minimum_deal_amount", "100"], ["minimum_deal_currency", "USD"]]) {
      const field = host.querySelector<HTMLInputElement>(`[name="${name}"]`)!;
      field.value = value;
      field.dispatchEvent(new Event("input", { bubbles: true }));
    }
    host.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await eventually(() => expect(host.querySelector("[data-run-id]")).not.toBeNull());
    const runLink = [...host.querySelectorAll<HTMLAnchorElement>(".form-message.success a")].find((link) => link.textContent?.includes("查看运行记录"));
    expect(runLink).toBeDefined();
    const href = runLink!.getAttribute("href")!;
    const resolved = router.resolve(href);
    expect(resolved.path).toBe("/runs");
    expect(resolved.query).toEqual({ run: runId });
    expect(resolved.hash).toBe("");
    expect(writes).toEqual(["/settings/playbook/proposals"]);
  });
});
