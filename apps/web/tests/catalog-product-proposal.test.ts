import { createApp, nextTick, type App as VueApp } from "vue";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "../src/App.vue";
import { createApiClient } from "../src/api/client";
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

async function mountProducts(fetch: typeof globalThis.fetch): Promise<HTMLElement> {
  const root = document.createElement("div");
  document.body.replaceChildren(root);
  const app = createApp(App);
  app.provide(
    "tradeos-api-client",
    createApiClient({ baseUrl: "https://tradeos.test", fetch }),
  );
  app.use(router);
  await router.replace("/products");
  app.mount(root);
  mountedApps.push(app);
  return root;
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
});
