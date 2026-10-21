import { createApp, nextTick, type App } from "vue";
import { afterEach, expect, it, vi } from "vitest";
import InventoryPanel from "../src/views/customer-discovery/InventoryPanel.vue";
import { createApiClient, type WebIdentityProvider } from "../src/api/client";
let app: App | undefined;
afterEach(() => {
  app?.unmount();
  vi.restoreAllMocks();
  document.body.replaceChildren();
  sessionStorage.clear();
});
async function settle() {
  for (let i = 0; i < 8; i++) {
    await nextTick();
    await new Promise((r) => setTimeout(r, 0));
  }
}
async function mount(size = 2, options: { post?: (request: Request) => Promise<Response>; read?: (request: Request) => Promise<Response> | undefined } = {}) {
  let assigned = false,
    failed = false,
    generation = 0;
  const listeners = new Set<() => void>(),
    reads: Request[] = [],
    writes: Request[] = [];
  const provider: WebIdentityProvider = {
    generation: () => generation,
    current: () => ({
      employeeId: "boss_a",
      tenantId: "tn_a",
      mode: "fixed-dev",
    }),
    subscribe: (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
  };
  const rows = Array.from({ length: size }, (_, i) => ({
    id: `alloc_${i}`,
    employee_id: null,
    email_verification: "unverified",
    account_id: null,
    allocated_at: "2026-10-14T00:00:00Z",
    candidate: {
      name: `Company ${i}`,
      country: "KE",
      canonical_domain: "company.test",
      product_tags: [],
      public_emails: ["public@company.test"],
      evidence_text: "Public evidence",
      source_url: "https://company.test",
      observed_at: "2026-10-14T00:00:00Z",
      expires_at: "2027-01-01T00:00:00Z",
    },
  }));
  const client = createApiClient(
    {
      baseUrl: "https://tradeos.test",
      fetch: async (input) => {
        const r = input as Request,
          p = new URL(r.url).pathname;
        if (r.method === "POST") {
          writes.push(r.clone());
          if (options.post) return options.post(r);
          assigned = true;
          return new Response(JSON.stringify(rows), { status: 200 });
        }
        reads.push(r.clone());
        const result = options.read?.(r); if (result) return result;
        if (failed) return new Response("{}", { status: 403 });
        if (p === "/knowledge/documents") return new Response(JSON.stringify({items: []}), {status: 200});
        return new Response(
          JSON.stringify(
            p.endsWith("/status")
              ? {
                  daily_limit: 50,
                  used_today: 2,
                  remaining_today: 48,
                  reset_at: "2026-10-15T00:00:00Z",
                }
              : p === "/team/employees"
                ? [
                    {
                      tenant_id: "tn_a",
                      employee_id: "sales_a",
                      name: "销售甲",
                      role: "sales",
                      is_active: true,
                    },
                    {
                      tenant_id: "tn_a",
                      employee_id: "inactive",
                      name: "已停用",
                      role: "sales",
                      is_active: false,
                    },
                    {
                      tenant_id: "tn_b",
                      employee_id: "foreign",
                      name: "其他公司",
                      role: "sales",
                      is_active: true,
                    },
                  ]
                : p.endsWith("/allocations")
                  ? assigned
                    ? []
                    : rows
                  : [],
          ),
          { status: 200 },
        );
      },
    },
    provider,
  );
  const host = document.createElement("div");
  document.body.append(host);
  app = createApp(InventoryPanel);
  app.provide("tradeos-api-client", client);
  app.mount(host);
  await settle();
  return {
    host,
    reads,
    writes,
    fail: () => {
      failed = true;
    },
    switchIdentity: () => {
      generation++;
      for (const l of listeners) l();
    },
  };
}
function button(host: HTMLElement, text: string) {
  return [...host.querySelectorAll<HTMLButtonElement>("button")].find((b) =>
    b.textContent?.includes(text),
  )!;
}
it("待分配使用服务端筛选，批量分配给本公司在职员工并清选择刷新", async () => {
  const { host, reads, writes } = await mount();
  expect(
    reads
      .filter((r) => r.url.includes("/allocations"))
      .every(
        (r) => new URL(r.url).searchParams.get("unassigned_only") === "true",
      ),
  ).toBe(true);
  const select = host.querySelector<HTMLSelectElement>(
    '[aria-label="分配给员工"]',
  )!;
  expect([...select.options].map((o) => o.text.trim())).toEqual([
    "选择负责人",
    "销售甲",
  ]);
  select.value = "sales_a";
  select.dispatchEvent(new Event("change"));
  host.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click();
  await nextTick();
  button(host, "分配所选").click();
  await settle();
  expect(await writes[0]!.json()).toEqual({
    allocation_ids: ["alloc_0", "alloc_1"],
    employee_id: "sales_a",
  });
  expect(host.querySelectorAll(".inventory-candidate")).toHaveLength(0);
  expect(host.textContent).toContain("已选择 0");
});
it("只全选当前加载范围，一次最多100条", async () => {
  const { host, writes } = await mount(101);
  host.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click();
  await nextTick();
  expect(host.textContent).toContain("已选择 100");
  expect(host.textContent).toContain("当前已加载");
  const select = host.querySelector<HTMLSelectElement>(
    '[aria-label="分配给员工"]',
  )!;
  select.value = "sales_a";
  select.dispatchEvent(new Event("change"));
  await nextTick();
  button(host, "分配所选").click();
  await settle();
  expect((await writes[0]!.json()).allocation_ids).toHaveLength(100);
});
it.each(["switch", "403"])("%s清除分配选择及目标员工", async (kind) => {
  const { host, fail, switchIdentity } = await mount();
  host.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click();
  await nextTick();
  if (kind === "switch") switchIdentity();
  else {
    fail();
    button(host, "刷新").click();
  }
  await settle();
  expect(host.querySelectorAll(".inventory-candidate")).toHaveLength(0);
  expect(
    host.querySelector<HTMLSelectElement>('[aria-label="分配给员工"]')?.value ??
      "",
  ).toBe("");
});
it("GET403清除尚未提交的分配选择和待操作", async () => {
  const { host, reads, writes, fail } = await mount();
  const select = host.querySelector<HTMLSelectElement>(
    '[aria-label="分配给员工"]',
  )!;
  select.value = "sales_a";
  select.dispatchEvent(new Event("change"));
  host.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click();
  await nextTick();
  fail();
  button(host, "刷新").click();
  await settle();
  expect(reads.length).toBeGreaterThan(0);
  expect(writes).toHaveLength(0);
  expect(button(host, "核对原分配")).toBeUndefined();
});

it.each(["immediate", "after_network_failure"])("GET403 (%s)之后迟到分配成功不回填，也不触发额外刷新", async (sequence) => {
  let finish!: (response: Response) => void;
  let denyRead = false;
  let denyLater!: (response: Response) => void;
  const delayedPost = new Promise<Response>(resolve => { finish = resolve; });
  const delayed403 = new Promise<Response>(resolve => { denyLater = resolve; });
  const { host, reads, writes } = await mount(100, { post: async () => delayedPost, read: request => {
    if (!denyRead) return;
    if (sequence === "immediate") return Promise.resolve(new Response("{}", { status: 403 }));
    const path = new URL(request.url).pathname;
    if (path.endsWith("/status")) return Promise.reject(new TypeError("network"));
    if (path.endsWith("/jobs")) return delayed403;
  }});
  const target = host.querySelector<HTMLSelectElement>('[aria-label="分配给员工"]')!;
  target.value = "sales_a"; target.dispatchEvent(new Event("change"));
  host.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click(); await nextTick();
  button(host, "分配所选").click(); await settle(); expect(writes).toHaveLength(1);
  denyRead = true; button(host, "刷新列表").click(); await settle();
  if (sequence === "after_network_failure") { denyLater(new Response("{}", { status: 403 })); await settle(); }
  expect(host.querySelectorAll(".inventory-candidate")).toHaveLength(0);
  expect(host.querySelector('[role="alert"]')?.textContent).toContain("无权");
  expect(target.value).toBe("");
  const deniedReads = reads.length;
  denyRead = false; finish(new Response("[]", { status: 200 })); await settle();
  expect(reads).toHaveLength(deniedReads);
  expect(host.querySelectorAll(".inventory-candidate")).toHaveLength(0);
  expect(host.textContent).not.toContain("已分配 100");
});
it("分配POST403也清除此前未知搜索的恢复键和搜索表单", async () => {
  const { host, writes } = await mount(100, { post: async request => new Response("{}", { status: request.url.endsWith("/requests") ? 503 : 403 }) });
  const input = host.querySelector<HTMLInputElement>('[aria-label="产品关键词"]')!;
  input.value = "hardware"; input.dispatchEvent(new Event("input"));
  host.querySelector("form")!.dispatchEvent(new Event("submit")); await settle();
  expect(sessionStorage.length).toBe(1);
  const target = host.querySelector<HTMLSelectElement>('[aria-label="分配给员工"]')!;
  target.value = "sales_a"; target.dispatchEvent(new Event("change"));
  host.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click(); await nextTick();
  button(host, "分配所选").click(); await settle();
  expect(writes).toHaveLength(2);
  expect(sessionStorage.length).toBe(0);
  expect(input.value).toBe(""); expect(target.value).toBe("");
  expect(host.textContent).not.toContain("重试原搜索请求");
});


it("进行中轮询只读任务与额度，保留分页选择；显式刷新清除失效归属与员工", async () => {
  const interval = vi.spyOn(globalThis, "setInterval");
  let invalid = false;
  const rows = Array.from({ length: 102 }, (_, i) => ({ id: `alloc_${i}`, employee_id: null, account_id: null, email_verification: "unverified", allocated_at: "2026-10-14T00:00:00Z", candidate: { name: `Company ${i}`, country: "KE", canonical_domain: "company.test", product_tags: [], public_emails: [], evidence_text: "Public evidence", source_url: "https://company.test", observed_at: "2026-10-14T00:00:00Z", expires_at: "2027-01-01T00:00:00Z" } }));
  const { host, reads, writes } = await mount(100, { read: request => {
    const url = new URL(request.url);
    if (url.pathname.endsWith("/jobs")) return Promise.resolve(new Response(JSON.stringify([{ id: "job_current", query: { product_tags: ["hardware"] }, status: "processing", created_at: "2026-10-14T00:00:00Z" }]), { status: 200 }));
    if (url.pathname.endsWith("/allocations")) {
      const start = url.searchParams.has("cursor") ? 100 : 0;
      const result = rows.slice(start, start + 100).filter(row => !invalid || row.id !== "alloc_101");
      return Promise.resolve(new Response(JSON.stringify(result), { status: 200 }));
    }
    if (invalid && url.pathname === "/team/employees") return Promise.resolve(new Response("[]", { status: 200 }));
  }});
  button(host, "加载更多").click(); await settle();
  const target = host.querySelector<HTMLSelectElement>('[aria-label="分配给员工"]')!;
  target.value = "sales_a"; target.dispatchEvent(new Event("change"));
  const row = [...host.querySelectorAll<HTMLElement>(".inventory-candidate")].find(row => row.textContent?.includes("Company 101"))!;
  row.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click(); await nextTick();
  const poll = interval.mock.calls[0][0] as () => void;
  const allocationReads = reads.filter(request => request.url.includes("/allocations")).length;
  const employeeReads = reads.filter(request => request.url.includes("/employees")).length;
  poll(); await settle();
  expect(reads.filter(request => request.url.includes("/allocations"))).toHaveLength(allocationReads);
  expect(reads.filter(request => request.url.includes("/employees"))).toHaveLength(employeeReads);
  expect(host.textContent).toContain("已选择 1"); expect(target.value).toBe("sales_a");
  expect([...host.querySelectorAll<HTMLElement>(".inventory-candidate")].find(row => row.textContent?.includes("Company 101"))?.querySelector<HTMLInputElement>('input[type="checkbox"]')?.checked).toBe(true);
  expect(reads.some(request => new URL(request.url).searchParams.get("cursor") === "alloc_99")).toBe(true);
  invalid = true; poll(); await settle();
  expect(host.textContent).toContain("已选择 1");
  button(host, "刷新列表").click(); await settle();
  expect(host.textContent).toContain("已选择 0"); expect(target.value).toBe("");
  expect(host.textContent).not.toContain("Company 101"); expect(button(host, "分配所选").disabled).toBe(true); expect(writes).toHaveLength(0);
});

it("进行中轮询观察403仍清除选择与目标员工", async () => {
  const interval = vi.spyOn(globalThis, "setInterval");
  let denied = false;
  const { host } = await mount(2, { read: request => {
    if (denied) return Promise.resolve(new Response("{}", { status: 403 }));
    if (request.url.endsWith("/jobs")) return Promise.resolve(new Response(JSON.stringify([{ id: "job_current", query: { product_tags: ["hardware"] }, status: "processing" }]), { status: 200 }));
  }});
  const target = host.querySelector<HTMLSelectElement>('[aria-label="分配给员工"]')!;
  target.value = "sales_a"; target.dispatchEvent(new Event("change")); host.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click(); await nextTick();
  denied = true; (interval.mock.calls[0][0] as () => void)(); await settle();
  expect(host.querySelectorAll(".inventory-candidate")).toHaveLength(0); expect(target.value).toBe("");
});
