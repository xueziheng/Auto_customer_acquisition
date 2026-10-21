import { createApp, nextTick, type App, type Component } from "vue";
import { afterEach, expect, it, vi } from "vitest";
import { createApiClient, type WebRequestIdentity } from "../src/api/client";
import router from "../src/router";
import { workspaceGroups } from "../src/navigation";

const item = {
  message_id: "bmi_test", mailbox_id: "mbx_test", mailbox_address: "owner@example.test",
  owner_id: "employee_test", sender: "buyer@example.test", subject: "Purchase inquiry",
  snippet: "Please quote 100 fans", received_at: "2026-10-21T00:00:00Z",
  category: "pending", classification_status: "pending", revision: 1,
  contact_status: "unconfirmed", failure_code: null,
};
const detail = {
  ...item, body_text: "Please quote 100 fans. <script>unsafe()</script>",
  source: { provider: "qq", folder: "INBOX", uidvalidity: 1, uid: 21,
    source_kind: "full_mime", source_sha256: "a".repeat(64), mime_sha256: "a".repeat(64), artifact_id: "raw_test", received_at: item.received_at },
  classification_history: [],
};
let app: App | undefined;
let root: HTMLDivElement;
function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}
async function flush() { for (let n = 0; n < 12; n++) { await nextTick(); await new Promise(resolve => setTimeout(resolve, 0)); } }
async function mount(handler: (url: URL, request: Request) => Promise<Response>) {
  const route = router.getRoutes().find(candidate => candidate.path === "/inbox/business-mail");
  expect(route, "普通来信必须有可用的消息页入口").toBeDefined();
  const loader = route!.components!.default as () => Promise<{ default: Component }>;
  const component = (await loader()).default;
  let generation = 0;
  let identity: WebRequestIdentity | null = { tenantId: "tenant_test", employeeId: "employee_test", mode: "fixed-dev" };
  const listeners = new Set<() => void>();
  const client = createApiClient({ baseUrl: "http://localhost", fetch: request => handler(new URL(request.url), request) }, {
    current: () => identity, generation: () => generation,
    subscribe: listener => { listeners.add(listener); return () => listeners.delete(listener); },
  });
  root = document.createElement("div"); document.body.append(root);
  app = createApp(component); app.provide("tradeos-api-client", client); app.mount(root); await flush();
  return () => { generation++; identity = null; for (const listener of listeners) listener(); };
}
function button(label: string) { return [...root.querySelectorAll("button")].find(node => node.textContent?.trim() === label)!; }
afterEach(() => { app?.unmount(); root?.remove(); app = undefined; vi.restoreAllMocks(); });

it("makes first mail visible before classification, keeps the contact unconfirmed and renders text safely", async () => {
  expect(workspaceGroups.find(group => group.id === "messages")!.links.some(link => link.to === "/inbox/business-mail")).toBe(true);
  await mount(async url => json(url.pathname.endsWith("bmi_test") ? detail : { items: [item], next_offset: null }));
  expect(root.textContent).toContain("Purchase inquiry");
  expect(root.textContent).toContain("待识别");
  root.querySelector<HTMLButtonElement>(".business-mail-item")!.click(); await flush();
  expect(root.textContent).toContain("待确认联系人");
  expect(root.textContent).toContain("<script>unsafe()</script>");
  expect(root.querySelector("script")).toBeNull();
  expect(root.textContent).toContain("raw_test");
  expect(root.textContent).not.toContain("已验证需求");
});

it("keeps failed classification visible and sends filters and page offsets to the server", async () => {
  const requests: URL[] = [];
  await mount(async url => {
    requests.push(url);
    return json({ items: [{ ...item, category: "review", classification_status: "failed", failure_code: "quota" }], next_offset: 50 });
  });
  expect(root.textContent).toContain("识别额度不足");
  button("下一页").click(); await flush();
  expect(requests.at(-1)?.searchParams.get("offset")).toBe("50");
  button("待人工确认").click(); await flush();
  expect(requests.at(-1)?.searchParams.get("bucket")).toBe("review");
  expect(requests.at(-1)?.searchParams.get("offset")).toBe("0");
});

it("records a correction with its observed revision and preserves the model judgement", async () => {
  const posts: unknown[] = [];
  await mount(async (url, request) => {
    if (request.method === "POST") {
      const command = await request.json(); posts.push(command);
      return json({ ...detail, category: "business", classification_status: "classified", revision: 3,
        classification_history: [
          { classification_id: "c1", category: "other", origin: "model", reason: "信息不足", evidence_quote: null, model: "fixture-model", configuration_version: "v1", actor_id: null, created_at: item.received_at },
          { classification_id: "c2", category: "business", origin: "human", reason: command.note, evidence_quote: null, model: null, configuration_version: null, actor_id: "employee_test", created_at: item.received_at },
        ] });
    }
    return json(url.pathname.endsWith("bmi_test") ? { ...detail, revision: 2 } : { items: [item], next_offset: null });
  });
  root.querySelector<HTMLButtonElement>(".business-mail-item")!.click(); await flush();
  const select = root.querySelector<HTMLSelectElement>('[aria-label="人工分类"]')!;
  select.value = "business"; select.dispatchEvent(new Event("change"));
  const note = root.querySelector<HTMLTextAreaElement>('[aria-label="纠正原因"]')!;
  note.value = "客户明确询问采购"; note.dispatchEvent(new Event("input"));
  await nextTick();
  button("保存分类").click(); await flush();
  expect(posts).toEqual([{ expected_revision: 2, category: "business", note: "客户明确询问采购" }]);
  expect(root.textContent).toContain("fixture-model");
  expect(root.textContent).toContain("人工纠正");
  expect(root.textContent).toContain("客户明确询问采购");
});

it("clears private mail and rejects a late detail response on identity change", async () => {
  let resolve: (response: Response) => void = () => {};
  const logout = await mount(async url => url.pathname.endsWith("bmi_test")
    ? new Promise<Response>(done => { resolve = done; }) : json({ items: [item], next_offset: null }));
  root.querySelector<HTMLButtonElement>(".business-mail-item")!.click(); await flush();
  logout(); resolve(json(detail)); await flush();
  expect(root.textContent).not.toContain("buyer@example.test");
  expect(root.textContent).not.toContain("Purchase inquiry");
});

it("clears both list and detail after authorization is revoked", async () => {
  await mount(async url => url.pathname.endsWith("bmi_test") ? json({}, 403) : json({ items: [item], next_offset: null }));
  root.querySelector<HTMLButtonElement>(".business-mail-item")!.click(); await flush();
  expect(root.textContent).toContain("当前身份无权访问");
  expect(root.textContent).not.toContain("buyer@example.test");
});

it("does not present a service failure as an empty mailbox", async () => {
  await mount(async () => json({}, 503));
  expect(root.textContent).toContain("来信服务暂不可用");
  expect(root.textContent).not.toContain("暂无来信");
});

it("labels oversized mail as headers only without claiming the complete original was saved", async () => {
  const oversized = { ...detail, category: "review", failure_code: "oversize", body_text: "",
    source: { ...detail.source, source_kind: "headers_only", mime_sha256: null } };
  await mount(async url => json(url.pathname.endsWith("bmi_test") ? oversized : { items: [{ ...item, category: "review" }], next_offset: null }));
  root.querySelector<HTMLButtonElement>(".business-mail-item")!.click(); await flush();
  expect(root.textContent).toContain("仅保存邮件头");
  expect(root.textContent).toContain("未提交模型识别");
});

it("requires reading the saved record after an unknown correction result and never retries automatically", async () => {
  let posts = 0;
  await mount(async (url, request) => {
    if (request.method === "POST") { posts++; throw new TypeError("network unavailable"); }
    return json(url.pathname.endsWith("bmi_test") ? detail : { items: [item], next_offset: null });
  });
  root.querySelector<HTMLButtonElement>(".business-mail-item")!.click(); await flush();
  const note = root.querySelector<HTMLTextAreaElement>('[aria-label="纠正原因"]')!;
  note.value = "确认是采购询问"; note.dispatchEvent(new Event("input")); await nextTick();
  button("保存分类").click(); await flush();
  expect(posts).toBe(1);
  expect(root.textContent).toContain("保存结果待核对");
  expect(button("保存分类").disabled).toBe(true);
  button("刷新邮件").click(); await flush();
  expect(posts).toBe(1);
  expect(button("保存分类").disabled).toBe(false);
});

it("does not apply a late correction failure to a newly selected mail", async () => {
  let reject: (error: Error) => void = () => {};
  await mount(async (url, request) => {
    if (request.method === "POST") return new Promise<Response>((_, fail) => { reject = fail; });
    if (url.pathname.endsWith("bmi_second")) return json({ ...detail, message_id: "bmi_second", subject: "Second inquiry" });
    return json(url.pathname.endsWith("bmi_test") ? detail : { items: [item, { ...item, message_id: "bmi_second", subject: "Second inquiry" }], next_offset: null });
  });
  root.querySelector<HTMLButtonElement>(".business-mail-item")!.click(); await flush();
  const note = root.querySelector<HTMLTextAreaElement>('[aria-label="纠正原因"]')!;
  note.value = "确认是采购询问"; note.dispatchEvent(new Event("input")); await nextTick();
  button("保存分类").click(); await flush();
  root.querySelectorAll<HTMLButtonElement>(".business-mail-item")[1]!.click(); await flush();
  reject(new Error("late failure")); await flush();
  expect(root.querySelector("h2")?.textContent).toBe("Second inquiry");
  expect(root.textContent).not.toContain("保存结果待核对");
});

it("keeps list refresh usable when selecting a message during an unfinished refresh", async () => {
  let reads = 0;
  let resolve: (response: Response) => void = () => {};
  await mount(async url => {
    if (url.pathname.endsWith("bmi_test")) return json(detail);
    if (++reads > 1) return new Promise<Response>(done => { resolve = done; });
    return json({ items: [item], next_offset: null });
  });
  button("刷新列表").click(); await flush();
  root.querySelector<HTMLButtonElement>(".business-mail-item")!.click(); await flush();
  resolve(json({ items: [], next_offset: null })); await flush();
  expect(button("刷新列表").disabled).toBe(false);
  expect(root.querySelector("h2")?.textContent).toBe("Purchase inquiry");
});

it("keeps the service error visible while a background retry is unfinished", async () => {
  let poll: () => void = () => {};
  vi.spyOn(globalThis, "setInterval").mockImplementation(callback => {
    poll = callback as () => void;
    return 1 as unknown as ReturnType<typeof globalThis.setInterval>;
  });
  let requests = 0;
  let resolve: (response: Response) => void = () => {};
  await mount(async () => ++requests === 1 ? json({}, 503) : new Promise<Response>(done => { resolve = done; }));
  expect(root.textContent).toContain("来信服务暂不可用");
  poll(); await flush();
  expect(root.textContent).toContain("来信服务暂不可用");
  expect(root.textContent).not.toContain("暂无来信");
  resolve(json({ items: [], next_offset: null })); await flush();
  expect(root.textContent).toContain("暂无来信");
  expect(root.textContent).not.toContain("来信服务暂不可用");
});
