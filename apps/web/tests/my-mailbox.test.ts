import { createApp, defineComponent, h, nextTick, type App } from "vue";
import { afterEach, expect, it } from "vitest";
import MyMailbox from "../src/views/inbox/MyMailbox.vue";
import { createApiClient, type WebRequestIdentity } from "../src/api/client";

const box = { mailbox_id: "mbx_test", email: "owner@example.test", phase: "catch_up", message_count: 51, last_synced_at: null, last_attempt_at: null, failure_code: null, sync_requested: false };
const thread = { thread_id: "a1", subject: "太阳能风扇询价", sender: "Buyer", snippet: "Catalogue please", latest_at: "2026-09-20T10:00:00Z", message_count: 2, unread: true };
const message = { message_id: "a2", thread_id: "a1", occurred_at: thread.latest_at, labels: ["SENT"], subject: thread.subject, sender: "Owner", recipients: "Buyer", snippet: "", body_text: '<script>secret()</script> catalogue', attachments: [], source_sha256: "test-hash", source_url: "https://mail.google.com/mail/u/0/#all/a2" };
let app: App | undefined;
let root: HTMLDivElement;
function json(value: unknown, status = 200) { return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } }); }
async function flush() { for (let i = 0; i < 12; i++) { await nextTick(); await new Promise(resolve => setTimeout(resolve, 0)); } }
afterEach(() => { app?.unmount(); root?.remove(); app = undefined; });
async function mount(handler: (url: URL, request: Request) => Promise<Response>) {
  let generation = 0;
  let identity: WebRequestIdentity | null = { tenantId: "tenant_test", employeeId: "employee_test", mode: "fixed-dev" };
  const listeners = new Set<() => void>();
  const client = createApiClient({ baseUrl: "http://localhost", fetch: request => handler(new URL(request.url), request) }, {
    current: () => identity, generation: () => generation,
    subscribe: listener => { listeners.add(listener); return () => listeners.delete(listener); },
  });
  root = document.createElement("div"); document.body.append(root);
  app = createApp(MyMailbox); app.component("RouterLink", defineComponent({ setup: (_, { slots }) => () => h("a", slots.default?.()) }));
  app.provide("tradeos-api-client", client); app.mount(root); await flush();
  return () => { generation++; identity = null; for (const listener of listeners) listener(); };
}
function button(text: string) { return [...root.querySelectorAll("button")].find(node => node.textContent?.trim() === text)!; }

it("shows full mailbox progress, sends filters to server, pages threads and safely renders text", async () => {
  const requests: URL[] = [];
  await mount(async url => {
    requests.push(url);
    if (url.pathname === "/inbox/mailboxes") return json([box]);
    if (url.pathname.endsWith("/threads")) return json({ items: [thread], next_offset: url.searchParams.get("offset") === "50" ? null : 50 });
    return json({ items: [message], next_offset: null });
  });
  expect(root.textContent).toContain("历史邮件尚未全部补齐");
  expect(root.textContent).toContain("太阳能风扇询价");
  button("下一页").click(); await flush();
  expect(requests.some(url => url.searchParams.get("offset") === "50")).toBe(true);
  root.querySelector<HTMLButtonElement>(".mailbox-thread")!.click(); await flush();
  expect(root.textContent).toContain("已发送");
  expect(root.textContent).toContain("<script>secret()</script>");
  expect(root.querySelector("script")).toBeNull();
  const input = root.querySelector<HTMLInputElement>('[aria-label="搜索邮件"]')!;
  input.value = "Mishka"; input.dispatchEvent(new Event("input", { bubbles: true }));
  root.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true })); await flush();
  expect(requests.some(url => url.searchParams.get("search") === "Mishka")).toBe(true);
});

it("clears private mail on identity change and rejects late responses", async () => {
  let resolve: (response: Response) => void = () => {};
  const logout = await mount(async url => {
    if (url.pathname === "/inbox/mailboxes") return json([box]);
    if (url.pathname.endsWith("/threads")) return json({ items: [thread], next_offset: null });
    return new Promise<Response>(done => { resolve = done; });
  });
  root.querySelector<HTMLButtonElement>(".mailbox-thread")!.click(); await flush();
  logout(); resolve(json({ items: [message], next_offset: null })); await flush();
  expect(root.textContent).not.toContain("owner@example.test");
  expect(root.textContent).not.toContain("catalogue");
});

it("does not turn a server failure into an empty mailbox", async () => {
  await mount(async () => json({}, 503));
  expect(root.querySelector('[role="alert"]')?.textContent).toContain("暂不可用");
  expect(root.textContent).not.toContain("尚未连接邮箱");
});
