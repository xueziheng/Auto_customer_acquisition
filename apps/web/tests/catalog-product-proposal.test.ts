import { createApp, nextTick, type App as VueApp } from "vue";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "../src/App.vue";
import { createApiClient, type WebIdentityProvider, type WebRequestIdentity } from "../src/api/client";
import router from "../src/router";

const mountedApps: VueApp[] = [];
const candidateWarning = "这是一项候选产品培养建议，不代表已确认供应、正式产品或可报价价格。";

afterEach(() => {
  mountedApps.splice(0).forEach((app) => app.unmount());
  document.body.replaceChildren();
  void router.replace("/");
});

async function eventually(assertion: () => void): Promise<void> {
  let latest: unknown;
  for (let attempt = 0; attempt < 100; attempt += 1) {
    await nextTick();
    await new Promise((resolve) => setTimeout(resolve, 0));
    try {
      assertion();
      return;
    } catch (error) {
      latest = error;
    }
  }
  throw latest;
}

function catalogPathResponse(path: string): Response | null {
  if (path === "/notifications") return Response.json([], { status: 200 });
  if (path === "/products") return Response.json([], { status: 200 });
  if (path === "/products/catalog-policies/active") return Response.json(null, { status: 200 });
  if (path === "/products/catalog-policies") return Response.json([], { status: 200 });
  if (path === "/products/catalog-evaluations") return Response.json([], { status: 200 });
  if (path === "/products/catalog-proposals") return Response.json([], { status: 200 });
  if (path === "/products/catalog-cultivation-cases") return Response.json([], { status: 200 });
  return null;
}

function identityHarness(name: string) {
  let current: WebRequestIdentity = {
    employeeId: `employee-${name}-a`,
    mode: "authenticated",
    tenantId: `tenant-${name}-a`,
  };
  let generation = 0;
  const listeners = new Set<() => void>();
  const provider: WebIdentityProvider = {
    current: () => current,
    generation: () => generation,
    subscribe(listener) {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
  };
  return {
    provider,
    switchTo(suffix: string) {
      current = {
        employeeId: `employee-${name}-${suffix}`,
        mode: "authenticated",
        tenantId: `tenant-${name}-${suffix}`,
      };
      generation += 1;
      for (const listener of listeners) listener();
    },
  };
}

function deferredResponse() {
  let resolve!: (response: Response) => void;
  const promise = new Promise<Response>((done) => { resolve = done; });
  return { promise, resolve };
}

async function mountProductsInstance(
  fetch: typeof globalThis.fetch,
  provider: WebIdentityProvider = identityHarness(`default-${mountedApps.length}-${Date.now()}`).provider,
  client = createApiClient({ baseUrl: "https://tradeos.test", fetch }, provider),
): Promise<{ app: VueApp; root: HTMLElement }> {
  const root = document.createElement("div");
  document.body.replaceChildren(root);
  const app = createApp(App);
  app.provide("tradeos-api-client", client);
  app.use(router);
  await router.replace("/products");
  app.mount(root);
  mountedApps.push(app);
  return { app, root };
}

async function mountProducts(
  fetch: typeof globalThis.fetch,
  provider?: WebIdentityProvider,
): Promise<HTMLElement> {
  return (await mountProductsInstance(fetch, provider)).root;
}

function setField(root: HTMLElement, name: string, value: string): void {
  const input = root.querySelector<HTMLInputElement>(`[name="${name}"]`);
  expect(input, name).not.toBeNull();
  if (!input) return;
  input.value = value;
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

function submitPolicyForm(root: HTMLElement): void {
  const form = root.querySelector<HTMLFormElement>(".policy-form");
  expect(form).not.toBeNull();
  form?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
}

function policyView(
  id: string,
  state: "pending_approval" | "active" | "superseded" | "rejected" | "expired" | "stale",
  approvalState: "pending" | "approved" | "applied" | "apply_failed" | "rejected" | "expired" | null,
) {
  return {
    approval: approvalState ? {
      approval_id: `apr_${id}`,
      approval_type: "catalog_proposal_policy_change",
      state: approvalState,
    } : null,
    policy: {
      activated_at: state === "active" ? "2026-09-05T01:00:00Z" : null,
      approval_id: approvalState ? `apr_${id}` : null,
      base_active_version_id: null,
      content: {
        minimum_distinct_accounts: 3,
        minimum_distinct_countries: null,
        minimum_quantity_unit_accounts: null,
        minimum_recurring_accounts: null,
        require_unified_unit: false,
      },
      content_hash: `hash_${id}`,
      created_at: "2026-09-05T00:00:00Z",
      policy_version_id: id,
      proposed_by: "emp_product_owner",
      state,
      terminal_at: state === "active" || state === "pending_approval" ? null : "2026-09-05T02:00:00Z",
    },
  };
}

function canonicalEvaluation() {
  return {
    blocked_reason: null,
    cluster_id: "ncl_catalog_1",
    created_at: "2026-09-05T03:00:00Z",
    evaluation_id: "cev_catalog_1",
    facts: {
      cluster_category: "工业紧固件",
      cluster_id: "ncl_catalog_1",
      display_codes: ["catalog_distinct_accounts_ready"],
      distinct_account_count: 3,
      distinct_account_ids: ["acc_1", "acc_2", "acc_3"],
      evidence_summaries: [{
        confirmed_at: "2026-09-04T01:00:00Z",
        confirmed_by: "emp_1",
        content_hash: "sha256:evidence-safe",
        extracted_by: "qualification_agent",
        observed_at: "2026-09-04T00:00:00Z",
        source_id: "msg_safe_1",
        source_type: "conversation",
      }],
      facts_hash: "facts_catalog_1",
      facts_observed_at: "2026-09-05T02:30:00Z",
      known_country_codes: ["DE"],
      member_count: 3,
      member_need_ids: ["ned_1", "ned_2", "ned_3"],
      quantity_unit_covered_account_count: 2,
      recurring_false_account_count: 0,
      recurring_true_account_count: 2,
      recurring_unknown_account_count: 1,
      safe_total_quantity: null,
      tenant_id: "tenant_hidden_by_api_client_contract",
      unified_unit: null,
      unknown_country_account_count: 2,
    },
    facts_hash: "facts_catalog_1",
    overall_passed: false,
    policy_version_id: "cpv_active",
    proposed_by_run: "run_catalog_1",
    rule_results: [
      { actual_value: true, explanation_code: "成员关系与品类完整一致", required_value: true, rule: "membership_integrity", status: "passed" },
      { actual_value: 2, explanation_code: "去重客户数未达到策略门槛", required_value: 3, rule: "distinct_accounts", status: "failed" },
      { actual_value: null, explanation_code: "复购客户事实不完整", required_value: 2, rule: "recurring_accounts", status: "unknown" },
      { actual_value: null, explanation_code: "策略不要求已知国家数", required_value: null, rule: "distinct_countries", status: "not_required" },
    ],
  };
}

function proposal(id: string, state: "awaiting_approval_submission" | "pending_review" | "cultivation_queued" | "rejected" | "expired" | "stale") {
  return {
    approval: state === "pending_review" ? {
      approval_id: `apr_${id}`,
      approval_type: "catalog_product_cultivation",
      state: "pending",
    } : null,
    proposal: {
      approval_id: state === "pending_review" ? `apr_${id}` : null,
      cluster_id: "ncl_catalog_1",
      created_at: "2026-09-05T03:10:00Z",
      evaluation_id: "cev_catalog_1",
      facts_hash: "facts_catalog_1",
      owner_employee: "emp_product_owner",
      policy_version_id: "cpv_active",
      proposal_id: id,
      proposed_by_run: "run_catalog_1",
      state,
      updated_at: "2026-09-05T03:20:00Z",
    },
  };
}

function cultivationCase(id: string, approvalId = `apr_${id}`) {
  return {
    approval: {
      approval_id: approvalId,
      approval_type: "catalog_product_cultivation",
      state: "approved",
    },
    cultivation_case: {
      approval_id: approvalId,
      cluster_id: "ncl_catalog_1",
      cultivation_case_id: id,
      evidence_refs: ["ev_safe_a"],
      facts_hash: "facts_catalog_1",
      policy_version_id: "cpv_active",
      proposal_id: "cpp_queued",
      queued_at: "2026-09-05T04:00:00Z",
      state: "queued",
    },
  };
}

function approvalView(approvalId: string, title: string, state = "applied") {
  return {
    affected_entities: ["catalog-product-proposal"],
    approval_id: approvalId,
    approval_type: "catalog_product_cultivation",
    can_current_user_decide: false,
    created_at: "2026-09-05T00:00:00Z",
    expires_at: "2026-09-06T00:00:00Z",
    if_approved: "进入受控培养队列",
    if_rejected: "保持候选状态",
    proposed_change_display: { "候选产品": "工业紧固件" },
    reason: "经人工审批的目录培养建议",
    reversible: true,
    state,
    title,
    type_label: "目录产品培养",
  };
}

describe("Catalog Product Proposal internal regions", () => {
  it("shows no-policy fail-closed state and submits only the controlled three-account content", async () => {
    const posts: Request[] = [];
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const request = input as Request;
      const path = new URL(request.url).pathname;
      if (path === "/products/catalog-policies" && request.method === "POST") {
        posts.push(request.clone());
        return Response.json(policyView("cpv_new", "pending_approval", "pending"), { status: 202 });
      }
      return catalogPathResponse(path) ?? Response.json({}, { status: 500 });
    });
    const root = await mountProducts(fetch);

    await eventually(() => {
      expect(root.textContent).toContain("未配置即关闭");
      expect(root.textContent).toContain("当前没有可展示的目录提案");
      expect(root.textContent).toContain("当前没有排队中的培养 Case");
    });
    expect((root.querySelector('[name="minimum_distinct_accounts"]') as HTMLInputElement).value).toBe("3");
    expect((root.querySelector('[name="require_unified_unit"]') as HTMLInputElement).checked).toBe(false);
    (root.querySelector('[data-action="submit-catalog-policy"]') as HTMLButtonElement).click();

    await eventually(() => expect(posts).toHaveLength(1));
    expect(await posts[0]!.json()).toEqual({
      minimum_distinct_accounts: 3,
      minimum_distinct_countries: null,
      minimum_quantity_unit_accounts: null,
      minimum_recurring_accounts: null,
      require_unified_unit: false,
    });
    expect(Object.keys(await posts[0]!.clone().json())).toEqual([
      "minimum_distinct_accounts",
      "minimum_distinct_countries",
      "minimum_quantity_unit_accounts",
      "minimum_recurring_accounts",
      "require_unified_unit",
    ]);
    const rawKey = posts[0]!.headers.get("Idempotency-Key");
    expect(rawKey).toBeTruthy();
    expect(root.textContent).not.toContain(rawKey!);
  });

  it("reuses the exact original key and body after transport uncertainty", async () => {
    const posts: Request[] = [];
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const request = input as Request;
      const path = new URL(request.url).pathname;
      if (path === "/products/catalog-policies" && request.method === "POST") {
        posts.push(request.clone());
        if (posts.length === 1) throw new TypeError("controlled transport uncertainty");
        return Response.json(policyView("cpv_transport", "pending_approval", "pending"), { status: 202 });
      }
      return catalogPathResponse(path) ?? Response.json({}, { status: 500 });
    });
    const root = await mountProducts(fetch);
    await eventually(() => expect(root.textContent).toContain("未配置即关闭"));

    (root.querySelector('[data-action="submit-catalog-policy"]') as HTMLButtonElement).click();
    await eventually(() => expect(root.textContent).toContain("提交结果未知"));
    (root.querySelector('[data-action="retry-catalog-policy"]') as HTMLButtonElement).click();
    await eventually(() => expect(posts).toHaveLength(2));

    expect(posts[1]!.headers.get("Idempotency-Key")).toBe(posts[0]!.headers.get("Idempotency-Key"));
    expect(await posts[1]!.text()).toBe(await posts[0]!.text());
    expect(root.textContent).not.toContain(posts[0]!.headers.get("Idempotency-Key")!);
  });

  it("reuses the exact original key and body after a retryable 503", async () => {
    const posts: Request[] = [];
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const request = input as Request;
      const path = new URL(request.url).pathname;
      if (path === "/products/catalog-policies" && request.method === "POST") {
        posts.push(request.clone());
        if (posts.length === 1) return Response.json({ code: "catalog_unavailable", message: "safe" }, { status: 503 });
        return Response.json(policyView("cpv_503", "pending_approval", "pending"), { status: 202 });
      }
      return catalogPathResponse(path) ?? Response.json({}, { status: 500 });
    });
    const root = await mountProducts(fetch);
    await eventually(() => expect(root.textContent).toContain("未配置即关闭"));

    (root.querySelector('[data-action="submit-catalog-policy"]') as HTMLButtonElement).click();
    await eventually(() => expect(root.textContent).toContain("策略服务暂不可用"));
    (root.querySelector('[data-action="retry-catalog-policy"]') as HTMLButtonElement).click();
    await eventually(() => expect(posts).toHaveLength(2));

    expect(posts[1]!.headers.get("Idempotency-Key")).toBe(posts[0]!.headers.get("Idempotency-Key"));
    expect(await posts[1]!.text()).toBe(await posts[0]!.text());
  });

  it("renders active rules, bounded history states, and linked approval state", async () => {
    const active = policyView("cpv_active", "active", "applied");
    const history = [
      policyView("cpv_pending", "pending_approval", "pending"),
      policyView("cpv_old", "superseded", "applied"),
    ];
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const request = input as Request;
      const url = new URL(request.url);
      if (url.pathname === "/products/catalog-policies/active") return Response.json(active, { status: 200 });
      if (url.pathname === "/products/catalog-policies") {
        expect(url.searchParams.get("limit")).toBe("50");
        return Response.json(history, { status: 200 });
      }
      return catalogPathResponse(url.pathname) ?? Response.json({}, { status: 500 });
    });
    const root = await mountProducts(fetch);

    await eventually(() => {
      expect(root.textContent).toContain("cpv_active");
      expect(root.textContent).toContain("待审批");
      expect(root.textContent).toContain("生效中");
      expect(root.textContent).toContain("已被替代");
      expect(root.textContent).toContain("审批状态：待处理");
      expect(root.textContent).toContain("审批状态：已应用");
    });
    expect(root.querySelector('a[href="/approvals?approval_id=apr_cpv_pending"]')).not.toBeNull();
    expect(root.querySelector('a[href="/approvals?approval_id=apr_cpv_active"]')).not.toBeNull();
    expect(root.textContent).toContain("去重客户数至少 3");
    expect(root.textContent).toContain("不要求统一单位");
  });

  it("keeps backend rule order and exact status mappings while filtering every proposal state group", async () => {
    const proposals = [
      proposal("cpp_awaiting", "awaiting_approval_submission"),
      proposal("cpp_review", "pending_review"),
      proposal("cpp_rejected", "rejected"),
      proposal("cpp_expired", "expired"),
      proposal("cpp_stale", "stale"),
      proposal("cpp_queued", "cultivation_queued"),
    ];
    const evaluation = canonicalEvaluation();
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const path = new URL((input as Request).url).pathname;
      if (path === "/products/catalog-evaluations") return Response.json([evaluation], { status: 200 });
      if (path === "/products/catalog-proposals") return Response.json(proposals, { status: 200 });
      return catalogPathResponse(path) ?? Response.json({}, { status: 500 });
    });
    const root = await mountProducts(fetch);

    await eventually(() => expect(root.textContent).toContain("去重客户数未达到策略门槛"));
    const ruleText = [...root.querySelectorAll('[data-rule-result]')].map((element) => element.textContent ?? "");
    expect(ruleText).toHaveLength(4);
    expect(ruleText[0]).toContain("通过");
    expect(ruleText[1]).toContain("未通过");
    expect(ruleText[2]).toContain("未知");
    expect(ruleText[3]).toContain("不要求");
    expect(ruleText.join("|")).toContain("成员关系与品类完整一致");
    expect(ruleText.join("|")).toContain("策略不要求已知国家数");

    expect(root.textContent).toContain("cpp_awaiting");
    expect(root.textContent).toContain("cpp_review");
    for (const [group, id] of [
      ["rejected", "cpp_rejected"],
      ["expired", "cpp_expired"],
      ["stale", "cpp_stale"],
      ["cultivation_queued", "cpp_queued"],
    ]) {
      (root.querySelector(`[data-proposal-filter="${group}"]`) as HTMLButtonElement).click();
      await eventually(() => expect(root.textContent).toContain(id));
    }
    const proposalRegion = root.querySelector('[data-region="catalog-proposals"]');
    const cultivationRegion = root.querySelector('[data-region="catalog-cultivation"]');
    expect(proposalRegion?.textContent).toContain(candidateWarning);
    expect(`${proposalRegion?.textContent}${cultivationRegion?.textContent}`).not.toMatch(/自动批准|创建正式产品|生成报价|联系供应商|概率|probability/i);
  });

  it("joins cultivation facts only by exact cluster, policy, and facts bindings", async () => {
    const evaluation = canonicalEvaluation();
    const cases = [{
      approval: {
        approval_id: "apr_cultivation_1",
        approval_type: "catalog_product_cultivation",
        state: "approved",
      },
      cultivation_case: {
        approval_id: "apr_cultivation_1",
        cluster_id: "ncl_catalog_1",
        cultivation_case_id: "ccs_catalog_1",
        evidence_refs: ["ev_safe_a", "ev_safe_b"],
        facts_hash: "facts_catalog_1",
        policy_version_id: "cpv_active",
        proposal_id: "cpp_queued",
        queued_at: "2026-09-05T04:00:00Z",
        state: "queued",
      },
    }, {
      approval: {
        approval_id: "apr_cultivation_unbound",
        approval_type: "catalog_product_cultivation",
        state: "approved",
      },
      cultivation_case: {
        approval_id: "apr_cultivation_unbound",
        cluster_id: "ncl_catalog_unbound",
        cultivation_case_id: "ccs_catalog_unbound",
        evidence_refs: ["ev_unbound"],
        facts_hash: "facts_other",
        policy_version_id: "cpv_other",
        proposal_id: "cpp_other",
        queued_at: "2026-09-05T05:00:00Z",
        state: "queued",
      },
    }];
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const path = new URL((input as Request).url).pathname;
      if (path === "/products/catalog-evaluations") return Response.json([evaluation], { status: 200 });
      if (path === "/products/catalog-cultivation-cases") return Response.json(cases, { status: 200 });
      return catalogPathResponse(path) ?? Response.json({}, { status: 500 });
    });
    const root = await mountProducts(fetch);

    await eventually(() => expect(root.textContent).toContain("ccs_catalog_1"));
    const bound = root.querySelector('[data-cultivation-case="ccs_catalog_1"]');
    expect(bound?.textContent).toContain("ncl_catalog_1");
    expect(bound?.textContent).toContain("cpp_queued");
    expect(bound?.textContent).toContain("cpv_active");
    expect(bound?.textContent).toContain("去重客户数：3");
    expect(bound?.textContent).toContain("数量/单位证据覆盖：2 / 3");
    expect(bound?.textContent).toContain("复购事实 1 个账户未知");
    expect(bound?.textContent).toContain("客户国家 2 个账户未知");
    expect(bound?.textContent).toContain("安全汇总数量未知");
    expect(bound?.textContent).toContain("ev_safe_a");
    expect(bound?.textContent).toContain("审批状态：已批准");
    expect(bound?.querySelector('a[href="/approvals?approval_id=apr_cultivation_1"]')).not.toBeNull();
    const unbound = root.querySelector('[data-cultivation-case="ccs_catalog_unbound"]');
    expect(unbound?.textContent).toContain("去重客户数：未知");
    expect(root.querySelector('[data-region="catalog-cultivation"]')?.textContent).toContain(candidateWarning);
  });

  it("keeps 403, 404, 503, and transport failures distinct from empty lists", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const path = new URL((input as Request).url).pathname;
      if (path === "/products/catalog-policies/active" || path === "/products/catalog-policies") {
        return Response.json({}, { status: 403 });
      }
      if (path === "/products/catalog-evaluations") throw new TypeError("controlled transport failure");
      if (path === "/products/catalog-proposals") return Response.json({}, { status: 404 });
      if (path === "/products/catalog-cultivation-cases") return Response.json({}, { status: 503 });
      return catalogPathResponse(path) ?? Response.json({}, { status: 500 });
    });
    const root = await mountProducts(fetch);

    await eventually(() => {
      expect(root.textContent).toContain("当前身份无权读取目录策略");
      expect(root.textContent).toContain("目录提案记录不存在或不属于当前租户");
      expect(root.textContent).toContain("培养队列服务暂不可用");
      expect(root.textContent).toContain("无法连接目录评估服务");
    });
    expect(root.querySelector('[data-region="catalog-policy"]')?.textContent).not.toContain("未配置即关闭");
    expect(root.querySelector('[data-region="catalog-proposals"]')?.textContent).not.toContain("当前没有可展示的目录提案");
    expect(root.querySelector('[data-region="catalog-cultivation"]')?.textContent).not.toContain("当前没有排队中的培养 Case");
  });

  it("clears all catalog data on identity change, aborts every channel, and ignores late A responses", async () => {
    const identity = identityHarness("lifecycle");
    const pending = new Map<string, ReturnType<typeof deferredResponse>>();
    const lateRequests: Request[] = [];
    let late = false;
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const request = input as Request;
      const path = new URL(request.url).pathname;
      if (late && request.headers.get("X-Tenant-Id") === "tenant-lifecycle-a" && path.startsWith("/products/catalog-")) {
        const slot = deferredResponse();
        pending.set(`${path}-${pending.size}`, slot);
        lateRequests.push(request);
        return slot.promise;
      }
      if (path === "/products/catalog-policies/active") return Response.json(policyView("cpv_identity_a", "active", "applied"));
      if (path === "/products/catalog-policies") return Response.json([policyView("cpv_history_a", "superseded", "applied")]);
      if (path === "/products/catalog-evaluations") return Response.json([{ ...canonicalEvaluation(), evaluation_id: "cev_identity_a" }]);
      if (path === "/products/catalog-proposals") return Response.json([proposal("cpp_identity_a", "pending_review")]);
      if (path === "/products/catalog-cultivation-cases") return Response.json([cultivationCase("ccs_identity_a")]);
      return catalogPathResponse(path) ?? Response.json({}, { status: 500 });
    });
    const root = await mountProducts(fetch, identity.provider);
    await eventually(() => {
      expect(root.textContent).toContain("cpv_identity_a");
      expect(root.textContent).toContain("cev_identity_a");
      expect(root.textContent).toContain("cpp_identity_a");
      expect(root.textContent).toContain("ccs_identity_a");
    });

    late = true;
    for (const label of ["刷新策略", "刷新提案", "刷新队列"]) {
      [...root.querySelectorAll("button")].find((button) => button.textContent?.includes(label))?.click();
    }
    await eventually(() => expect(pending.size).toBe(5));
    identity.switchTo("b");
    await nextTick();
    expect(lateRequests.every((request) => request.signal.aborted)).toBe(true);
    expect(root.textContent).not.toMatch(/cpv_identity_a|cev_identity_a|cpp_identity_a|ccs_identity_a/);

    for (const [key, slot] of pending) {
      if (key.startsWith("/products/catalog-policies/active")) slot.resolve(Response.json(policyView("cpv_late_a", "active", "applied")));
      else if (key.startsWith("/products/catalog-policies")) slot.resolve(Response.json([policyView("cpv_history_late_a", "superseded", "applied")]));
      else if (key.startsWith("/products/catalog-evaluations")) slot.resolve(Response.json([{ ...canonicalEvaluation(), evaluation_id: "cev_late_a" }]));
      else if (key.startsWith("/products/catalog-proposals")) slot.resolve(Response.json([proposal("cpp_late_a", "pending_review")]));
      else slot.resolve(Response.json([cultivationCase("ccs_late_a")]));
    }
    await nextTick();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(root.textContent).not.toMatch(/late_a/);
  });

  it("keeps last safe same-identity data visible when refreshes fail", async () => {
    let failing = false;
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const path = new URL((input as Request).url).pathname;
      if (failing && path.startsWith("/products/catalog-")) return Response.json({}, { status: 503 });
      if (path === "/products/catalog-policies/active") return Response.json(policyView("cpv_safe", "active", "applied"));
      if (path === "/products/catalog-policies") return Response.json([policyView("cpv_history_safe", "superseded", "applied")]);
      if (path === "/products/catalog-evaluations") return Response.json([{ ...canonicalEvaluation(), evaluation_id: "cev_safe" }]);
      if (path === "/products/catalog-proposals") return Response.json([proposal("cpp_safe", "pending_review")]);
      if (path === "/products/catalog-cultivation-cases") return Response.json([cultivationCase("ccs_safe")]);
      return catalogPathResponse(path) ?? Response.json({}, { status: 500 });
    });
    const root = await mountProducts(fetch);
    await eventually(() => expect(root.textContent).toContain("ccs_safe"));
    failing = true;
    for (const label of ["刷新策略", "刷新提案", "刷新队列"]) {
      [...root.querySelectorAll("button")].find((button) => button.textContent?.includes(label))?.click();
    }
    await eventually(() => {
      expect(root.textContent).toContain("目录策略服务暂不可用");
      expect(root.textContent).toContain("目录评估服务暂不可用");
      expect(root.textContent).toContain("目录提案服务暂不可用");
      expect(root.textContent).toContain("培养队列服务暂不可用");
    });
    expect(root.textContent).toMatch(/cpv_safe/);
    expect(root.textContent).toMatch(/cev_safe/);
    expect(root.textContent).toMatch(/cpp_safe/);
    expect(root.textContent).toMatch(/ccs_safe/);
  });

  it("purges protected catalog projections on same-identity 403 while only transient reads retain safe data", async () => {
    let authorizationRevoked = false;
    let cultivationRevoked = false;
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const path = new URL((input as Request).url).pathname;
      if (authorizationRevoked && path.startsWith("/products/catalog-")) {
        if (path === "/products/catalog-cultivation-cases" && !cultivationRevoked) {
          return Response.json({}, { status: 503 });
        }
        return Response.json({}, { status: 403 });
      }
      if (path === "/products/catalog-policies/active") return Response.json(policyView("cpv_revoked", "active", "applied"));
      if (path === "/products/catalog-policies") return Response.json([policyView("cpv_history_revoked", "superseded", "applied")]);
      if (path === "/products/catalog-evaluations") return Response.json([{ ...canonicalEvaluation(), evaluation_id: "cev_revoked" }]);
      if (path === "/products/catalog-proposals") return Response.json([proposal("cpp_revoked", "pending_review")]);
      if (path === "/products/catalog-cultivation-cases") return Response.json([cultivationCase("ccs_revoked")]);
      return catalogPathResponse(path) ?? Response.json({}, { status: 500 });
    });
    const root = await mountProducts(fetch);
    await eventually(() => {
      expect(root.textContent).toMatch(/cpv_revoked|cev_revoked|cpp_revoked|ccs_revoked/);
      expect(root.querySelector('[data-cultivation-case="ccs_revoked"]')?.textContent).toContain("去重客户数：3");
    });

    authorizationRevoked = true;
    for (const label of ["刷新策略", "刷新提案", "刷新队列"]) {
      [...root.querySelectorAll("button")].find((button) => button.textContent?.includes(label))?.click();
    }
    await eventually(() => {
      expect(root.textContent).toContain("当前身份无权读取目录策略");
      expect(root.textContent).toContain("当前身份无权读取目录评估");
      expect(root.textContent).toContain("当前身份无权读取目录提案");
      expect(root.textContent).toContain("培养队列服务暂不可用");
    });
    expect(root.textContent).not.toMatch(/cpv_revoked|cpv_history_revoked|cev_revoked|cpp_revoked/);
    expect(root.querySelector('[data-cultivation-case="ccs_revoked"]')?.textContent).toContain("去重客户数：未知");

    cultivationRevoked = true;
    [...root.querySelectorAll("button")].find((button) => button.textContent?.includes("刷新队列"))?.click();
    await eventually(() => expect(root.textContent).toContain("当前身份无权读取培养队列"));
    expect(root.textContent).not.toContain("ccs_revoked");
  });

  it("prevents older overlapping catalog refreshes from overwriting newer results", async () => {
    const old = new Map<string, ReturnType<typeof deferredResponse>>();
    const calls = new Map<string, number>();
    let overlap = false;
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const path = new URL((input as Request).url).pathname;
      if (overlap && path.startsWith("/products/catalog-")) {
        const count = (calls.get(path) ?? 0) + 1;
        calls.set(path, count);
        if (count === 1) {
          const slot = deferredResponse();
          old.set(path, slot);
          return slot.promise;
        }
        if (path === "/products/catalog-policies/active") return Response.json(policyView("cpv_newer", "active", "applied"));
        if (path === "/products/catalog-policies") return Response.json([policyView("cpv_history_newer", "superseded", "applied")]);
        if (path === "/products/catalog-evaluations") return Response.json([{ ...canonicalEvaluation(), evaluation_id: "cev_newer" }]);
        if (path === "/products/catalog-proposals") return Response.json([proposal("cpp_newer", "pending_review")]);
        return Response.json([cultivationCase("ccs_newer")]);
      }
      return catalogPathResponse(path) ?? Response.json({}, { status: 500 });
    });
    const root = await mountProducts(fetch);
    await eventually(() => expect(root.textContent).toContain("未配置即关闭"));
    overlap = true;
    for (const label of ["刷新策略", "刷新提案", "刷新队列"]) {
      const button = [...root.querySelectorAll("button")].find((candidate) => candidate.textContent?.includes(label));
      button?.click();
      button?.click();
    }
    await eventually(() => expect(root.textContent).toContain("ccs_newer"));
    for (const [path, slot] of old) {
      if (path === "/products/catalog-policies/active") slot.resolve(Response.json(policyView("cpv_older", "active", "applied")));
      else if (path === "/products/catalog-policies") slot.resolve(Response.json([policyView("cpv_history_older", "superseded", "applied")]));
      else if (path === "/products/catalog-evaluations") slot.resolve(Response.json([{ ...canonicalEvaluation(), evaluation_id: "cev_older" }]));
      else if (path === "/products/catalog-proposals") slot.resolve(Response.json([proposal("cpp_older", "pending_review")]));
      else slot.resolve(Response.json([cultivationCase("ccs_older")]));
    }
    await nextTick();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(root.textContent).toMatch(/cpv_newer|cev_newer|cpp_newer|ccs_newer/);
    expect(root.textContent).not.toMatch(/older/);
  });

  it("lets a submit-triggered refresh supersede in-flight initial proposal and cultivation reads", async () => {
    const initial = new Map<string, ReturnType<typeof deferredResponse>>();
    const calls = new Map<string, number>();
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const request = input as Request;
      const path = new URL(request.url).pathname;
      if (path === "/products/catalog-policies" && request.method === "POST") {
        return Response.json(policyView("cpv_trigger", "pending_approval", "pending"), { status: 202 });
      }
      if (["/products/catalog-evaluations", "/products/catalog-proposals", "/products/catalog-cultivation-cases"].includes(path)) {
        const count = (calls.get(path) ?? 0) + 1;
        calls.set(path, count);
        if (count === 1) {
          const slot = deferredResponse();
          initial.set(path, slot);
          return slot.promise;
        }
        if (path === "/products/catalog-evaluations") return Response.json([{ ...canonicalEvaluation(), evaluation_id: "cev_trigger_new" }]);
        if (path === "/products/catalog-proposals") return Response.json([proposal("cpp_trigger_new", "pending_review")]);
        return Response.json([cultivationCase("ccs_trigger_new")]);
      }
      return catalogPathResponse(path) ?? Response.json({}, { status: 500 });
    });
    const root = await mountProducts(fetch);
    await eventually(() => expect(root.querySelector(".policy-form")).not.toBeNull());
    submitPolicyForm(root);
    await eventually(() => expect(root.textContent).toContain("ccs_trigger_new"));
    initial.get("/products/catalog-evaluations")?.resolve(Response.json([{ ...canonicalEvaluation(), evaluation_id: "cev_trigger_old" }]));
    initial.get("/products/catalog-proposals")?.resolve(Response.json([proposal("cpp_trigger_old", "pending_review")]));
    initial.get("/products/catalog-cultivation-cases")?.resolve(Response.json([cultivationCase("ccs_trigger_old")]));
    await nextTick();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(root.textContent).toMatch(/cev_trigger_new|cpp_trigger_new|ccs_trigger_new/);
    expect(root.textContent).not.toMatch(/trigger_old/);
  });

  it("aborts a pending policy mutation and ignores its late receipt after identity change", async () => {
    const identity = identityHarness("policy-late-action");
    const pending = deferredResponse();
    const postRequests: Request[] = [];
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const request = input as Request;
      const path = new URL(request.url).pathname;
      if (path === "/products/catalog-policies" && request.method === "POST") {
        postRequests.push(request);
        return pending.promise;
      }
      return catalogPathResponse(path) ?? Response.json({}, { status: 500 });
    });
    const root = await mountProducts(fetch, identity.provider);
    await eventually(() => expect(root.querySelector(".policy-form")).not.toBeNull());
    submitPolicyForm(root);
    await eventually(() => expect(postRequests).toHaveLength(1));
    identity.switchTo("b");
    await nextTick();
    expect(postRequests[0]!.signal.aborted).toBe(true);
    pending.resolve(Response.json(policyView("cpv_late_action", "pending_approval", "pending"), { status: 202 }));
    await nextTick();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(root.textContent).not.toContain("策略候选已提交");
    expect(root.textContent).not.toContain("cpv_late_action");
  });

  it("never reuses an uncertain A intent under B and clears mutation state on identity switch", async () => {
    const identity = identityHarness("policy-cross-identity");
    const posts: Request[] = [];
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const request = input as Request;
      const path = new URL(request.url).pathname;
      if (path === "/products/catalog-policies" && request.method === "POST") {
        posts.push(request.clone());
        if (posts.length === 1) throw new TypeError("uncertain A");
        return Response.json(policyView("cpv_b", "pending_approval", "pending"), { status: 202 });
      }
      return catalogPathResponse(path) ?? Response.json({}, { status: 500 });
    });
    const root = await mountProducts(fetch, identity.provider);
    await eventually(() => expect(root.textContent).toContain("未配置即关闭"));
    submitPolicyForm(root);
    await eventually(() => expect(root.textContent).toContain("提交结果未知"));
    const aKey = posts[0]!.headers.get("Idempotency-Key");
    identity.switchTo("b");
    await nextTick();
    expect(root.textContent).not.toContain("提交结果未知");
    expect(root.querySelector('[data-action="retry-catalog-policy"]')).toBeNull();
    await eventually(() => expect(root.querySelector(".policy-form")).not.toBeNull());
    submitPolicyForm(root);
    await eventually(() => expect(posts).toHaveLength(2));
    expect(posts[1]!.headers.get("X-Tenant-Id")).toBe("tenant-policy-cross-identity-b");
    expect(posts[1]!.headers.get("Idempotency-Key")).not.toBe(aKey);
    expect(root.textContent).not.toContain(aKey!);
  });

  it("restores the exact unresolved A intent after switching A to B and back within one component", async () => {
    const identity = identityHarness("policy-identity-return");
    const posts: Request[] = [];
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const request = input as Request;
      const path = new URL(request.url).pathname;
      if (path === "/products/catalog-policies" && request.method === "POST") {
        posts.push(request.clone());
        throw new TypeError("controlled uncertainty");
      }
      return catalogPathResponse(path) ?? Response.json({}, { status: 500 });
    });
    const client = createApiClient({ baseUrl: "https://tradeos.test", fetch }, identity.provider);
    const root = (await mountProductsInstance(fetch, identity.provider, client)).root;
    await eventually(() => expect(root.querySelector(".policy-form")).not.toBeNull());
    setField(root, "minimum_distinct_accounts", "4");
    setField(root, "minimum_recurring_accounts", "2");
    setField(root, "minimum_distinct_countries", "5");
    setField(root, "minimum_quantity_unit_accounts", "3");
    const unified = root.querySelector<HTMLInputElement>('[name="require_unified_unit"]')!;
    unified.checked = true;
    unified.dispatchEvent(new Event("change", { bubbles: true }));
    await nextTick();
    submitPolicyForm(root);
    await eventually(() => expect(root.textContent).toContain("提交结果未知"));
    const originalKey = posts[0]!.headers.get("Idempotency-Key");
    const originalBody = await posts[0]!.text();

    identity.switchTo("b");
    await eventually(() => {
      expect(root.querySelector<HTMLInputElement>('[name="minimum_distinct_accounts"]')?.value).toBe("3");
      expect(root.querySelector('[data-action="retry-catalog-policy"]')).toBeNull();
    });
    identity.switchTo("a");
    await eventually(() => {
      expect(root.querySelector<HTMLInputElement>('[name="minimum_distinct_accounts"]')?.value).toBe("4");
      expect(root.querySelector<HTMLInputElement>('[name="minimum_recurring_accounts"]')?.value).toBe("2");
      expect(root.querySelector<HTMLInputElement>('[name="minimum_distinct_countries"]')?.value).toBe("5");
      expect(root.querySelector<HTMLInputElement>('[name="minimum_quantity_unit_accounts"]')?.value).toBe("3");
      expect(root.querySelector<HTMLInputElement>('[name="require_unified_unit"]')?.checked).toBe(true);
      expect(root.querySelector('[data-action="retry-catalog-policy"]')).not.toBeNull();
    });
    (root.querySelector('[data-action="retry-catalog-policy"]') as HTMLButtonElement).click();
    await eventually(() => expect(posts).toHaveLength(2));
    expect(posts[1]!.headers.get("Idempotency-Key")).toBe(originalKey);
    expect(await posts[1]!.text()).toBe(originalBody);
  });

  it("restores and retries the exact non-default same-client intent after remount, then replaces it on form change", async () => {
    const identity = identityHarness("policy-remount");
    const posts: Request[] = [];
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const request = input as Request;
      const path = new URL(request.url).pathname;
      if (path === "/products/catalog-policies" && request.method === "POST") {
        posts.push(request.clone());
        throw new TypeError("controlled uncertainty");
      }
      return catalogPathResponse(path) ?? Response.json({}, { status: 500 });
    });
    const client = createApiClient({ baseUrl: "https://tradeos.test", fetch }, identity.provider);
    const first = await mountProductsInstance(fetch, identity.provider, client);
    await eventually(() => expect(first.root.textContent).toContain("未配置即关闭"));
    setField(first.root, "minimum_distinct_accounts", "4");
    setField(first.root, "minimum_recurring_accounts", "2");
    setField(first.root, "minimum_distinct_countries", "5");
    setField(first.root, "minimum_quantity_unit_accounts", "3");
    const firstUnified = first.root.querySelector<HTMLInputElement>('[name="require_unified_unit"]')!;
    firstUnified.checked = true;
    firstUnified.dispatchEvent(new Event("change", { bubbles: true }));
    await nextTick();
    submitPolicyForm(first.root);
    await eventually(() => expect(first.root.textContent).toContain("提交结果未知"));
    const firstKey = posts[0]!.headers.get("Idempotency-Key");
    const firstBody = await posts[0]!.text();
    mountedApps.splice(mountedApps.indexOf(first.app), 1);
    first.app.unmount();

    const second = await mountProductsInstance(fetch, identity.provider, client);
    await eventually(() => expect(second.root.textContent).toContain("未配置即关闭"));
    expect(second.root.querySelector<HTMLInputElement>('[name="minimum_distinct_accounts"]')?.value).toBe("4");
    expect(second.root.querySelector<HTMLInputElement>('[name="minimum_recurring_accounts"]')?.value).toBe("2");
    expect(second.root.querySelector<HTMLInputElement>('[name="minimum_distinct_countries"]')?.value).toBe("5");
    expect(second.root.querySelector<HTMLInputElement>('[name="minimum_quantity_unit_accounts"]')?.value).toBe("3");
    expect(second.root.querySelector<HTMLInputElement>('[name="require_unified_unit"]')?.checked).toBe(true);
    expect(second.root.querySelector('[data-action="retry-catalog-policy"]')).not.toBeNull();
    (second.root.querySelector('[data-action="retry-catalog-policy"]') as HTMLButtonElement).click();
    await eventually(() => expect(posts).toHaveLength(2));
    expect(posts[1]!.headers.get("Idempotency-Key")).toBe(firstKey);
    expect(await posts[1]!.text()).toBe(firstBody);

    setField(second.root, "minimum_distinct_accounts", "5");
    await nextTick();
    submitPolicyForm(second.root);
    await eventually(() => expect(posts).toHaveLength(3));
    expect(posts[2]!.headers.get("Idempotency-Key")).not.toBe(firstKey);
    expect(await posts[2]!.json()).toMatchObject({
      minimum_distinct_accounts: 5,
      minimum_distinct_countries: 5,
      minimum_quantity_unit_accounts: 3,
      minimum_recurring_accounts: 2,
      require_unified_unit: true,
    });
    expect(second.root.textContent).not.toContain(posts[2]!.headers.get("Idempotency-Key")!);
  });

  it("isolates retained policy intent by the exact injected API client", async () => {
    const identity = identityHarness("policy-client-isolation");
    const posts: Request[] = [];
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const request = input as Request;
      const path = new URL(request.url).pathname;
      if (path === "/products/catalog-policies" && request.method === "POST") {
        posts.push(request.clone());
        throw new TypeError("controlled uncertainty");
      }
      return catalogPathResponse(path) ?? Response.json({}, { status: 500 });
    });
    const firstClient = createApiClient({ baseUrl: "https://tradeos.test", fetch }, identity.provider);
    const first = await mountProductsInstance(fetch, identity.provider, firstClient);
    await eventually(() => expect(first.root.querySelector(".policy-form")).not.toBeNull());
    setField(first.root, "minimum_distinct_accounts", "4");
    submitPolicyForm(first.root);
    await eventually(() => expect(first.root.textContent).toContain("提交结果未知"));
    const firstKey = posts[0]!.headers.get("Idempotency-Key");
    mountedApps.splice(mountedApps.indexOf(first.app), 1);
    first.app.unmount();

    const secondClient = createApiClient({ baseUrl: "https://tradeos.test", fetch }, identity.provider);
    const second = await mountProductsInstance(fetch, identity.provider, secondClient);
    await eventually(() => expect(second.root.querySelector(".policy-form")).not.toBeNull());
    setField(second.root, "minimum_distinct_accounts", "4");
    submitPolicyForm(second.root);
    await eventually(() => expect(posts).toHaveLength(2));
    expect(posts[1]!.headers.get("Idempotency-Key")).not.toBe(firstKey);
  });

  it.each([
    [403, "当前身份无权提交目录策略"],
    [404, "目录策略依赖记录不存在或不属于当前租户"],
    [409, "策略基线或请求内容已冲突，请核对当前版本"],
    [422, "策略内容无效，请检查所有门槛"],
  ])("retains the exact policy intent after HTTP %i for an unchanged ordinary resubmit", async (status, errorMessage) => {
    const identity = identityHarness(`policy-http-${status}`);
    const posts: Request[] = [];
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const request = input as Request;
      const path = new URL(request.url).pathname;
      if (path === "/products/catalog-policies" && request.method === "POST") {
        posts.push(request.clone());
        return Response.json({}, { status });
      }
      return catalogPathResponse(path) ?? Response.json({}, { status: 500 });
    });
    const root = await mountProducts(fetch, identity.provider);
    await eventually(() => expect(root.querySelector(".policy-form")).not.toBeNull());
    setField(root, "minimum_distinct_accounts", "4");
    setField(root, "minimum_recurring_accounts", "2");
    setField(root, "minimum_distinct_countries", "5");
    setField(root, "minimum_quantity_unit_accounts", "3");
    const unified = root.querySelector<HTMLInputElement>('[name="require_unified_unit"]')!;
    unified.checked = true;
    unified.dispatchEvent(new Event("change", { bubbles: true }));
    await nextTick();
    submitPolicyForm(root);
    await eventually(() => expect(posts).toHaveLength(1));
    const originalKey = posts[0]!.headers.get("Idempotency-Key");
    const originalBody = await posts[0]!.text();
    await eventually(() => expect(root.textContent).toContain(errorMessage));
    expect(root.querySelector('[data-action="retry-catalog-policy"]')).toBeNull();

    submitPolicyForm(root);
    await eventually(() => expect(posts).toHaveLength(2));
    expect(posts[1]!.headers.get("Idempotency-Key")).toBe(originalKey);
    expect(await posts[1]!.text()).toBe(originalBody);
  });

  it("bounds each client registry to eight identities with deterministic oldest-entry eviction", async () => {
    const identity = identityHarness("policy-registry-bound");
    const posts: Request[] = [];
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const request = input as Request;
      const path = new URL(request.url).pathname;
      if (path === "/products/catalog-policies" && request.method === "POST") {
        posts.push(request.clone());
        throw new TypeError("controlled uncertainty");
      }
      return catalogPathResponse(path) ?? Response.json({}, { status: 500 });
    });
    const client = createApiClient({ baseUrl: "https://tradeos.test", fetch }, identity.provider);
    const root = (await mountProductsInstance(fetch, identity.provider, client)).root;
    const keys = new Map<string, string | null>();
    for (const suffix of ["a", "b", "c", "d", "e", "f", "g", "h", "i"]) {
      if (suffix !== "a") identity.switchTo(suffix);
      await eventually(() => expect(root.querySelector(".policy-form")).not.toBeNull());
      submitPolicyForm(root);
      await eventually(() => expect(posts).toHaveLength(keys.size + 1));
      keys.set(suffix, posts.at(-1)!.headers.get("Idempotency-Key"));
      await eventually(() => expect(root.textContent).toContain("提交结果未知"));
    }

    identity.switchTo("b");
    await eventually(() => expect(root.querySelector(".policy-form")).not.toBeNull());
    submitPolicyForm(root);
    await eventually(() => expect(posts).toHaveLength(10));
    expect(posts.at(-1)!.headers.get("Idempotency-Key")).toBe(keys.get("b"));

    identity.switchTo("a");
    await eventually(() => expect(root.querySelector(".policy-form")).not.toBeNull());
    submitPolicyForm(root);
    await eventually(() => expect(posts).toHaveLength(11));
    expect(posts.at(-1)!.headers.get("Idempotency-Key")).not.toBe(keys.get("a"));
  });

  it("mirrors every catalog policy integer and cross-field bound before POST and normalizes cleared optionals", async () => {
    const posts: Request[] = [];
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const request = input as Request;
      const path = new URL(request.url).pathname;
      if (path === "/products/catalog-policies" && request.method === "POST") {
        posts.push(request.clone());
        return Response.json(policyView("cpv_valid", "pending_approval", "pending"), { status: 202 });
      }
      return catalogPathResponse(path) ?? Response.json({}, { status: 500 });
    });
    const root = await mountProducts(fetch);
    await eventually(() => expect(root.textContent).toContain("未配置即关闭"));
    const invalid: Array<[string, string, string?, string?]> = [
      ["minimum_distinct_accounts", "1"],
      ["minimum_distinct_accounts", "2147483648"],
      ["minimum_recurring_accounts", "0"],
      ["minimum_recurring_accounts", "4"],
      ["minimum_recurring_accounts", "2147483648"],
      ["minimum_distinct_countries", "1"],
      ["minimum_distinct_countries", "2147483648"],
      ["minimum_quantity_unit_accounts", "0"],
      ["minimum_quantity_unit_accounts", "4"],
      ["minimum_quantity_unit_accounts", "2147483648"],
    ];
    for (const [name, value] of invalid) {
      setField(root, "minimum_distinct_accounts", "3");
      setField(root, "minimum_recurring_accounts", "");
      setField(root, "minimum_distinct_countries", "");
      setField(root, "minimum_quantity_unit_accounts", "");
      const unified = root.querySelector<HTMLInputElement>('[name="require_unified_unit"]')!;
      unified.checked = false;
      unified.dispatchEvent(new Event("change", { bubbles: true }));
      setField(root, name, value);
      await nextTick();
      submitPolicyForm(root);
      await nextTick();
      expect(posts, `${name}=${value}`).toHaveLength(0);
    }
    const unified = root.querySelector<HTMLInputElement>('[name="require_unified_unit"]')!;
    unified.checked = true;
    unified.dispatchEvent(new Event("change", { bubbles: true }));
    await nextTick();
    submitPolicyForm(root);
    await nextTick();
    expect(posts).toHaveLength(0);

    unified.checked = false;
    unified.dispatchEvent(new Event("change", { bubbles: true }));
    setField(root, "minimum_distinct_countries", "4");
    setField(root, "minimum_recurring_accounts", "2");
    setField(root, "minimum_quantity_unit_accounts", "2");
    setField(root, "minimum_recurring_accounts", "");
    setField(root, "minimum_quantity_unit_accounts", "");
    await nextTick();
    submitPolicyForm(root);
    await eventually(() => expect(posts).toHaveLength(1));
    expect(await posts[0]!.json()).toEqual({
      minimum_distinct_accounts: 3,
      minimum_distinct_countries: 4,
      minimum_quantity_unit_accounts: null,
      minimum_recurring_accounts: null,
      require_unified_unit: false,
    });
    for (const name of ["minimum_distinct_accounts", "minimum_recurring_accounts", "minimum_distinct_countries", "minimum_quantity_unit_accounts"]) {
      expect(root.querySelector<HTMLInputElement>(`[name="${name}"]`)?.max).toBe("2147483647");
    }
  });

  it("opens the exact linked terminal approval even when absent from pending, and follows exact query changes", async () => {
    const requestedDetails: string[] = [];
    const target = approvalView("apr_cpv_terminal", "精确终态目录审批");
    const second = approvalView("apr_cpv_second", "第二个精确目录审批", "rejected");
    const other = approvalView("apr_other_pending", "待办中的其他审批", "pending");
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const path = new URL((input as Request).url).pathname;
      if (path === "/products/catalog-policies") return Response.json([policyView("cpv_terminal", "rejected", "rejected")]);
      if (path === "/approvals/pending") return Response.json([other]);
      if (path.startsWith("/approvals/")) {
        requestedDetails.push(path.slice("/approvals/".length));
        if (path === "/approvals/apr_cpv_terminal") return Response.json(target);
        if (path === "/approvals/apr_cpv_second") return Response.json(second);
      }
      return catalogPathResponse(path) ?? Response.json({}, { status: 404 });
    });
    const root = await mountProducts(fetch);
    await eventually(() => expect(root.querySelector('a[href="/approvals?approval_id=apr_cpv_terminal"]')).not.toBeNull());
    (root.querySelector('a[href="/approvals?approval_id=apr_cpv_terminal"]') as HTMLAnchorElement).click();
    await eventually(() => expect(root.textContent).toContain("精确终态目录审批"));
    expect(root.querySelector(".approval-packet h2")?.textContent).toBe("精确终态目录审批");
    expect(root.querySelector(".approval-list")?.textContent).toContain("待办中的其他审批");
    expect(requestedDetails).toContain("apr_cpv_terminal");

    await router.replace({ path: "/approvals", query: { approval_id: "apr_cpv_second" } });
    await eventually(() => expect(root.textContent).toContain("第二个精确目录审批"));
    expect(requestedDetails).toContain("apr_cpv_second");
  });

  it("uses a cultivation column minimum that cannot overflow a 320px viewport", async () => {
    const root = await mountProducts(async (input) => {
      const path = new URL((input as Request).url).pathname;
      if (path === "/products/catalog-cultivation-cases") return Response.json([cultivationCase("ccs_mobile")]);
      return catalogPathResponse(path) ?? Response.json({}, { status: 500 });
    });
    await eventually(() => expect(root.querySelector(".cultivation-grid")).not.toBeNull());
    expect(root.querySelector<HTMLElement>(".cultivation-grid")?.style.gridTemplateColumns)
      .toBe("repeat(auto-fit, minmax(min(290px, 100%), 1fr))");
  });
});
