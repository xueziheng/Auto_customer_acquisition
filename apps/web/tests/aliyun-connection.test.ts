import { createApp, nextTick, type App } from "vue";
import { afterEach, expect, it } from "vitest";
import { createApiClient } from "../src/api/client";
import AliyunConnectionPanel from "../src/views/inbox/AliyunConnectionPanel.vue";
let app: App | undefined;
afterEach(() => { app?.unmount(); document.body.replaceChildren(); });
async function settle() { for (let i = 0; i < 10; i++) { await nextTick(); await new Promise(r => setTimeout(r, 0)); } }
it("连接只提交到本人接口，提交后立即清空密码且显示真实认证时间", async () => {
  let submitted: unknown;
  const client = createApiClient({ baseUrl: "https://trade.test", fetch: async request => {
    const r = request as Request;
    if (r.method === "POST") submitted = await r.json();
    return new Response(JSON.stringify(r.method === "POST"
      ? { configured: true, email: "sales@example.com", connected_at: "2026-10-14T08:00:00Z", smtp_authenticated: true, imap_authenticated: true }
      : { configured: true, email: null }), { headers: { "Content-Type": "application/json" } });
  } }, { current: () => ({ tenantId: "tn_a", employeeId: "emp_a", mode: "fixed-dev" }), generation: () => 0, subscribe: () => () => {} });
  const host = document.createElement("div"); document.body.append(host);
  app = createApp(AliyunConnectionPanel); app.provide("tradeos-api-client", client); app.mount(host);
  await settle();
  for (const [label, value] of [["阿里邮箱地址", "sales@example.com"], ["阿里邮箱客户端专用密码", "synthetic-only"]]) {
    const input = host.querySelector<HTMLInputElement>(`input[aria-label="${label}"]`)!;
    input.value = value; input.dispatchEvent(new Event("input"));
  }
  host.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click(); await settle();
  host.querySelector<HTMLButtonElement>("button")!.click(); await settle();
  expect(submitted).toEqual({ email: "sales@example.com", password: "synthetic-only", confirm_connect: true });
  expect(host.querySelector<HTMLInputElement>('input[type="password"]')!.value).toBe("");
  expect(host.textContent).toContain("sales@example.com");
  expect(host.textContent).toContain("已通过收发认证");
  expect(host.textContent).not.toContain("邮件已发送");
});

it("权限撤回时清除邮箱和凭证并禁止继续提交", async () => {
  let reads = 0;
  const client = createApiClient({ baseUrl: "https://trade.test", fetch: async () => {
    reads++;
    return reads === 1
      ? new Response(JSON.stringify({ configured: true, email: "sales@example.com" }), { headers: { "Content-Type": "application/json" } })
      : new Response("{}", { status: 403, headers: { "Content-Type": "application/json" } });
  } }, { current: () => ({ tenantId: "tn_a", employeeId: "emp_a", mode: "fixed-dev" }), generation: () => 0, subscribe: () => () => {} });
  const host = document.createElement("div"); document.body.append(host);
  app = createApp(AliyunConnectionPanel); app.provide("tradeos-api-client", client); app.mount(host);
  await settle();
  const authorizationInput = host.querySelector<HTMLInputElement>('input[type="password"]')!;
  authorizationInput.value = "synthetic-only"; authorizationInput.dispatchEvent(new Event("input"));
  [...host.querySelectorAll("button")].find(b => b.textContent?.trim() === "刷新阿里邮箱状态")!.click();
  await settle();
  expect(host.textContent).not.toContain("sales@example.com");
  expect(host.querySelector<HTMLInputElement>('input[type="password"]')?.value ?? "").toBe("");
  expect(host.querySelector<HTMLButtonElement>("button")?.disabled ?? true).toBe(true);
});
