import { createApp, nextTick, type App } from "vue";
import { afterEach, expect, it } from "vitest";
import { createApiClient, type WebRequestIdentity } from "../src/api/client";
import Component from "../src/views/inbox/BusinessMailFollowup.vue";

const state = { message_id: "bmi_test", revision: 2, owner_id: "employee_test", status: "in_progress", draft: null, history: [] };
let app: App | undefined;
let root: HTMLDivElement;
function json(body: unknown, status = 200) { return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }); }
async function flush() { for (let i = 0; i < 12; i++) { await nextTick(); await new Promise(resolve => setTimeout(resolve, 0)); } }
async function mount(handler: (request: Request) => Promise<Response>) {
  let generation = 0;
  let identity: WebRequestIdentity | null = { tenantId: "tenant_test", employeeId: "employee_test", mode: "fixed-dev" };
  const listeners = new Set<() => void>();
  const client = createApiClient({ baseUrl: "http://localhost", fetch: handler }, { current: () => identity, generation: () => generation,
    subscribe: listener => { listeners.add(listener); return () => listeners.delete(listener); } });
  root = document.createElement("div"); document.body.append(root);
  app = createApp(Component, { messageId: "bmi_test" }); app.provide("tradeos-api-client", client); app.mount(root); await flush();
  return () => { generation++; identity = null; for (const listener of listeners) listener(); };
}
function button(name: string) { return [...root.querySelectorAll("button")].find(b => b.textContent?.trim() === name)!; }
async function enter(text: string) { const node = root.querySelector("textarea")!; node.value = text; node.dispatchEvent(new Event("input")); await nextTick(); }
afterEach(() => { app?.unmount(); root?.remove(); app = undefined; });

it("keeps an uncertain handling request and retries the exact original command", async () => {
  const posts: { key: string | null; body: string }[] = [];
  await mount(async request => {
    if (request.method === "POST") { posts.push({ key: request.headers.get("Idempotency-Key"), body: await request.text() }); throw new Error("network"); }
    return json(state);
  });
  await enter("已核对测试来源，等待补充"); button("等待客户补充").click(); await flush();
  expect(root.textContent).toContain("保存结果待核对"); expect(button("完成本次处理").disabled).toBe(true);
  button("刷新处理记录").click(); await flush(); expect(posts).toHaveLength(1);
  button("恢复原处理请求").click(); await flush(); expect(posts).toHaveLength(2); expect(posts[1]).toEqual(posts[0]);
});

it("shows source quotes as unconfirmed and clears all data after identity changes", async () => {
  const revoke = await mount(async () => json({ ...state, history: [{ event_id: "event1", request_key: "inquiry-analysis:test", kind: "handling_updated", status: "in_progress",
    created_at: "2026-10-21T08:00:00Z", note: JSON.stringify({ source_id: "source1", confirmed_by: null, analysis: { reason: "提取测试数量", fields: [{ name: "quantity", value: "100 units", evidence_quote: "100 units" }] } }) }] }));
  expect(root.textContent).toContain("待负责人核对"); expect(root.textContent).toContain("100 units");
  revoke(); await flush(); expect(root.textContent).not.toContain("100 units"); expect(root.querySelector("textarea")).toBeNull();
});

it("does not present a saved draft as sent", async () => {
  await mount(async () => json({ ...state, draft: { subject: "Re: inquiry", body_text: "Question", source_sha256: "a".repeat(64) } }));
  expect(root.textContent).toContain("草稿，尚无成功发送记录"); expect(root.textContent).not.toContain("邮件服务已接受发送");
});
