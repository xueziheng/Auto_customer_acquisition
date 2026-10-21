import { createApp, nextTick, type App } from "vue";
import { afterEach, expect, it } from "vitest";
import { createApiClient, type WebRequestIdentity } from "../src/api/client";
import BusinessMail from "../src/views/inbox/BusinessMail.vue";

const stamp = "2026-10-21T00:00:00Z";
const item = { message_id: "bmi_one", mailbox_id: "box_one", mailbox_address: "owner@example.test", owner_id: "emp_owner", sender: "customer@example.test", subject: "Product inquiry", snippet: "Please send details.", received_at: stamp, category: "business", classification_status: "classified", revision: 1, contact_status: "unconfirmed", failure_code: null };
const mail = { ...item, body_text: "Please send details.", source: { provider: "qq", folder: "INBOX", uidvalidity: 1, uid: 1, source_kind: "full_mime", source_sha256: "a".repeat(64), mime_sha256: "a".repeat(64), artifact_id: "art_one", received_at: stamp }, classification_history: [] };
type MailFixture = Omit<typeof mail, "source"> & { source: Omit<typeof mail.source, "mime_sha256"> & { mime_sha256: string | null } };
const initial = { message_id: "bmi_one", revision: 0, owner_id: "emp_owner", status: "pending", draft: null, history: [] };
let app: App | undefined; let root: HTMLDivElement;
function json(data: unknown, status = 200) { return new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } }); }
async function flush() { for (let n = 0; n < 10; n++) { await nextTick(); await new Promise(resolve => setTimeout(resolve, 0)); } }
function button(label: string) { return [...root.querySelectorAll("button")].find(node => node.textContent?.trim() === label)!; }
function field(label: string, value: string) { const node = root.querySelector<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>(`[aria-label="${label}"]`)!; node.value = value; node.dispatchEvent(new Event(node.tagName === "SELECT" ? "change" : "input")); }
async function mount(handler: (url: URL, request: Request) => Promise<Response>, details: MailFixture = mail) {
 let generation = 0; let identity: WebRequestIdentity | null = { tenantId: "tenant_one", employeeId: "emp_owner", mode: "fixed-dev" };
 const listeners = new Set<() => void>();
 const client = createApiClient({ baseUrl: "http://localhost", fetch: async request => {
  const url = new URL(request.url);
  if (url.pathname === "/inbox/business-mail") return json({ items: [item, { ...item, message_id: "bmi_two", subject: "Second inquiry" }], next_offset: null });
  if (url.pathname.endsWith("/bmi_one")) return json(details);
  if (url.pathname.endsWith("/bmi_two")) return json({ ...details, message_id: "bmi_two", subject: "Second inquiry" });
  return handler(url, request);
 } }, { current: () => identity, generation: () => generation, subscribe: listener => { listeners.add(listener); return () => listeners.delete(listener); } });
 root = document.createElement("div"); document.body.append(root); app = createApp(BusinessMail); app.provide("tradeos-api-client", client); app.mount(root); await flush();
 root.querySelector<HTMLButtonElement>(".business-mail-item")!.click(); await flush();
 return () => { generation++; identity = null; for (const listener of listeners) listener(); };
}
afterEach(() => { app?.unmount(); root?.remove(); app = undefined; });

it("saves an English draft with one request identity and reads the persisted draft after refresh without claiming mail was sent", async () => {
 let saved: typeof initial | Record<string, unknown> = initial;
 const writes: { key: string | null; body: unknown }[] = [];
 await mount(async (url, request) => {
  if (request.method === "POST") { const body = await request.json(); writes.push({ key: request.headers.get("Idempotency-Key"), body });
   saved = { ...initial, revision: 1, draft: { draft_id: "draft_one", subject: body.subject, body_text: body.body_text, source_sha256: "a".repeat(64), created_by: "emp_owner", created_at: stamp }, history: [{ event_id: "event_one", kind: "draft_saved", revision: 1, status: "pending", note: "保存回复草稿", actor_id: "emp_owner", created_at: stamp, draft_id: "draft_one", request_key: writes[0].key }] };
  }
  return json(saved);
 });
 expect(root.textContent).toContain("回复草稿");
 button("使用询盘模板").click(); await nextTick();
 const body = root.querySelector<HTMLTextAreaElement>('[aria-label="回复正文"]')!.value;
 expect(body).toContain("quantity"); expect(body).toContain("destination"); expect(body).not.toMatch(/MOQ|minimum order|25W|100 units/);
 button("保存草稿").click(); await flush();
 expect(writes).toHaveLength(1); expect(writes[0].key?.length).toBeGreaterThan(7);
 expect(writes[0].body).toEqual({ expected_revision: 0, subject: "Re: Product inquiry", body_text: body });
 button("刷新处理记录").click(); await flush();
 expect(root.querySelector<HTMLTextAreaElement>('[aria-label="回复正文"]')!.value).toBe(body);
 expect(root.textContent).toContain("草稿已保存，尚未发送");
 expect(root.textContent).not.toContain("发送成功");
});

it("persists a responsible employee's handling note separately from the mail classification", async () => {
 let saved: Record<string, unknown> = initial; const writes: unknown[] = [];
 await mount(async (url, request) => {
  if (request.method === "POST") { const body = await request.json(); writes.push(body); saved = { ...initial, revision: 1, status: body.status, history: [{ event_id: "event_two", kind: "handling_updated", revision: 1, status: body.status, note: body.note, actor_id: "emp_owner", created_at: stamp, draft_id: null, request_key: request.headers.get("Idempotency-Key") }] }; }
  return json(saved);
 });
 field("处理状态", "in_progress"); field("处理说明", "正在核对产品资料和客户要求"); await nextTick();
 button("保存处理记录").click(); await flush(); button("刷新处理记录").click(); await flush();
 expect(writes).toEqual([{ expected_revision: 0, status: "in_progress", note: "正在核对产品资料和客户要求" }]);
 expect(root.textContent).toContain("正在核对产品资料和客户要求"); expect(root.textContent).toContain("emp_owner");
 expect(root.textContent).toContain("待确认联系人");
});

it("keeps an unknown save tied to its original key and payload until an exact receipt is read", async () => {
 const writes: { key: string | null; body: unknown }[] = []; let receipt: Record<string, unknown> = initial;
 await mount(async (url, request) => {
  if (request.method === "POST") { const body = await request.json(); writes.push({ key: request.headers.get("Idempotency-Key"), body });
   if (writes.length === 1) throw new TypeError("lost response");
   receipt = { ...initial, revision: 1, draft: { draft_id: "draft_one", subject: body.subject, body_text: body.body_text, source_sha256: "a".repeat(64), created_by: "emp_owner", created_at: stamp }, history: [{ event_id: "event_one", kind: "draft_saved", revision: 1, status: "pending", note: "保存回复草稿", actor_id: "emp_owner", created_at: stamp, draft_id: "draft_one", request_key: writes[0].key }] };
  }
  return json(receipt);
 });
 button("使用询盘模板").click(); await nextTick(); button("保存草稿").click(); await flush();
 expect(root.textContent).toContain("保存结果待核对"); expect(button("保存草稿").disabled).toBe(true);
 button("刷新处理记录").click(); await flush();
 expect(writes).toHaveLength(1); expect(button("保存草稿").disabled).toBe(true);
 button("重试原保存请求").click(); await flush();
 expect(writes).toHaveLength(2); expect(writes[1]).toEqual(writes[0]);
 expect(root.textContent).toContain("草稿已保存，尚未发送");
});

it("clears the parent mail and followup when followup access is revoked", async () => {
 await mount(async () => json({}, 403));
 expect(root.textContent).toContain("当前身份无权访问");
 expect(root.textContent).not.toContain("customer@example.test"); expect(root.textContent).not.toContain("Product inquiry");
});

it("never applies a late followup result to another selected mail or identity", async () => {
 let resolve: (value: Response) => void = () => {};
 const logout = await mount(async url => url.pathname.includes("bmi_one") ? new Promise<Response>(done => { resolve = done; }) : json({ ...initial, message_id: "bmi_two" }));
 root.querySelectorAll<HTMLButtonElement>(".business-mail-item")[1]!.click(); await flush();
 resolve(json({ ...initial, draft: { draft_id: "late_draft", subject: "Private first reply", body_text: "Private text", source_sha256: "a".repeat(64), created_by: "emp_owner", created_at: stamp } })); await flush();
 expect(root.textContent).not.toContain("Private text"); expect(root.querySelector("h2")?.textContent).toBe("Second inquiry");
 logout(); await flush(); expect(root.textContent).not.toContain("Second inquiry");
});

it("does not offer a response draft for headers-only mail", async () => {
 await mount(async () => json(initial), { ...mail, source: { ...mail.source, source_kind: "headers_only", mime_sha256: null } });
 expect(root.textContent).toContain("正文未完整读取");
 expect(button("使用询盘模板").disabled).toBe(true); expect(button("保存草稿").disabled).toBe(true);
});

it("refreshes an untouched handling selector after another operator completes the mail", async () => {
 let current: Record<string, unknown> = initial;
 await mount(async () => json(current));
 current = { ...initial, revision: 1, status: "completed" };
 button("刷新处理记录").click(); await flush();
 expect(root.querySelector<HTMLSelectElement>('[aria-label="处理状态"]')!.value).toBe("completed");
});

it("preserves an explicitly edited handling selector across a read refresh", async () => {
 let current: Record<string, unknown> = initial;
 await mount(async () => json(current));
 field("处理状态", "waiting_customer"); await nextTick();
 current = { ...initial, revision: 1, status: "completed" };
 button("刷新处理记录").click(); await flush();
 expect(root.querySelector<HTMLSelectElement>('[aria-label="处理状态"]')!.value).toBe("waiting_customer");
});

it("admits an ordinary inquiry using server choices and retains the exact request after an uncertain result", async () => {
 const writes: unknown[] = [];
 await mount(async (url, request) => {
  if (url.pathname.endsWith("ordinary-dialogue-options")) return json({ contacts: [{account_id:"acc_one",contact_point_id:"cp_one",account_name:"Buyer"}], scopes:[{campaign_id:"cmp_one",campaign_version:3,name:"Approved scope",products:[{product_id:"prd_one",name:"Hinges"}]}] });
  if (url.pathname.endsWith("ordinary-dialogue")) {
   writes.push(await request.json());
   if (writes.length===1) throw new TypeError("synthetic uncertain result");
   return json({binding_id:"dlg_one"});
  }
  return json(initial);
 });
 button("纳入获批普通沟通").click(); await flush();
 field("询盘现有客户","cp_one"); field("询盘获批范围","cmp_one:3"); await nextTick();
 field("询盘产品","prd_one"); field("客户原话摘录","Please send details."); await nextTick();
 expect(root.textContent).not.toContain("customer_view_sha256");
 button("确认纳入普通沟通").click(); await flush();
 expect(root.textContent).toContain("准入结果待核对");
 button("核对原准入请求").click(); await flush();
 expect(writes).toEqual([{
  account_id:"acc_one",contact_point_id:"cp_one",campaign_id:"cmp_one",campaign_version:3,product_id:"prd_one",customer_quote:"Please send details.",
 },writes[0]]);
 expect(root.textContent).toContain("未建立冷邮件序列");
 expect(root.textContent).not.toContain("发送成功");
});

it("clears ordinary inquiry choices when the current employee loses access", async () => {
 const revoke = await mount(async url => url.pathname.endsWith("ordinary-dialogue-options") ? json({contacts:[{account_id:"a",contact_point_id:"c",account_name:"Private buyer"}],scopes:[]}) : json(initial));
 button("纳入获批普通沟通").click(); await flush();
 revoke(); await flush();
 expect(root.textContent).not.toContain("Private buyer");
 expect(root.querySelector('[aria-label="客户原话摘录"]')).toBeNull();
});
