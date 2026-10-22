import { createApp, h, nextTick, ref, type App } from "vue";
import { createMemoryHistory, createRouter } from "vue-router";
import { afterEach, expect, it, vi } from "vitest";
import { createApiClient, type WebIdentityProvider } from "../src/api/client";
import CustomerFollowup from "../src/views/customers/CustomerFollowup.vue";
import { followupRecovery, type PendingFollowup } from "../src/views/customers/followupRecovery";

const apps: App[] = [];
const key = "tradeos-customer-followup-pending-v1";
const command = { campaign_id: "cmp_a", campaign_version: 1, contact_point_id: "cp_a", source_hypothesis_id: "hyp_a" };
const pending = (mode: PendingFollowup["body"]["execution_mode"] = "automatic"): PendingFollowup => ({ allocationId: "a", key: "original", body: { ...command, execution_mode: mode } });
function persist(mode?: string) {
  sessionStorage.setItem(key, JSON.stringify({ owner: JSON.stringify(["fixed-dev", "tn_a", "emp_a"]), records: [{ ...pending(), body: { ...command, ...(mode ? { execution_mode: mode } : {}) } }] }));
}
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
async function settle() {
  for (let i = 0; i < 8; i++) { await nextTick(); await new Promise(r => setTimeout(r, 0)); }
}
async function mount(mode: "manual" | "automatic" = "manual", result = 200) {
  let generation = 0, employeeId = "emp_a";
  const listeners = new Set<() => void>();
  const provider: WebIdentityProvider = {
    current: () => ({ mode: "fixed-dev", tenantId: "tn_a", employeeId }), generation: () => generation,
    subscribe: listener => { listeners.add(listener); return () => listeners.delete(listener); },
  };
  const requests: Request[] = [];
  const client = createApiClient({ baseUrl: "https://tradeos.test", fetch: async input => {
    const request = input as Request; requests.push(request.clone());
    if (request.method === "POST") {
      const body = await request.json();
      return json({ ...body, enrollment_id: "enr_a", account_id: "acc_a", state: "enrolled", current_step: 0, enrolled_at: "2026-10-14T00:00:00Z", next_send_at: null, stopped_at: null, stop_reason: null }, result);
    }
    return json({ allocation_id: "a", account_id: "acc_a", execution_mode: new URL(request.url).searchParams.get("execution_mode") ?? "automatic", blocking_reasons: [],
      campaigns: [{ campaign_id: "cmp_a", campaign_version: 1, name: "已批准任务", markets: ["KE"], target_entity_types: ["importer"], allowed_categories: ["hardware"], can_start: true, eligible_contact_point_ids: ["cp_a"], blocking_reasons: [] }],
      hypotheses: [{ hypothesis_id: "hyp_a", category: "hardware", status: "inferred", is_inference: true }],
      contacts: [{ contact: { contact_id: "con_a" }, contact_points: [{ contact_point: { contact_point_id: "cp_a", kind: "email", value: "verified@example.test", verification: "verified", verified_at: "2026-10-14T00:00:00Z" } }] }],
    });
  } }, provider);
  const selectedMode = ref(mode);
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/", component: { template: "<div />" } }, { path: "/crm/outreach", component: { template: "<div />" } }] });
  await router.replace("/");
  const root = document.createElement("div"); document.body.append(root);
  const app = createApp({ render: () => h(CustomerFollowup, { allocationId: "a", executionMode: selectedMode.value }) });
  apps.push(app); app.provide("tradeos-api-client", client); app.use(router).mount(root); await settle();
  return { root, requests, client, selectedMode, switchIdentity: () => { employeeId = "emp_b"; generation++; listeners.forEach(l => l()); } };
}
async function choose(root: HTMLElement) {
  for (const field of root.querySelectorAll<HTMLSelectElement>("select[data-followup-field]")) {
    field.value = field.options[1].value; field.dispatchEvent(new Event("change")); await nextTick();
  }
}
const button = (root: HTMLElement, label: string) => [...root.querySelectorAll("button")].find(b => b.textContent?.includes(label))!;
afterEach(() => { apps.splice(0).forEach(app => app.unmount()); vi.unstubAllGlobals(); sessionStorage.clear(); document.body.replaceChildren(); });

it("手动模式只入组，明确尚未发送并链接原单封工作台", async () => {
  const view = await mount();
  expect(new URL(view.requests[0].url).searchParams.get("execution_mode")).toBe("manual");
  await choose(view.root); button(view.root, "准备单封手动联系").click(); await settle();
  const writes = view.requests.filter(r => r.method === "POST");
  expect(writes).toHaveLength(1);
  expect(await writes[0].json()).toEqual({ ...command, execution_mode: "manual" });
  expect(view.root.textContent).toContain("尚未发信");
  expect(view.root.querySelector('a[href="/crm/outreach?enrollment_id=enr_a"]')).not.toBeNull();
});

it("原手动请求恢复保留模式，父组件切换不能改变未知请求", async () => {
  persist("manual");
  const view = await mount("automatic", 503);
  expect(() => followupRecovery(view.client).save(pending("automatic"))).toThrow("followup_recovery_conflict");
  expect(view.requests.every(r => r.method === "GET")).toBe(true);
  expect(new URL(view.requests[0].url).searchParams.get("execution_mode")).toBe("manual");
  view.selectedMode.value = "manual"; await settle();
  view.selectedMode.value = "automatic"; await settle();
  button(view.root, "核对原跟进请求").click(); await settle();
  const request = view.requests.find(r => r.method === "POST")!;
  expect(request.headers.get("Idempotency-Key")).toBe("original");
  expect(await request.json()).toEqual({ ...command, execution_mode: "manual" });
  expect(JSON.parse(sessionStorage.getItem(key)!).records[0].body.execution_mode).toBe("manual");
});

it("旧未指定模式的未决请求仍按automatic读取并原样重放", async () => {
  persist();
  const view = await mount("manual", 503);
  expect(new URL(view.requests[0].url).searchParams.get("execution_mode")).toBe("automatic");
  button(view.root, "核对原跟进请求").click(); await settle();
  expect(await view.requests.find(r => r.method === "POST")!.json()).toEqual(command);
});

it("手动未知请求被403或身份变化撤销，不能保留旧记录", async () => {
  persist("manual");
  const view = await mount("manual", 403);
  button(view.root, "核对原跟进请求").click(); await settle();
  expect(followupRecovery(view.client).records.value).toEqual([]);
  expect(sessionStorage.getItem(key)).toBeNull();
  followupRecovery(view.client).save(pending("manual"));
  view.switchIdentity(); await settle();
  expect(view.root.querySelectorAll("select[data-followup-field]")).toHaveLength(0);
  expect(followupRecovery(view.client).records.value).toEqual([]);
});

it("损坏的未知模式不会被解释为automatic后恢复", async () => {
  persist("untrusted");
  const view = await mount();
  expect(followupRecovery(view.client).records.value).toEqual([]);
  expect(sessionStorage.getItem(key)).toBeNull();
});

it("403清理内存不受浏览器存储清理失败阻止", async () => {
  persist("manual");
  const view = await mount("manual", 403);
  const storage = sessionStorage;
  vi.stubGlobal("sessionStorage", {
    getItem: storage.getItem.bind(storage), setItem: storage.setItem.bind(storage),
    removeItem: () => { throw new Error("storage unavailable"); },
  });
  button(view.root, "核对原跟进请求").click(); await settle();
  expect(followupRecovery(view.client).records.value).toEqual([]);
  expect(view.root.textContent).not.toContain("核对原跟进请求");
});
