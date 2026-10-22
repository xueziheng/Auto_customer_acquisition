import { createApp, nextTick, type App } from "vue";
import { afterEach, expect, it, vi } from "vitest";
import { createApiClient, type WebRequestIdentity } from "../src/api/client";
import Component from "../src/views/inbox/BusinessMailFollowup.vue";

const state = { message_id: "bmi_test", revision: 2, owner_id: "employee_test", status: "in_progress", draft: null, history: [] };
let app: App | undefined;
let root: HTMLDivElement;
function json(body: unknown, status = 200) { return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }); }
async function flush() { for (let i = 0; i < 12; i++) { await nextTick(); await new Promise(resolve => setTimeout(resolve, 0)); } }
async function mount(handler: (request: Request) => Promise<Response>) {
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
  let generation = 0;
  let identity: WebRequestIdentity | null = { tenantId: "tenant_test", employeeId: "employee_test", mode: "fixed-dev" };
  const listeners = new Set<() => void>();
  const client = createApiClient({ baseUrl: "http://localhost", fetch: handler }, { current: () => identity, generation: () => generation,
    subscribe: listener => { listeners.add(listener); return () => listeners.delete(listener); } });
  root = document.createElement("div"); document.body.append(root);
  app = createApp(Component, { messageId: "bmi_test", subject: "Inquiry", sourceKind: "full_mime" }); app.provide("tradeos-api-client", client); app.mount(root); await flush();
  return () => { generation++; identity = null; for (const listener of listeners) listener(); };
}
function button(name: string) { return [...root.querySelectorAll("button")].find(b => b.textContent?.trim() === name)!; }
async function tick() { await vi.advanceTimersByTimeAsync(30000); await flush(); }
afterEach(() => { app?.unmount(); root?.remove(); app = undefined; vi.useRealTimers(); });

it("observes a background send and its source analysis after thirty seconds", async () => {
  let current: unknown = state;
  const reads: Request[] = [];
  await mount(async request => { reads.push(request); return json(current); });
  current = { ...state, revision: 3, status: "waiting_customer", draft: { subject: "Re: inquiry", body_text: "Question", source_sha256: "a".repeat(64) }, history: [
    { event_id: "sent1", request_key: "inquiry-sent:test", kind: "handling_updated", status: "waiting_customer", created_at: "2026-10-22T08:00:00Z", note: "后台发送记录" },
    { event_id: "analysis1", request_key: "inquiry-analysis:test", kind: "handling_updated", status: "in_progress", created_at: "2026-10-22T08:00:00Z", note: JSON.stringify({ source_id: "source1", analysis: { reason: "等待核对", fields: [{ name: "quantity", value: "100 units", evidence_quote: "100 units" }] } }) },
  ] };
  await tick();
  expect(reads).toHaveLength(2);
  expect(root.textContent).toContain("邮件服务已接受发送");
  expect(root.textContent).toContain("100 units");
  expect(root.textContent).toContain("待负责人核对");
});

it.each(["回复主题", "回复正文", "处理说明", "处理状态"])("does not poll over an edited %s", async label => {
  let reads = 0;
  await mount(async () => { reads++; return json(state); });
  const input = root.querySelector<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>(`[aria-label="${label}"]`)!;
  input.value = input.tagName === "SELECT" ? "waiting_customer" : "unsaved edit";
  input.dispatchEvent(new Event(input.tagName === "SELECT" ? "change" : "input")); await nextTick();
  await tick();
  expect(reads).toBe(1);
  expect(input.value).toBe(input.tagName === "SELECT" ? "waiting_customer" : "unsaved edit");
});

it("keeps an uncertain write for explicit recovery without background reads or resubmission", async () => {
  let reads = 0, writes = 0;
  await mount(async request => {
    if (request.method === "POST") { writes++; throw new Error("uncertain"); }
    reads++; return json(state);
  });
  const note = root.querySelector<HTMLTextAreaElement>('[aria-label="处理说明"]')!;
  note.value = "待核对"; note.dispatchEvent(new Event("input")); await nextTick();
  button("等待客户补充").click(); await flush();
  expect(root.textContent).toContain("保存结果待核对");
  await tick(); expect(reads).toBe(1); expect(writes).toBe(1);
});

it("does not overlap reads and discards the pending response after identity revocation", async () => {
  let reads = 0;
  let resolveRead: ((value: Response) => void) | undefined;
  const revoke = await mount(async () => {
    reads++; if (reads === 1) return json(state);
    return new Promise<Response>(resolve => { resolveRead = resolve; });
  });
  await tick(); expect(reads).toBe(2);
  await tick(); expect(reads).toBe(2);
  revoke(); await flush(); expect(vi.getTimerCount()).toBe(0);
  resolveRead!(json({ ...state, history: [{ event_id: "late", request_key: "inquiry-sent:late", kind: "handling_updated", status: "waiting_customer", created_at: "2026-10-22T08:00:00Z", note: "stale private record" }] }));
  await flush(); await tick();
  expect(reads).toBe(2); expect(root.textContent).not.toContain("stale private record");
});

it("removes polling on unmount and after a permission denial", async () => {
  let reads = 0;
  await mount(async () => { reads++; return reads === 1 ? json(state) : json({}, 403); });
  await tick(); expect(reads).toBe(2); expect(vi.getTimerCount()).toBe(0);
  await tick(); expect(reads).toBe(2);
  app!.unmount(); app = undefined; expect(vi.getTimerCount()).toBe(0);
});

it("clears a still active interval on unmount", async () => {
  let reads = 0;
  await mount(async () => { reads++; return json(state); });
  expect(vi.getTimerCount()).toBe(1);
  app!.unmount(); app = undefined;
  expect(vi.getTimerCount()).toBe(0);
  await tick(); expect(reads).toBe(1);
});
