import { createApp, nextTick } from "vue";
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearAuthenticatedIdentity, configureAuthenticatedIdentity, createApiClient } from "../src/api/client";
import InventoryPanel from "../src/views/customer-discovery/InventoryPanel.vue";
const cleanup: (() => void)[] = [];
afterEach(() => { cleanup.splice(0).forEach((f) => f()); clearAuthenticatedIdentity(); vi.restoreAllMocks(); sessionStorage.clear(); });
async function settle() { for (let i = 0; i < 8; i++) { await nextTick(); await new Promise((r) => setTimeout(r, 0)); } }
async function mount(failFirst = false, options: { populated?: boolean; postStatus?: number; delayedPost?: Promise<Response>; delayedGet?: Promise<Response>; history?: boolean; delayedPage?: Promise<Response>; sources?: unknown[]; knowledge?: unknown[]; delayedKnowledge?: Promise<Response> } = {}) {
  configureAuthenticatedIdentity("tenant_a", "employee_a");
  let failure: number | "network" | null = null;
  let postStatus = options.postStatus;
  const claims: Request[] = [];
  const reads: Request[] = [];
  const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
    const req = input as Request, path = new URL(req.url).pathname;
    if (req.method === "POST") {
      claims.push(req.clone());
      if (options.delayedPost && claims.length === 1) return options.delayedPost;
      if (postStatus) return new Response("{}", { status: postStatus });
      if (failFirst && claims.length === 1) throw new TypeError("network");
      return new Response(JSON.stringify({ allocations: [], job: { id: "acq_test", query: { product_tags: ["三轮车"], region: "africa" }, status: "queued", requested_count: 5, remaining_count: 5, attempts: 0 } }), { status: 202 });
    }
    reads.push(req.clone());
    if (path === "/knowledge/documents" && options.delayedKnowledge) return options.delayedKnowledge;
    if (path === "/knowledge/documents") return new Response(JSON.stringify({items: options.knowledge ?? []}), {status: 200});
    if (path === "/team/employees") return new Response("[]", { status: 200 });
    if (failure && options.delayedGet && path.endsWith("/jobs")) return options.delayedGet;
    if (failure === "network") throw new TypeError("network");
    if (failure) return new Response("{}", { status: failure });
    if (options.history && path.endsWith("/allocations")) {
      const cursor = new URL(req.url).searchParams.get("cursor");
      if (cursor && options.delayedPage) return options.delayedPage;
      const items = Array.from({ length: cursor ? 1 : 100 }, (_, i) => ({ id: `allocation_${cursor ? 100 : i}`, allocated_at: "2026-10-13T00:00:00Z", candidate: { name: `History ${cursor ? 100 : i}`, canonical_domain: "history.test", country: "KE", product_tags: [], public_emails: [], evidence_text: "History Evidence", source_url: "https://history.test", expires_at: "2027-01-01T00:00:00Z", observed_at: "2026-10-13T00:00:00Z" } }));
      return new Response(JSON.stringify(items), { status: 200 });
    }
    const data = path.endsWith("/status") ? { daily_limit: 50, used_today: 3, remaining_today: 47, reset_at: "2026-10-13T16:00:00Z", fresh_candidates: 80 } : path.endsWith("/sources") ? options.sources ?? [{ source_id: "hunter", name: "Hunter", category: "联系人", access_methods: ["官方 API"], state: { availability: options.populated ? "available" : "not_configured", valid_new_count: 0, attempts: 0 } }] : options.populated && path.endsWith("/allocations") ? [{ id: "allocation_a", allocated_at: "2026-10-13T00:00:00Z", candidate: { name: "Protected Candidate", canonical_domain: "candidate.test", country: "KE", product_tags: ["tricycle"], public_emails: ["public@candidate.test"], evidence_text: "Protected Evidence", source_url: "https://candidate.test", expires_at: "2027-01-01T00:00:00Z", observed_at: "2026-10-13T00:00:00Z" } }] : options.populated ? [{ id: "job_a", query: { product_tags: ["protected job"] }, status: "queued", remaining_count: 5 }] : [];
    return new Response(JSON.stringify(data), { status: 200 });
  });
  const root = document.createElement("div"); document.body.replaceChildren(root);
  const client = createApiClient({ baseUrl: "https://tradeos.test", fetch });
  function mountAgain(component = InventoryPanel) {
    const app = createApp(component); app.provide("tradeos-api-client", client); app.mount(root);
    let mounted = true; const unmount = () => { if (mounted) { app.unmount(); mounted = false; } };
    cleanup.push(unmount); return unmount;
  }
  const unmount = mountAgain(); await settle();
  return { root, reads, claims, client, unmount, mountAgain, failPost: (value: number) => { postStatus = value; }, fail: (value: typeof failure) => { failure = value; } };
}
function fill(root: HTMLElement) {
  const input = root.querySelector<HTMLInputElement>('[aria-label="产品关键词"]')!;
  input.value = "三轮车"; input.dispatchEvent(new Event("input"));
  const market = root.querySelector<HTMLSelectElement>('[aria-label="目标市场"]')!;
  market.value = "africa"; market.dispatchEvent(new Event("change"));
}
function submit(root: HTMLElement) { root.querySelector("form")!.dispatchEvent(new Event("submit")); }
describe("候选库存", () => {
  it("展示公司额度但不读取平台来源和全市场总数，搜索参数不带租户或员工", async () => {
    const { root, reads, claims } = await mount(); expect(root.textContent).toContain("47"); expect(root.textContent).not.toContain("公共库新鲜候选总数"); expect(reads.some(r => r.url.endsWith("/sources"))).toBe(false);
    fill(root); submit(root); await settle();
    expect(await claims[0]!.json()).toEqual({ query: { product_tags: ["三轮车"], region: "africa", country: null }, count: 5 });
    expect(claims[0]!.headers.get("Idempotency-Key")).toBeTruthy();
  });
  it("网络结果不确定后人工重试沿用同一幂等键", async () => {
    const { root, claims } = await mount(true);
    fill(root); submit(root); await settle(); expect(root.textContent).toContain("重试");
    submit(root); await settle();
    expect(claims).toHaveLength(2); expect(claims[0]!.headers.get("Idempotency-Key")).toBe(claims[1]!.headers.get("Idempotency-Key"));
  });
  it.each([403, 503, "network"] as const)("刷新 %s 后清除旧授权资料和实时来源状态", async (failure) => {
    const { root, fail } = await mount(false, { populated: true });
    expect(root.textContent).toContain("Protected Candidate");
    fail(failure); root.querySelector<HTMLButtonElement>("button")!.click(); await settle();
    expect(root.querySelector('[role="alert"]')).not.toBeNull();
    for (const text of ["Protected Candidate", "public@candidate.test", "Protected Evidence", "protected job", "可采集", "可尝试采集"]) expect(root.textContent).not.toContain(text);
    expect(root.querySelector('[aria-label="公司今日额度"]')).toBeNull();
  });
  it("POST 403 立即失效旧授权资料", async () => {
    const { root } = await mount(false, { populated: true, postStatus: 403 });
    fill(root); submit(root); await settle();
    expect(root.textContent).toContain("没有搜索权限");
    expect(root.textContent).not.toContain("Protected Candidate");
    expect(root.textContent).not.toContain("可采集");
    expect(sessionStorage.length).toBe(0);
    expect(root.querySelector<HTMLInputElement>('[aria-label="产品关键词"]')!.value).toBe("");
    expect(root.querySelector<HTMLSelectElement>('[aria-label="目标市场"]')!.value).toBe("all");
    expect(root.textContent).not.toContain("重试原搜索请求");
  });
  it("未知结果卸载重挂仍优先重试原键和原请求", async () => {
    const { root, claims, unmount, mountAgain } = await mount(true);
    fill(root); submit(root); await settle(); unmount(); mountAgain(); await settle();
    expect(root.textContent).toContain("重试原搜索请求");
    submit(root); await settle(); expect(claims).toHaveLength(2);
    expect(claims[1]!.headers.get("Idempotency-Key")).toBe(claims[0]!.headers.get("Idempotency-Key"));
    expect(await claims[1]!.json()).toEqual(await claims[0]!.json());
  });
  it("浏览器恢复使用同身份的原请求且不存候选或凭证", async () => {
    const { root, claims, unmount, mountAgain } = await mount(true, { populated: true });
    fill(root); submit(root); await settle(); expect(sessionStorage.length).toBe(1);
    const stored = sessionStorage.getItem(sessionStorage.key(0)!);
    expect(stored).not.toContain("public@candidate.test"); expect(stored).not.toContain("Protected Candidate");
    unmount(); vi.resetModules();
    const restored = (await import("../src/views/customer-discovery/InventoryPanel.vue")).default;
    mountAgain(restored); await settle(); expect(root.textContent).toContain("重试原搜索请求");
    submit(root); await settle();
    expect(claims[1]!.headers.get("Idempotency-Key")).toBe(claims[0]!.headers.get("Idempotency-Key"));
    expect(sessionStorage.length).toBe(0);
  });
  it("未挂载期间身份更换不复用他人的未决请求", async () => {
    const { root, claims, unmount, mountAgain } = await mount(true);
    fill(root); submit(root); await settle(); unmount(); configureAuthenticatedIdentity("tenant_b", "employee_b"); mountAgain(); await settle();
    expect(root.textContent).not.toContain("重试原搜索请求");
    fill(root); submit(root); await settle();
    expect(claims[1]!.headers.get("Idempotency-Key")).not.toBe(claims[0]!.headers.get("Idempotency-Key"));
  });
  it("任一 GET 403 到达时立即清除资料，不等待其他 GET", async () => {
    let resolve!: (response: Response) => void;
    const delayedGet = new Promise<Response>((r) => { resolve = r; });
    const { root, fail } = await mount(false, { populated: true, delayedGet });
    fail(403); root.querySelector<HTMLButtonElement>("button")!.click(); await settle();
    const retained = root.textContent?.includes("Protected Candidate");
    resolve(new Response("[]", { status: 200 })); await settle();
    expect(retained).toBe(false);
  });
  it("旧身份在卸载后的确定响应不能清除新身份的未决请求", async () => {
    let resolve!: (response: Response) => void;
    const delayedPost = new Promise<Response>((r) => { resolve = r; });
    const { root, claims, unmount, mountAgain } = await mount(false, { delayedPost, postStatus: 503 });
    fill(root); submit(root); await settle(); unmount();
    configureAuthenticatedIdentity("tenant_b", "employee_b"); mountAgain(); await settle();
    fill(root); submit(root); await settle();
    resolve(new Response(JSON.stringify({ allocations: [] }), { status: 202 })); await settle();
    const unmountB = cleanup.pop()!; unmountB(); mountAgain(); await settle();
    expect(root.textContent).toContain("重试原搜索请求");
    submit(root); await settle();
    expect(claims[2]!.headers.get("Idempotency-Key")).toBe(claims[1]!.headers.get("Idempotency-Key"));
  });
  it("存储不可用时导航保持原请求并明确刷新限制", async () => {
    vi.spyOn(globalThis, "sessionStorage", "get").mockImplementation(() => { throw new Error("storage unavailable"); });
    const { root, claims, unmount, mountAgain } = await mount(true);
    fill(root); submit(root); await settle();
    expect(root.textContent).toContain("刷新页面无法恢复原请求");
    unmount(); mountAgain(); await settle();
    expect(root.textContent).toContain("刷新页面无法恢复原请求");
    submit(root); await settle();
    expect(claims[1]!.headers.get("Idempotency-Key")).toBe(claims[0]!.headers.get("Idempotency-Key"));
  });

  it.each([202, 400, 403, 409, 429])("同身份旧 k1 的迟到 %s 不能清掉新 k2，前后重挂均可恢复", async (lateStatus) => {
    let resolve!: (response: Response) => void;
    const delayedPost = new Promise<Response>((r) => { resolve = r; });
    const { root, claims, unmount, mountAgain, failPost } = await mount(false, { delayedPost });
    fill(root); submit(root); await settle(); unmount();
    const unmountB = mountAgain(); await settle();
    submit(root); await settle();
    expect(claims[1]!.headers.get("Idempotency-Key")).toBe(claims[0]!.headers.get("Idempotency-Key"));
    failPost(503); submit(root); await settle();
    const newKey = claims[2]!.headers.get("Idempotency-Key");
    expect(newKey).not.toBe(claims[0]!.headers.get("Idempotency-Key"));
    unmountB(); const unmountC = mountAgain(); await settle();
    expect(root.textContent).toContain("重试原搜索请求");
    submit(root); await settle();
    expect(claims[3]!.headers.get("Idempotency-Key")).toBe(newKey);
    const storedBefore = sessionStorage.getItem(sessionStorage.key(0)!);
    expect(storedBefore).not.toBeNull();
    unmountC();
    resolve(new Response(JSON.stringify({ allocations: [] }), { status: lateStatus })); await settle();
    expect(sessionStorage.getItem(sessionStorage.key(0)!)).toBe(storedBefore);
    mountAgain(); await settle();
    expect(root.textContent).toContain("重试原搜索请求");
    submit(root); await settle();
    expect(claims[4]!.headers.get("Idempotency-Key")).toBe(newKey);
    expect(await claims[4]!.json()).toEqual(await claims[2]!.json());
  });
  it("组件卸载后原 k1 确定成功且仍为当前请求时清理恢复记录", async () => {
    let resolve!: (response: Response) => void;
    const delayedPost = new Promise<Response>((r) => { resolve = r; });
    const { root, unmount, mountAgain } = await mount(false, { delayedPost });
    fill(root); submit(root); await settle(); unmount();
    expect(sessionStorage.length).toBe(1);
    resolve(new Response(JSON.stringify({ allocations: [] }), { status: 202 })); await settle();
    expect(sessionStorage.length).toBe(0);
    mountAgain(); await settle();
    expect(root.textContent).not.toContain("重试原搜索请求");
  });

});

function more(root: HTMLElement) { return Array.from(root.querySelectorAll("button")).find((b) => b.textContent?.includes("加载更多")); }
it("历史加载更多追加一页，重取首页替换而非重复追加", async () => {
  const { root } = await mount(false, { history: true });
  expect(root.querySelectorAll(".inventory-candidate")).toHaveLength(100);
  expect(more(root)).toBeTruthy(); more(root)!.click(); await settle();
  expect(root.querySelectorAll(".inventory-candidate")).toHaveLength(101);
  expect(root.textContent).toContain("History 100"); expect(more(root)).toBeUndefined();
  root.querySelector<HTMLButtonElement>("button")!.click(); await settle();
  expect(root.querySelectorAll(".inventory-candidate")).toHaveLength(100);
  expect(root.textContent).not.toContain("History 100");
});
it.each([403, 503, "network"] as const)("分页失败 %s 清除所有保护资料", async (failure) => {
  const { root, fail } = await mount(false, { history: true });
  expect(more(root)).toBeTruthy(); fail(failure); more(root)!.click(); await settle();
  expect(root.querySelectorAll(".inventory-candidate")).toHaveLength(0);
  expect(root.querySelector('[aria-label="公司今日额度"]')).toBeNull();
  expect(root.querySelector('[role="alert"]')).not.toBeNull();
});
it("分页旧响应不越身份边界", async () => {
  let resolve!: (response: Response) => void;
  const delayedPage = new Promise<Response>((r) => { resolve = r; });
  const { root } = await mount(false, { history: true, delayedPage });
  expect(more(root)).toBeTruthy(); more(root)!.click(); await settle();
  configureAuthenticatedIdentity("tenant_b", "employee_b"); await settle();
  resolve(new Response(JSON.stringify([{ id: "old", candidate: { name: "Old Identity" } }]), { status: 200 })); await settle();
  expect(root.textContent).not.toContain("Old Identity");
  expect(root.querySelectorAll(".inventory-candidate")).toHaveLength(0);
});
it("刷新首页后忽略迟到的历史分页响应", async () => {
  let resolve!: (response: Response) => void;
  const delayedPage = new Promise<Response>((r) => { resolve = r; });
  const { root } = await mount(false, { history: true, delayedPage });
  more(root)!.click(); await settle();
  root.querySelector<HTMLButtonElement>("button")!.click(); await settle();
  resolve(new Response(JSON.stringify([{ id: "stale", candidate: { name: "Stale Page" } }]), { status: 200 })); await settle();
  expect(root.querySelectorAll(".inventory-candidate")).toHaveLength(100);
  expect(root.textContent).not.toContain("Stale Page");
});

it.each([403, 503, "network"] as const)("未知搜索后分页失败 %s：403清除恢复和表单，普通故障保留原请求", async (failure) => {
  const { root, fail } = await mount(true, { history: true });
  fill(root);
  const countryInput = root.querySelector<HTMLInputElement>('[aria-label="国家代码"]')!;
  countryInput.value = "KE"; countryInput.dispatchEvent(new Event("input"));
  const countInput = root.querySelector<HTMLInputElement>('[aria-label="搜索数量"]')!;
  countInput.value = "7"; countInput.dispatchEvent(new Event("input"));
  submit(root); await settle();
  expect(sessionStorage.length).toBe(1);
  const original = sessionStorage.getItem(sessionStorage.key(0)!);
  fail(failure); more(root)!.click(); await settle();
  expect(root.querySelectorAll(".inventory-candidate")).toHaveLength(0);
  expect(root.querySelector('[aria-label="公司今日额度"]')).toBeNull();
  const keywordInput = root.querySelector<HTMLInputElement>('[aria-label="产品关键词"]')!;
  const marketInput = root.querySelector<HTMLSelectElement>('[aria-label="目标市场"]')!;
  if (failure === 403) {
    expect(sessionStorage.length).toBe(0);
    expect(keywordInput.value).toBe(""); expect(marketInput.value).toBe("all");
    expect(countryInput.value).toBe(""); expect(countInput.value).toBe("5");
    expect(root.textContent).not.toContain("重试原搜索请求");
  } else {
    expect(sessionStorage.getItem(sessionStorage.key(0)!)).toBe(original);
    expect(keywordInput.value).toBe("三轮车"); expect(marketInput.value).toBe("africa");
    expect(countryInput.value).toBe("KE"); expect(countInput.value).toBe("7");
    expect(root.textContent).toContain("重试原搜索请求");
  }
});


it("提交已确认企业资料、自然要求和多国家，换账号清空引用", async () => {
  const { root, claims } = await mount(false, {knowledge: [{document_id: "doc1", tenant_id:"tenant_a", status:"confirmed", current_revision_id:"rev1", source:{filename:"产品目录.md"}}]});
  fill(root);
  const intent = root.querySelector<HTMLTextAreaElement>('[aria-label="搜索要求"]')!;
  expect(intent).not.toBeNull();
  intent.value = "寻找经销商"; intent.dispatchEvent(new Event("input"));
  const countries = root.querySelector<HTMLInputElement>('[aria-label="国家代码"]')!;
  countries.value = "KE,NG"; countries.dispatchEvent(new Event("input"));
  submit(root); await settle();
  const body = await claims[0]!.json();
  expect(body.query.confirmed_refs).toEqual([{document_id:"doc1", revision_id:"rev1"}]);
  expect(body.query.countries).toEqual(["KE","NG"]);
  expect(body.query.intent).toBe("寻找经销商");
  configureAuthenticatedIdentity("tenant_b", "employee_b"); await settle();
  expect(root.textContent).not.toContain("产品目录.md");
  expect(intent.value).toBe("");
});


it("资料请求未返回时阻止关键词回退，换账号拒绝迟到企业资料", async () => {
  let resolve!: (value: Response) => void;
  const delayedKnowledge = new Promise<Response>(r => {resolve = r;});
  const {root, claims} = await mount(false, {delayedKnowledge});
  fill(root); submit(root); await settle(); expect(claims).toHaveLength(0);
  configureAuthenticatedIdentity("tenant_b", "employee_b");
  resolve(new Response(JSON.stringify({items:[{document_id:"private_doc", tenant_id:"tenant_a",status:"confirmed",current_revision_id:"rev1",source:{filename:"原企业私有资料.md"}}]}),{status:200}));
  await settle();
  expect(root.textContent).not.toContain("原企业私有资料.md");
  expect(root.querySelector<HTMLSelectElement>('[aria-label="已确认企业资料"]')!.options.length).toBe(0);
});

it("原搜索恢复严格保留资料引用和私有要求，不保存资料原文", async () => {
  const {root, claims, unmount, mountAgain} = await mount(true, {knowledge:[{document_id:"doc1",tenant_id:"tenant_a",status:"confirmed",current_revision_id:"rev1",source:{filename:"产品目录.md"}}]});
  fill(root);
  const intent = root.querySelector<HTMLTextAreaElement>('[aria-label="搜索要求"]')!;
  intent.value = "寻找经销商"; intent.dispatchEvent(new Event("input"));
  submit(root); await settle();
  const original = await claims[0]!.json();
  unmount(); vi.resetModules();
  mountAgain((await import("../src/views/customer-discovery/InventoryPanel.vue")).default); await settle();
  submit(root); await settle();
  expect(await claims[1]!.json()).toEqual(original);
  expect(claims[1]!.headers.get("Idempotency-Key")).toBe(claims[0]!.headers.get("Idempotency-Key"));
});
