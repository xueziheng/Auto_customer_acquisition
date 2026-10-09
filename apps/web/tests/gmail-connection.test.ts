import { createApp, nextTick, type App } from "vue";
import { afterEach, expect, it, vi } from "vitest";
import GmailConnectionPanel from "../src/views/inbox/GmailConnectionPanel.vue";
import { createApiClient, type WebRequestIdentity } from "../src/api/client";

let app: App | undefined;
let root: HTMLDivElement;
function json(value: unknown) { return new Response(JSON.stringify(value), { headers: { "Content-Type": "application/json" } }); }
async function flush() { for (let i = 0; i < 12; i++) { await nextTick(); await new Promise(resolve => setTimeout(resolve, 0)); } }
afterEach(() => { app?.unmount(); root?.remove(); app = undefined; vi.restoreAllMocks(); });
async function mount(handler: (request: Request) => Promise<Response>, navigate = vi.fn()) {
  let generation = 0;
  let identity: WebRequestIdentity | null = { tenantId: "tenant_test", employeeId: "employee_test", mode: "fixed-dev" };
  const listeners = new Set<() => void>();
  const client = createApiClient({ baseUrl: "http://localhost", fetch: handler }, {
    current: () => identity, generation: () => generation,
    subscribe: listener => { listeners.add(listener); return () => listeners.delete(listener); },
  });
  root = document.createElement("div"); document.body.append(root);
  app = createApp(GmailConnectionPanel);
  app.provide("tradeos-api-client", client); app.provide("tradeos-gmail-navigate", navigate);
  app.mount(root); await flush();
  return () => { generation++; identity = null; for (const listener of listeners) listener(); };
}
function button(text: string) { return [...root.querySelectorAll("button")].find(node => node.textContent?.trim() === text)!; }
function input(label: string, value: string) {
  const element = root.querySelector<HTMLInputElement>('[aria-label="' + label + '"]')!;
  element.value = value; element.dispatchEvent(new Event("input", { bubbles: true }));
}

it("starts explicit authorization and accepts only the official Google destination", async () => {
  const navigate = vi.fn();
  let sent: unknown;
  await mount(async request => {
    if (request.url.endsWith("/status")) return json({ configured: true, email: null, mailbox_id: null, can_test: false });
    sent = await request.json();
    return json({ authorization_url: "https://accounts.google.com/o/oauth2/auth?state=synthetic" });
  }, navigate);
  input("Gmail 地址", "owner@gmail.com"); await nextTick();
  button("连接 Gmail").click(); await flush();
  expect(sent).toEqual({ email: "owner@gmail.com" });
  expect(navigate).toHaveBeenCalledWith("https://accounts.google.com/o/oauth2/auth?state=synthetic");
});

it("requires preview and explicit ownership confirmation, then preserves the send key", async () => {
  const sends: Record<string, unknown>[] = [];
  await mount(async request => {
    if (request.url.endsWith("/status")) return json({ configured: true, email: "owner@gmail.com", mailbox_id: "mbx_test", can_test: true });
    if (request.url.endsWith("/template")) return json({ subject: "Fixed diagnostic", body: "Please reply to test receiving." });
    if (request.method === "GET") return json(null);
    sends.push(await request.json());
    return json({ request_id: sends[0]!.request_id, sender: "owner@gmail.com", recipient: "recipient@example.com",
      subject: "Fixed diagnostic", body: "Please reply to test receiving.", status: "succeeded",
      tool_call_id: "tc_test", provider_ref: "provider_test", created_at: "2026-10-09T00:00:00Z" });
  });
  input("测试收件邮箱", "recipient@example.com"); await nextTick();
  button("预览测试邮件").click(); await flush();
  expect(sends).toHaveLength(0);
  expect(root.textContent).toContain("Please reply to test receiving.");
  expect(button("确认发送一封测试邮件").disabled).toBe(true);
  root.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click(); await flush();
  button("确认发送一封测试邮件").click(); await flush();
  expect(sends).toHaveLength(1);
  expect(sends[0]!.email).toBe("recipient@example.com");
  expect(sends[0]!.confirm_my_mailbox).toBe(true);
  expect(root.textContent).toContain("Gmail 已接受");
  expect(root.textContent).toContain("需要收件人确认");
});

it("clears private connection data on identity change and refuses a late authorization redirect", async () => {
  let resolve: (response: Response) => void = () => {};
  const navigate = vi.fn();
  const logout = await mount(async request => {
    if (request.url.endsWith("/status")) return json({ configured: true, email: null, mailbox_id: null, can_test: false });
    return new Promise<Response>(done => { resolve = done; });
  }, navigate);
  input("Gmail 地址", "owner@gmail.com"); await nextTick(); button("连接 Gmail").click(); await flush();
  logout(); resolve(json({ authorization_url: "https://accounts.google.com/o/oauth2/auth" })); await flush();
  expect(navigate).not.toHaveBeenCalled();
  expect(root.textContent).not.toContain("owner@gmail.com");
});

it("does not navigate to a destination supplied by an invalid response", async () => {
  const navigate = vi.fn();
  await mount(async request => request.url.endsWith("/status")
    ? json({ configured: true, email: null, mailbox_id: null, can_test: false })
    : json({ authorization_url: "https://example.test/phishing" }), navigate);
  input("Gmail 地址", "owner@gmail.com"); await nextTick(); button("连接 Gmail").click(); await flush();
  expect(navigate).not.toHaveBeenCalled();
  expect(root.querySelector('[role="alert"]')).not.toBeNull();
});
