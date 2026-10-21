import { createApp, nextTick, type App } from "vue";
import { createMemoryHistory, createRouter } from "vue-router";
import { afterEach, expect, it, vi } from "vitest";
import { createApiClient, type WebIdentityProvider } from "../src/api/client";
import type { components } from "../src/api/api";
import Customers from "../src/views/customers/MyCustomers.vue";
import CustomerDetail from "../src/views/customers/CustomerDetail.vue";
type Allocation = components["schemas"]["CustomerAllocationView"];
type ExecutionMode = components["schemas"]["CustomerFollowupOptions"]["execution_mode"];
const time = "2026-10-14T00:00:00Z";
function allocation(id: string): Allocation {
  return {
    id,
    account_id: `acc_${id}`,
    employee_id: "emp_a",
    email_verification: "unverified",
    assigned_at: time,
    assigned_by: "boss",
    allocated_at: time,
    candidate: {
      id: `can_${id}`,
      name: `Company ${id}`,
      canonical_domain: "company.test",
      country: "KE",
      product_tags: ["hardware"],
      public_emails: ["public@company.test"],
      source_id: "public",
      source_url: "https://company.test/evidence",
      evidence_text: "Public source",
      observed_at: time,
      expires_at: time,
      provenance: {
        source_type: "web_page",
        source_id: "public",
        extracted_by: "human",
        extracted_at: time,
        confirmed_by: null,
        confirmed_at: null,
        page_hash: null, source_quote: null, source_url: null,
      },
    },
  };
}
function options(id: string, executionMode: ExecutionMode = "automatic"): components["schemas"]["CustomerFollowupOptions"] {
  return {
    allocation_id: id,
    execution_mode: executionMode,
    account_id: `acc_${id}`,
    blocking_reasons: [],
    contacts: [
      {
        contact: {
          account_id: `acc_${id}`,
          contact_id: `con_${id}`,
          tenant_id: "tn_a",
          created_at: time,
        },
        contact_points: [
          {
            contact_point: {
              account_id: `acc_${id}`,
              contact_id: `con_${id}`,
              contact_point_id: `cp_${id}`,
              tenant_id: "tn_a",
              created_at: time,
              kind: "email",
              value: `verified_${id}@company.test`,
              verification: "verified",
              verified_at: time,
            },
            legal_basis: "legitimate_interest",
            contact_type: "role_based",
            subject_type: "legal_entity",
            legal_basis_source: "lia",
            collected_at: time,
          },
        ],
      },
    ],
    hypotheses: [
      {
        hypothesis_id: `hyp_${id}`,
        category: "hardware",
        status: "inferred",
        is_inference: true,
      },
    ],
    campaigns: [
      {
        campaign_id: `cam_${id}`,
        campaign_version: 3,
        name: `Task ${id}`,
        markets: ["KE"],
        target_entity_types: ["distributor"],
        allowed_categories: ["hardware"],
        eligible_contact_point_ids: [`cp_${id}`],
        can_start: true,
        blocking_reasons: [],
      },
    ],
  };
}
function enrollment(
  id: string,
  state = "enrolled",
  executionMode: ExecutionMode = "automatic",
): components["schemas"]["CustomerEnrollmentView"] {
  return {
    enrollment_id: `enr_${id}`,
    execution_mode: executionMode,
    campaign_id: `cam_${id}`,
    campaign_version: 3,
    account_id: `acc_${id}`,
    contact_point_id: `cp_${id}`,
    source_hypothesis_id: `hyp_${id}`,
    state,
    current_step: 0,
    next_send_at: null,
    enrolled_at: time,
    stopped_at: null,
    stop_reason: null,
  };
}
const apps: App[] = [];

it("管理员可将已有客户显式转交，提交原归属版本且不冒用目标身份", async () => {
  const { host, writes } = await mount("/customers/a", {
    role: "boss",
    read: (r) => new URL(r.url).pathname === "/team/employees" ? Promise.resolve(json([
      { employee_id: "emp_a", tenant_id: "tn_a", name: "管理员", role: "boss", is_active: true },
      { employee_id: "emp_b", tenant_id: "tn_a", name: "齐凯", role: "sales", is_active: true },
      { employee_id: "emp_foreign", tenant_id: "tn_b", name: "其他公司", role: "sales", is_active: true },
    ])) : undefined,
    post: async () => json({ ...allocation("a"), employee_id: "emp_b" }),
  });
  const select = host.querySelector<HTMLSelectElement>('select[aria-label="新负责人"]');
  expect(select).not.toBeNull();
  expect(select!.textContent).toContain("齐凯");
  expect(select!.textContent).not.toContain("其他公司");
  select!.value = "emp_b";
  select!.dispatchEvent(new Event("change"));
  const reason = host.querySelector<HTMLTextAreaElement>('textarea[aria-label="转交原因"]')!;
  reason.value = "管理员安排齐凯联系客户";
  reason.dispatchEvent(new Event("input"));
  await settle();
  const submit = [...host.querySelectorAll("button")].find(b => b.textContent?.trim() === "确认转交客户")!;
  submit.click();
  await settle();
  expect(writes).toHaveLength(1);
  expect(new URL(writes[0].url).pathname).toBe("/prospects/inventory/allocations/a/transfer");
  expect(await writes[0].json()).toEqual({
    employee_id: "emp_b", expected_employee_id: "emp_a", expected_assigned_at: time,
    reason: "管理员安排齐凯联系客户",
  });
});
afterEach(() => {
  apps.splice(0).forEach((a) => a.unmount());
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  document.body.replaceChildren();
  sessionStorage.clear();
});
async function settle() {
  for (let i = 0; i < 10; i++) {
    await nextTick();
    await new Promise((r) => setTimeout(r, 0));
  }
}
function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
async function mount(
  path = "/customers",
  config: {
    role?: string;
    pending?: boolean;
    blocked?: boolean;
    stop?: boolean;
    post?: (request: Request) => Promise<Response>;
    read?: (request: Request) => Promise<Response> | undefined;
  } = {},
) {
  let generation = 0,
    employeeId = "emp_a",
    fail = false;
  const listeners = new Set<() => void>(),
    reads: Request[] = [],
    writes: Request[] = [];
  const provider: WebIdentityProvider = {
    generation: () => generation,
    current: () => ({ employeeId, tenantId: "tn_a", mode: "fixed-dev" }),
    subscribe: (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
  };
  const client = createApiClient(
    {
      baseUrl: "https://tradeos.test",
      fetch: async (input) => {
        const r = input as Request,
          p = new URL(r.url).pathname;
        if (r.method === "POST") {
          writes.push(r.clone());
          if (config.post) return config.post(r);
          const body = await r.json();
          return json(
            enrollment(
              "a",
              p.endsWith("/stop") ? "stopped_manual" : "enrolled",
              body.execution_mode ?? "automatic",
            ),
          );
        }
        reads.push(r.clone());
        if (fail) return json({}, 403);
        const custom = config.read?.(r);
        if (custom) return custom;
        if (p.endsWith("/access"))
          return json({
            role: config.role ?? "sales",
            can_search: config.role === "boss",
            can_assign: config.role === "boss",
          });
        if (p === "/prospects/customers")
          return json([allocation("a"), allocation("b")]);
        const id = p.split("/")[3];
        if (p.endsWith("/follow-up")) {
          const executionMode = new URL(r.url).searchParams.get("execution_mode") === "manual" ? "manual" : "automatic";
          const o = options(id, executionMode);
          if (config.blocked) {
            o.blocking_reasons = [
              ...(executionMode === "automatic" ? ["automatic_followup_disabled"] : []),
              "contact_unverified",
              "sending_identity_status_unavailable",
            ];
            o.campaigns[0].can_start = false;
            o.contacts = [];
          }
          return json(o);
        }
        const a = allocation(id);
        if (config.pending) a.account_id = null;
        return json({
          allocation: a,
          owner: "emp_a",
          account: null,
          contacts: options(id).contacts,
          enrollments: config.stop
            ? [enrollment(id), enrollment("done", "completed")]
            : [],
          can_stop: config.stop ?? false,
          blocking_reasons: config.pending ? ["account_pending"] : [],
        });
      },
    },
    provider,
  );
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/inbox", component: { template: "<div />" } },
      { path: "/crm/handoffs", component: { template: "<div />" } },
      { path: "/crm/outreach", component: { template: "<div />" } },
      { path: "/customers", name: "customers", component: Customers },
      {
        path: "/customers/:allocationId",
        name: "customer-detail",
        component: CustomerDetail,
      },
    ],
  });
  await router.replace(path);
  const host = document.createElement("div");
  document.body.append(host);
  function mountAgain() {
    const app = createApp(path === "/customers" ? Customers : CustomerDetail);
    apps.push(app);
    app.provide("tradeos-api-client", client);
    app.use(router).mount(host);
    return app;
  }
  const app = mountAgain();
  await settle();
  return {
    host,
    router,
    reads,
    writes,
    app,
    mountAgain,
    fail: () => {
      fail = true;
    },
    switchIdentity: () => {
      employeeId = "emp_b";
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
async function choose(host: HTMLElement) {
  for (const field of host.querySelectorAll<HTMLSelectElement>(
    "select[data-followup-field]",
  )) {
    field.value = field.options[1]?.value ?? "";
    field.dispatchEvent(new Event("change"));
    await nextTick();
  }
}
it("本人列表通过allocation详情路由，不请求公司池", async () => {
  const { host, reads } = await mount();
  expect(host.textContent).toContain("Company a");
  expect(host.querySelector('a[href="/customers/a"]')).not.toBeNull();
  expect(reads.some((r) => r.url.includes("/inventory/"))).toBe(false);
});
it("阻塞具体解释未就绪，公开邮箱不成为跟进选项", async () => {
  const { host, writes } = await mount("/customers/a", { blocked: true });
  const panel = host.querySelector<HTMLElement>('[aria-label="客户单封手动联系"]')!;
  expect(panel.textContent).not.toContain("自动跟进尚未启用");
  expect(panel.textContent).toContain("联系方式待验证");
  expect(panel.textContent).toContain("发件邮箱状态暂时无法确认");
  expect(panel.querySelector('option[value="public@company.test"]')).toBeNull();
  expect(button(panel, "准备单封手动联系").disabled).toBe(true);
  expect(writes).toHaveLength(0);
});
it("详情使用精确任务版本、已验证联系人和推断来源准备单封手动联系", async () => {
  const { host, reads, writes } = await mount("/customers/a");
  const optionRequests = reads.filter(request => new URL(request.url).pathname.endsWith("/follow-up"));
  expect(optionRequests).toHaveLength(1);
  expect(new URL(optionRequests[0].url).searchParams.get("execution_mode")).toBe("manual");
  await choose(host);
  expect(host.textContent).toContain("推断");
  button(host, "准备单封手动联系").click();
  await settle();
  expect(await writes[0].json()).toEqual({
    campaign_id: "cam_a",
    campaign_version: 3,
    execution_mode: "manual",
    contact_point_id: "cp_a",
    source_hypothesis_id: "hyp_a",
  });
  expect(writes[0].headers.get("Idempotency-Key")).toBeTruthy();
  const panel = host.querySelector('[aria-label="客户单封手动联系"]')!;
  expect(panel.textContent).toContain("尚未发信");
  expect(panel.querySelector('a[href="/crm/outreach?enrollment_id=enr_a"]')).not.toBeNull();
  expect(writes).toHaveLength(1);
});
it("网络未知后刷新及重挂不偷偷重发，显式恢复原key和body", async () => {
  let attempts = 0;
  const { host, writes, app, mountAgain } = await mount("/customers/a", {
    post: async () => {
      attempts++;
      if (attempts === 1) throw new Error("network");
      return json(enrollment("a", "enrolled", "manual"));
    },
  });
  await choose(host);
  button(host, "准备单封手动联系").click();
  await settle();
  expect(host.textContent).toContain("结果未知");
  button(host, "刷新").click();
  await settle();
  expect(writes).toHaveLength(1);
  app.unmount();
  mountAgain();
  await settle();
  expect(writes).toHaveLength(1);
  button(host, "核对原跟进请求").click();
  await settle();
  expect(writes).toHaveLength(2);
  expect(writes[1].headers.get("Idempotency-Key")).toBe(
    writes[0].headers.get("Idempotency-Key"),
  );
  expect(await writes[1].json()).toEqual(await writes[0].clone().json());
});
it("逐客户批量跟进呈现每条成功和未知，不宣称原子", async () => {
  const { host, writes } = await mount("/customers", {
    post: async (r) =>
      r.url.includes("/a/") ? json(enrollment("a")) : json({}, 503),
  });
  host.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click();
  await settle();
  await choose(host);
  button(host, "逐项启动").click();
  await settle();
  expect(writes).toHaveLength(2);
  expect(await writes[0].json()).toMatchObject({
    campaign_id: "cam_a",
    execution_mode: "automatic",
    contact_point_id: "cp_a",
    source_hypothesis_id: "hyp_a",
  });
  expect(await writes[1].json()).toMatchObject({
    campaign_id: "cam_b",
    execution_mode: "automatic",
    contact_point_id: "cp_b",
    source_hypothesis_id: "hyp_b",
  });
  expect(host.textContent).toContain("已加入跟进");
  expect(host.textContent).toContain("结果未知");
});
it.each(["switch", "403"])("%s清除本人资料、选择及跟进表单", async (kind) => {
  const { host, fail, switchIdentity } = await mount();
  host.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click();
  await settle();
  await choose(host);
  if (kind === "switch") {
    fail();
    switchIdentity();
  } else {
    fail();
    button(host, "刷新").click();
  }
  await settle();
  expect(host.textContent).not.toContain("Company a");
  expect(host.querySelectorAll("select[data-followup-field]")).toHaveLength(0);
  expect(host.querySelectorAll('input[type="checkbox"]:checked')).toHaveLength(
    0,
  );
});
it("停止只显示当前owner的活跃序列，使用精确客户和Enrollment", async () => {
  const { host, writes } = await mount("/customers/a", { stop: true });
  expect(
    [...host.querySelectorAll("button")].filter((b) =>
      b.textContent?.includes("停止跟进"),
    ),
  ).toHaveLength(1);
  button(host, "停止跟进").click();
  await settle();
  expect(new URL(writes[0].url).pathname).toBe(
    "/prospects/customers/a/follow-up/enr_a/stop",
  );
});
it("历史缺档案boss显式同原owner关联，不改负责人", async () => {
  const { host, writes } = await mount("/customers/a", {
    role: "boss",
    pending: true,
  });
  expect(writes).toHaveLength(0);
  button(host, "关联客户档案").click();
  await settle();
  expect(new URL(writes[0].url).pathname).toBe(
    "/prospects/inventory/assignments",
  );
  expect(await writes[0].json()).toEqual({
    allocation_ids: ["a"],
    employee_id: "emp_a",
  });
});
it("员工历史缺档案只解释管理员关联，不可替自己分配", async () => {
  const { host } = await mount("/customers/a", { pending: true });
  expect(host.textContent).toContain("档案待关联");
  expect(button(host, "关联客户档案")).toBeUndefined();
});
it("options 的403清除客户详情和所有未决跟进表单", async () => {
  let deny = false;
  const { host, writes, app, mountAgain } = await mount("/customers/a", {
    post: async () => json({}, 503),
    read: (r) =>
      deny && new URL(r.url).pathname.endsWith("/follow-up")
        ? Promise.resolve(json({}, 403))
        : undefined,
  });
  await choose(host);
  button(host, "准备单封手动联系").click();
  await settle();
  expect(writes).toHaveLength(1);
  deny = true;
  button(host, "刷新").click();
  await settle();
  expect(host.textContent).not.toContain("Company a");
  expect(host.querySelectorAll("select[data-followup-field]")).toHaveLength(0);
  expect(sessionStorage.length).toBe(0);
  app.unmount();
  mountAgain();
  await settle();
  expect(writes).toHaveLength(1);
});
it("读取选项失败立即清除旧联系方式，不能继续启动旧表单", async () => {
  let deny = false;
  const { host, writes } = await mount("/customers/a", {
    read: (r) =>
      deny && new URL(r.url).pathname.endsWith("/follow-up")
        ? Promise.resolve(json({}, 503))
        : undefined,
  });
  await choose(host);
  deny = true;
  button(host, "刷新").click();
  await settle();
  expect(host.querySelectorAll("select[data-followup-field]")).toHaveLength(0);
  expect(button(host, "准备单封手动联系")).toBeUndefined();
  expect(writes).toHaveLength(0);
});
it("切换身份后迟到的启动成功不能恢复旧客户或原恢复记录", async () => {
  let resolve!: (r: Response) => void;
  const delayed = new Promise<Response>((r) => {
    resolve = r;
  });
  const { host, writes, switchIdentity } = await mount("/customers/a", {
    post: async () => delayed,
  });
  await choose(host);
  button(host, "准备单封手动联系").click();
  await settle();
  switchIdentity();
  await settle();
  resolve(json(enrollment("a", "enrolled", "manual")));
  await settle();
  expect(host.textContent).not.toContain("Company a");
  expect(host.textContent).not.toContain("已加入跟进");
  expect(host.textContent).not.toContain("已准备手动联系");
  expect(sessionStorage.length).toBe(0);
  expect(writes).toHaveLength(1);
});
it("跟进can_start为false时即使有已验证邮箱和假设仍不能提交", async () => {
  const { host, writes } = await mount("/customers/a", {
    read: (r) => {
      if (!new URL(r.url).pathname.endsWith("/follow-up")) return;
      const o = options("a", "manual");
      o.campaigns[0].can_start = false;
      o.campaigns[0].blocking_reasons = ["campaign_not_approved"];
      return Promise.resolve(json(o));
    },
  });
  await choose(host);
  expect(host.textContent).toContain("当前任务版本没有有效审批");
  expect(button(host, "准备单封手动联系").disabled).toBe(true);
  expect(writes).toHaveLength(0);
});
it("待停止权限false时活跃序列也没有停止操作", async () => {
  const { host } = await mount("/customers/a", {
    read: (r) =>
      new URL(r.url).pathname === "/prospects/customers/a"
        ? Promise.resolve(
            json({
              allocation: allocation("a"),
              account: null,
              owner: "emp_other",
              contacts: [],
              enrollments: [enrollment("a")],
              can_stop: false,
              blocking_reasons: ["not_customer_owner"],
            }),
          )
        : undefined,
  });
  expect(button(host, "停止跟进")).toBeUndefined();
});
it("分页迟到响应在换身份后不能恢复旧客户", async () => {
  let resolve!: (r: Response) => void;
  const delayed = new Promise<Response>((r) => {
    resolve = r;
  });
  const { host, switchIdentity } = await mount("/customers", {
    read: (r) => {
      const u = new URL(r.url);
      if (u.pathname !== "/prospects/customers") return;
      return u.searchParams.has("cursor")
        ? delayed
        : Promise.resolve(
            json(Array.from({ length: 50 }, (_, i) => allocation(`page_${i}`))),
          );
    },
  });
  button(host, "加载更多").click();
  await settle();
  switchIdentity();
  resolve(json([allocation("late")]));
  await settle();
  expect(host.textContent).not.toContain("Company late");
  expect(host.querySelectorAll(".customer-card")).toHaveLength(0);
});
it("本人列表403清除同身份未决批量跟进，不保留可恢复表单", async () => {
  let denyList = false;
  const { host, writes } = await mount("/customers", {
    post: async () => json({}, 503),
    read: (r) =>
      denyList && new URL(r.url).pathname === "/prospects/customers"
        ? Promise.resolve(json({}, 403))
        : undefined,
  });
  host.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click();
  await settle();
  await choose(host);
  button(host, "逐项启动").click();
  await settle();
  expect(writes).toHaveLength(2);
  expect(sessionStorage.length).toBe(1);
  denyList = true;
  button(host, "刷新").click();
  await settle();
  expect(sessionStorage.length).toBe(0);
  expect(host.querySelector('[aria-label="逐客户跟进结果"]')).toBeNull();
});
it("显式关联成功后刷新尚未完成时换身份，不显示旧身份完成提示", async () => {
  let detailReads = 0;
  let resolve!: (r: Response) => void;
  const delayed = new Promise<Response>(r => { resolve = r; });
  const { host, switchIdentity } = await mount("/customers/a", { role: "boss", pending: true, read: request => {
    if (new URL(request.url).pathname === "/prospects/customers/a" && ++detailReads > 1) return delayed;
  }});
  button(host, "关联客户档案").click(); await settle(); switchIdentity(); await settle();
  resolve(json({ allocation: allocation("a"), account: null, owner: "emp_a", contacts: [], enrollments: [], can_stop: false, blocking_reasons: [] }));
  await settle(); expect(host.textContent).not.toContain("已请求关联原负责人的客户档案");
});
it("100条合法未决请求恢复后第101位客户跟进拒绝发送并释放批量busy", async () => {
  const original = JSON.stringify({ owner: JSON.stringify(["fixed-dev", "tn_a", "emp_a"]), records: Array.from({ length: 100 }, (_, i) => ({ allocationId: `saved_${i}`, key: `key_${i}`, body: { campaign_id: `cam_saved_${i}`, campaign_version: 3, contact_point_id: `cp_saved_${i}`, source_hypothesis_id: `hyp_saved_${i}` } })) });
  sessionStorage.setItem("tradeos-customer-followup-pending-v1", original);
  const { host, writes, app } = await mount();
  const errors: unknown[] = []; app.config.errorHandler = error => errors.push(error);
  host.querySelector<HTMLInputElement>('[aria-label="选择 Company a"]')!.click(); await settle();
  const row = [...host.querySelectorAll<HTMLElement>('[aria-label="逐客户跟进结果"] > article')].find(row => row.querySelector('h2')?.textContent === "Company a")!;
  await choose(row); button(host, "逐项启动").click(); await settle();
  expect(errors).toHaveLength(0); expect(writes).toHaveLength(0);
  expect(row.textContent).toContain("未提交");
  expect(button(host, "刷新客户").disabled).toBe(false);
  expect(button(host, "逐项启动").disabled).toBe(false);
  expect(sessionStorage.getItem("tradeos-customer-followup-pending-v1")).toBe(original);
});

it("单项意外拒绝不会永久锁住批量或隐藏已选客户结果", async () => {
  const { host, writes, app } = await mount();
  const errors: unknown[] = []; app.config.errorHandler = error => errors.push(error);
  host.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click(); await settle(); await choose(host);
  vi.spyOn(globalThis.crypto, "randomUUID").mockImplementation(() => { throw new Error("key generation unavailable"); });
  button(host, "逐项启动").click(); await settle();
  expect(errors).toHaveLength(0); expect(writes).toHaveLength(0);
  expect(button(host, "刷新客户").disabled).toBe(false);
  expect(host.querySelector('[aria-label="逐客户跟进结果"]')).not.toBeNull();
  expect(host.textContent).toContain("批量跟进未完成");
});


it("持久写失败时逐项跟进零POST，已有未知原请求与刷新恢复保持完整", async () => {
  const original = JSON.stringify({ owner: JSON.stringify(["fixed-dev", "tn_a", "emp_a"]), records: [{ allocationId: "saved", key: "original_saved", body: { campaign_id: "cam_saved", campaign_version: 3, contact_point_id: "cp_saved", source_hypothesis_id: "hyp_saved" } }] });
  sessionStorage.setItem("tradeos-customer-followup-pending-v1", original);
  const { host, writes, app } = await mount();
  host.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click(); await settle(); await choose(host);
  const storage = sessionStorage;
  vi.stubGlobal("sessionStorage", { getItem: storage.getItem.bind(storage), removeItem: storage.removeItem.bind(storage), setItem: () => { throw new DOMException("full", "QuotaExceededError"); } });
  button(host, "逐项启动").click(); await settle();
  expect(writes).toHaveLength(0);
  expect(host.textContent).toContain("未提交");
  expect(button(host, "刷新客户").disabled).toBe(false);
  expect(button(host, "逐项启动").disabled).toBe(false);
  expect(sessionStorage.getItem("tradeos-customer-followup-pending-v1")).toBe(original);
  app.unmount(); vi.restoreAllMocks(); vi.unstubAllGlobals();
  const fresh = await mount("/customers/saved", { post: async () => json({}, 503) });
  expect(fresh.writes).toHaveLength(0);
  button(fresh.host, "核对原跟进请求").click(); await settle();
  expect(fresh.writes[0].headers.get("Idempotency-Key")).toBe("original_saved");
  expect(await fresh.writes[0].json()).toEqual({ campaign_id: "cam_saved", campaign_version: 3, contact_point_id: "cp_saved", source_hypothesis_id: "hyp_saved" });
});

it("持久读取失败不能冒充空恢复状态提交新命令", async () => {
  const storage = sessionStorage;
  vi.stubGlobal("sessionStorage", { getItem: () => { throw new DOMException("blocked", "SecurityError"); }, setItem: storage.setItem.bind(storage), removeItem: storage.removeItem.bind(storage) });
  const { host, writes } = await mount("/customers/a");
  await choose(host); button(host, "准备单封手动联系").click(); await settle();
  expect(writes).toHaveLength(0); expect(host.textContent).toContain("未提交");
});

it("原请求未知后当前条件拒绝仍保留原键，刷新只读并允许显式核对", async () => {
  let changed = false;
  const { host, writes, app } = await mount("/customers/a", {
    post: async () => json({}, changed ? 400 : 503),
    read: request => {
      if (!changed || !new URL(request.url).pathname.endsWith("/follow-up")) return;
      const value = options("a", "manual"); value.hypotheses = []; value.contacts = []; value.campaigns = [];
      value.blocking_reasons = ["product_fit_missing", "no_active_campaign", "contact_unverified"];
      return Promise.resolve(json(value));
    },
  });
  await choose(host); button(host, "准备单封手动联系").click(); await settle();
  const original = sessionStorage.getItem("tradeos-customer-followup-pending-v1");
  changed = true; button(host, "刷新").click(); await settle();
  expect(writes).toHaveLength(1); expect(button(host, "核对原跟进请求").disabled).toBe(false);
  button(host, "核对原跟进请求").click(); await settle();
  expect(writes).toHaveLength(2);
  expect(sessionStorage.getItem("tradeos-customer-followup-pending-v1")).toBe(original);
  expect(writes[1].headers.get("Idempotency-Key")).toBe(writes[0].headers.get("Idempotency-Key"));
  expect(await writes[1].json()).toEqual(await writes[0].clone().json());
  app.unmount();
  const fresh = await mount("/customers/a", { post: async () => json(enrollment("a", "enrolled", "manual")), read: request => new URL(request.url).pathname.endsWith("/follow-up") ? Promise.resolve(json({}, 503)) : undefined });
  expect(fresh.writes).toHaveLength(0);
  button(fresh.host, "核对原跟进请求").click(); await settle();
  expect(fresh.writes).toHaveLength(1);
  expect(fresh.writes[0].headers.get("Idempotency-Key")).toBe(writes[0].headers.get("Idempotency-Key"));
  expect(await fresh.writes[0].json()).toEqual(await writes[0].clone().json());
  expect(sessionStorage.length).toBe(0);
});
