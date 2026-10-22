import { createApp, nextTick, type App } from "vue";
import { afterEach, expect, it, vi } from "vitest";
import PrivateReplyTestSetup from "../src/components/PrivateReplyTestSetup.vue";
import { createApiClient, configureAuthenticatedIdentity, clearAuthenticatedIdentity } from "../src/api/client";

const apps: App[] = [];
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { "content-type": "application/json" },
});
async function flush() {
  for (let i = 0; i < 10; i++) { await nextTick(); await new Promise(resolve => setTimeout(resolve, 0)); }
}
function mount(fetch: typeof globalThis.fetch) {
  configureAuthenticatedIdentity("tenant-fixture", "owner-fixture");
  const prepared = vi.fn();
  const root = document.createElement("div");
  document.body.append(root);
  const app = createApp(PrivateReplyTestSetup, { onPrepared: prepared });
  app.provide("tradeos-api-client", createApiClient({ baseUrl: "https://fixture.test", fetch }));
  app.mount(root); apps.push(app);
  return { root, prepared };
}
const options = { enabled: true, options: [{ campaign_id: "cmp_fixture", sending_identity_id: "sid_fixture", name: "已批准单封任务" }] };
function choose(root: HTMLElement) {
  const select = root.querySelector("select")!;
  select.value = "cmp_fixture"; select.dispatchEvent(new Event("change"));
  const consent = root.querySelector<HTMLInputElement>('input[type="checkbox"]')!;
  consent.checked = true; consent.dispatchEvent(new Event("change"));
}
afterEach(() => { apps.splice(0).forEach(app => app.unmount()); clearAuthenticatedIdentity(); document.body.replaceChildren(); });

it("requires explicit consent and only prepares through the original flow", async () => {
  const calls: Request[] = [];
  const fetch = vi.fn<typeof globalThis.fetch>(async input => {
    const request = input as Request; calls.push(request);
    return response(request.method === "GET" ? options : { enrollment_id: "enr_fixture", purpose: "private_reply_test", sent: false });
  });
  const { root, prepared } = mount(fetch);
  await flush();
  expect(root.querySelector("button")!.disabled).toBe(true);
  choose(root); await nextTick(); root.querySelector("button")!.click(); await flush();
  expect(prepared).toHaveBeenCalledWith("enr_fixture");
  expect(root.textContent).toContain("尚未发送邮件");
  expect(calls.filter(c => c.method === "POST")).toHaveLength(1);
  expect(await calls[1]!.json()).toEqual({ campaign_id: "cmp_fixture", sending_identity_id: "sid_fixture", confirm_controlled_recipient: true });
  expect(calls.every(c => !new URL(c.url).pathname.includes("/send"))).toBe(true);
});

it("discards a late options response after switching identity", async () => {
  let finish!: (value: Response) => void;
  const pending = new Promise<Response>(resolve => { finish = resolve; });
  const { root } = mount(vi.fn<typeof globalThis.fetch>(() => pending));
  configureAuthenticatedIdentity("another-tenant", "another-owner");
  finish(response(options)); await flush();
  expect(root.textContent).not.toContain("1249583590");
});

it("does not emit a prepared record after identity revocation", async () => {
  let finish!: (value: Response) => void;
  const pending = new Promise<Response>(resolve => { finish = resolve; });
  const fetch = vi.fn<typeof globalThis.fetch>(input => (input as Request).method === "GET" ? Promise.resolve(response(options)) : pending);
  const { root, prepared } = mount(fetch);
  await flush(); choose(root); await nextTick(); root.querySelector("button")!.click(); await flush();
  clearAuthenticatedIdentity();
  finish(response({ enrollment_id: "enr_late", sent: false })); await flush();
  expect(prepared).not.toHaveBeenCalled();
  expect(root.textContent).not.toContain("1249583590");
});

it.each([403, 503])("does not retry preparation on %s", async status => {
  const fetch = vi.fn<typeof globalThis.fetch>(async input => (input as Request).method === "GET" ? response(options) : response({}, status));
  const { root, prepared } = mount(fetch);
  await flush(); choose(root); await nextTick(); root.querySelector("button")!.click(); await flush();
  expect(prepared).not.toHaveBeenCalled();
  expect(fetch.mock.calls.filter(([input]) => (input as Request).method === "POST")).toHaveLength(1);
  if (status === 403) expect(root.textContent).not.toContain("1249583590");
});
