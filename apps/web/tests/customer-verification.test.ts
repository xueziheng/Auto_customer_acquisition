import { createApp, nextTick, type App } from "vue";
import { afterEach, expect, it } from "vitest";
import { createApiClient, type WebIdentityProvider } from "../src/api/client";
import CustomerVerification from "../src/views/customers/CustomerVerification.vue";

const mounted: App[] = [];
afterEach(() => { mounted.splice(0).forEach(a => a.unmount()); document.body.replaceChildren(); });
async function settle() { for (let i = 0; i < 8; i++) { await nextTick(); await new Promise(r => setTimeout(r, 0)); } }
function json(body: unknown, status = 200) { return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }); }
const point = { contact_point_id: "cp_a", account_id: "acc_a", tenant_id: "tn_a", kind: "email",
  value: "sales@company.test", verification: "unverified", verification_checked_at: null };
function view(enabled = true) { return { allocation_id: "cal_a", account_id: "acc_a", can_verify: enabled,
  blocking_reasons: enabled ? [] : ["runtime_not_composed"], contacts: [{ contact: { contact_point: point },
  can_verify: enabled, blocking_reasons: enabled ? [] : ["runtime_not_composed"] }] }; }
async function mount(options: { enabled?: boolean; post?: () => Promise<Response>; read?: () => unknown } = {}) {
  let generation = 0, employeeId = "emp_a";
  const listeners = new Set<() => void>(), writes: Request[] = [];
  const identity: WebIdentityProvider = { generation: () => generation,
    current: () => ({ tenantId: "tn_a", employeeId, mode: "fixed-dev" }),
    subscribe: l => { listeners.add(l); return () => listeners.delete(l); } };
  const client = createApiClient({ baseUrl: "https://tradeos.test", fetch: async input => {
    const request = input as Request;
    if (request.method === "POST") { writes.push(request.clone()); return options.post ? options.post() : json({
      allocation_id: "cal_a", contact_point_id: "cp_a", result: "risky", provider: "hunter", checked_at: "2026-10-14T13:00:00Z", cache_hit: false, privacy_erased: false }); }
    return json(options.read ? options.read() : view(options.enabled));
  } }, identity);
  const host = document.createElement("div"); document.body.append(host);
  const app = createApp(CustomerVerification, { allocationId: "cal_a" }); app.provide("tradeos-api-client", client);
  mounted.push(app); app.mount(host); await settle();
  return { host, writes, switchIdentity: () => { employeeId = "emp_other"; generation++; listeners.forEach(l => l()); } };
}
it("uses only the server contact id and preserves a risky result", async () => {
  const { host, writes } = await mount();
  (host.querySelector('[data-action="verify"]') as HTMLButtonElement).click(); await settle();
  expect(writes).toHaveLength(1); expect(await writes[0]!.json()).toEqual({ contact_point_id: "cp_a" });
  expect(host.textContent).toContain("有风险"); expect(host.textContent).not.toContain("验证成功");
});
it("shows actual runtime blocker without enabled verification controls", async () => {
  const { host, writes } = await mount({ enabled: false });
  expect(host.textContent).toContain("尚未接入");
  expect((host.querySelector('[data-action="verify"]') as HTMLButtonElement).disabled).toBe(true);
  expect(writes).toHaveLength(0);
});
it("clears private contact data and ignores a late verification after identity change", async () => {
  let finish!: (r: Response) => void; const waiting = new Promise<Response>(r => { finish = r; });
  const { host, switchIdentity } = await mount({ post: () => waiting });
  (host.querySelector('[data-action="verify"]') as HTMLButtonElement).click(); await settle();
  switchIdentity(); await settle(); finish(json({ allocation_id: "cal_a", contact_point_id: "cp_a", result: "verified" })); await settle();
  expect(host.textContent).not.toContain("sales@company.test"); expect(host.textContent).not.toContain("已验证");
});
it("an unknown result never automatically repeats a paid query", async () => {
  const { host, writes } = await mount({ post: async () => { throw new Error("offline"); } });
  (host.querySelector('[data-action="verify"]') as HTMLButtonElement).click(); await settle();
  expect(writes).toHaveLength(1); expect(host.textContent).toContain("待核对");
  const button = host.querySelector('[data-action="verify"]') as HTMLButtonElement;
  expect(button.disabled).toBe(true); button.click(); await settle();
  expect(writes).toHaveLength(1);
});
it.each([401, 403])("clears contacts and disables reads after authorization failure %s", async status => {
  const { host, writes } = await mount({ post: async () => json({ code: "permission_denied" }, status) });
  (host.querySelector('[data-action="verify"]') as HTMLButtonElement).click(); await settle();
  expect(host.textContent).not.toContain("sales@company.test");
  expect(host.querySelector('[data-action="verify"]')).toBeNull();
  expect((host.querySelector("button") as HTMLButtonElement).disabled).toBe(true);
  expect(writes).toHaveLength(1);
});
it.each([400, 503])("keeps an ambiguous provider response %s pending", async status => {
  const { host, writes } = await mount({ post: async () => json({ code: "request_error" }, status) });
  (host.querySelector('[data-action="verify"]') as HTMLButtonElement).click(); await settle();
  expect((host.querySelector('[data-action="verify"]') as HTMLButtonElement).disabled).toBe(true);
  expect(host.textContent).toContain("请勿重复调用"); expect(writes).toHaveLength(1);
});
it("only a newer server observation releases the same contact's pending state", async () => {
  let observed = "2026-08-01T13:00:00Z";
  const { host, writes } = await mount({ post: async () => { throw new Error("offline"); }, read: () => {
    const current = view(); return { ...current, contacts: [{ ...current.contacts[0], contact: {
      contact_point: { ...point, verification_checked_at: observed, verification: "risky" } } }] };
  } });
  (host.querySelector('[data-action="verify"]') as HTMLButtonElement).click(); await settle();
  expect((host.querySelector('[data-action="verify"]') as HTMLButtonElement).disabled).toBe(true);
  observed = "2026-10-14T13:00:00Z";
  (host.querySelector("button") as HTMLButtonElement).click(); await settle();
  expect((host.querySelector('[data-action="verify"]') as HTMLButtonElement).disabled).toBe(false);
  expect(host.textContent).toContain("新的验证观察"); expect(writes).toHaveLength(1);
});
it("keeps pending contact ids separate while another contact is selected", async () => {
  const { host, writes } = await mount({ post: async () => { throw new Error("offline"); }, read: () => {
    const current = view(); return { ...current, contacts: [...current.contacts,
      { ...current.contacts[0], contact: { contact_point: { ...point, contact_point_id: "cp_b" } } }] };
  } });
  let buttons = host.querySelectorAll<HTMLButtonElement>('[data-action="verify"]');
  buttons[0]!.click(); await settle();
  buttons = host.querySelectorAll<HTMLButtonElement>('[data-action="verify"]');
  expect(buttons[0]!.disabled).toBe(true); expect(buttons[1]!.disabled).toBe(false);
  buttons[1]!.click(); await settle();
  buttons = host.querySelectorAll<HTMLButtonElement>('[data-action="verify"]');
  expect(buttons[0]!.disabled).toBe(true); expect(buttons[1]!.disabled).toBe(true);
  expect(writes).toHaveLength(2);
});
it("labels recorded privacy refusal as local erasure recovery despite provider blockers", async () => {
  const { host, writes } = await mount({ read: () => {
    const current = view(false); return { ...current, can_verify: true, contacts: [{ ...current.contacts[0],
      can_verify: true, blocking_reasons: [], contact: { contact_point: { ...point,
        verification_cost_note: "hunter.email_verifier.privacy_refused", verification_checked_at: "2026-08-01T13:00:00Z" } } }] };
  }, post: async () => { throw new Error("local erasure result unknown"); } });
  let button = host.querySelector('[data-action="verify"]') as HTMLButtonElement;
  expect(button.textContent).toContain("继续清除联系方式"); expect(button.disabled).toBe(false);
  button.click(); await settle();
  button = host.querySelector('[data-action="verify"]') as HTMLButtonElement;
  expect(button.textContent).toContain("继续清除联系方式"); expect(button.disabled).toBe(false);
  expect(writes).toHaveLength(1);
});
