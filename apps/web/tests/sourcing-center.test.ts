import { createApp, nextTick, type App as VueApp } from "vue";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "../src/App.vue";
import { createApiClient } from "../src/api/client";
import router from "../src/router";

const caseId = "src_01K39P9M5D6K4A91YEQ80EJZ0X";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

async function eventually(assertion: () => void): Promise<void> {
  let latest: unknown;
  for (let attempt = 0; attempt < 80; attempt += 1) {
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

const mountedApps: VueApp[] = [];
afterEach(() => {
  mountedApps.splice(0).forEach((app) => app.unmount());
  void router.replace("/");
});

async function mount(path: string, fetch: typeof globalThis.fetch): Promise<HTMLElement> {
  const root = document.createElement("div");
  document.body.replaceChildren(root);
  const app = createApp(App);
  app.provide("tradeos-api-client", createApiClient({ baseUrl: "https://tradeos.test", fetch }));
  app.use(router);
  await router.replace(path);
  app.mount(root);
  mountedApps.push(app);
  return root;
}

describe("Sourcing and Product centers", () => {
  it("renders source_only supply as indicative-only without a customer quote or contact action", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const path = new URL((input as Request).url).pathname;
      if (path === "/notifications") return jsonResponse([]);
      if (path === "/products") return jsonResponse([{
        candidate_status: "source_only",
        category: "hinges",
        lead_time_display: null,
        moq: 500,
        name_en: "Stainless hinge",
        name_zh: "不锈钢铰链",
        pool: "candidate",
        product_id: "prd_01K39P9M5D6K4A91YEQ80EJZ0X",
        quote_warning: "不可用于客户报价",
        source_only: true,
        spec_summary: "304 stainless steel",
        source: {
          evidence_refs: ["art_01K39P9M5D6K4A91YEQ80EJZ0X"],
          indicative_prices: [{ currency: "USD", evidence_ref: "art_01K39P9M5D6K4A91YEQ80EJZ0X", minimum_quantity: 500, unit: "piece", unit_amount: "1.25" }],
          price_basis: "indicative",
          sourcing_case_id: caseId,
          supplier_candidate_id: "sup_01K39P9M5D6K4A91YEQ80EJZ0X",
        },
      }]);
      return jsonResponse({ code: "unexpected", message: "unexpected" }, 500);
    });

    const root = await mount("/products", fetch);
    await eventually(() => {
      expect(root.textContent).toContain("不锈钢铰链");
      expect(root.textContent).toContain("不可用于客户报价");
      expect(root.textContent).toContain("公开页面参考价（indicative）");
      expect(root.textContent).toContain("1.25 USD / piece");
    });
    expect([...root.querySelectorAll("button")].map((button) => button.textContent)).not.toContain("创建正式报价");
    expect([...root.querySelectorAll("button")].map((button) => button.textContent)).not.toContain("联系供应商");
  });

  it("renders a case with separated fact, claim, inference and unknown sections", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const path = new URL((input as Request).url).pathname;
      if (path === "/notifications") return jsonResponse([]);
      if (path === `/sourcing-cases/${caseId}`) return jsonResponse({
        case_id: caseId, need_id: "need_01K39P9M5D6K4A91YEQ80EJZ0X", state: "verifying",
        workflow_version: 2, version: 4, opened_at: "2026-08-30T09:00:00Z", state_changed_at: null,
        ladder_checked_to: 5, active_search_plan_id: null, need_snapshot: null, stop: null,
      });
      if (path === `/sourcing-cases/${caseId}/candidates`) return jsonResponse([{
        candidate_id: "sup_01K39P9M5D6K4A91YEQ80EJZ0X", currency: "USD",
        evidence: [{ artifact_id: "art_01K39P9M5D6K4A91YEQ80EJZ0X", canonical_url: "https://example.test/hinge", content_hash: "a".repeat(64), observed_at: "2026-08-30T09:00:00Z" }],
        indicative_price_tiers: [{ amount: "1.25", currency: "USD", evidence_ref: "art_01K39P9M5D6K4A91YEQ80EJZ0X", minimum_quantity: 500, unit: "piece" }],
        match_inferences: { suitability: { based_on: ["art_01K39P9M5D6K4A91YEQ80EJZ0X"], inferred_at: "2026-08-30T09:00:00Z", inferred_by: "emp_01K39P9M5D6K4A91YEQ80EJZ0X", value: "材质可能适配" } },
        moq: 500, observed_facts: { material: { evidence_ref: "art_01K39P9M5D6K4A91YEQ80EJZ0X", provenance: { confirmed_at: null, confirmed_by: null, extracted_at: "2026-08-30T09:00:00Z", extracted_by: "human", page_hash: null, source_id: "art_01K39P9M5D6K4A91YEQ80EJZ0X", source_quote: null, source_type: "web_page", source_url: "https://example.test/hinge" }, value: "304" } },
        price_basis: "indicative", price_unit: "piece", product_title: "Stainless hinge", rejection_reasons: [], source_platform: "web",
        spec_comparisons: [], supplier_claims: { moq: { evidence_ref: "art_01K39P9M5D6K4A91YEQ80EJZ0X", provenance: { confirmed_at: null, confirmed_by: null, extracted_at: "2026-08-30T09:00:00Z", extracted_by: "human", page_hash: null, source_id: "art_01K39P9M5D6K4A91YEQ80EJZ0X", source_quote: null, source_type: "web_page", source_url: "https://example.test/hinge" }, value: 500 } },
        supplier_name: "Example supplier", supply_option: null, verification_missing: ["model"], verification_status: "incomplete",
      }]);
      if (path === `/sourcing-cases/${caseId}/ladder-checks`) return jsonResponse([]);
      if (path === `/sourcing-cases/${caseId}/public-search-plan`) return jsonResponse(null);
      return jsonResponse({ code: "unexpected", message: "unexpected" }, 500);
    });

    const root = await mount(`/sourcing/${caseId}`, fetch);
    await eventually(() => {
      expect(root.textContent).toContain("网页观察事实");
      expect(root.textContent).toContain("供应商自述");
      expect(root.textContent).toContain("匹配推断");
      expect(root.textContent).toContain("未知 / 待核验");
      expect(root.textContent).toContain("公开页面参考价（indicative）");
      expect(root.textContent).toContain("不可用于客户报价");
      expect(root.textContent).toContain("model");
    });
  });

  it("reloads canonical projections through draft, replace, confirm, run, review, boss confirmation and reconciliation", async () => {
    let caseReads = 0;
    let caseVersion = 5;
    let plan: Record<string, unknown> | null = null;
    let review: Record<string, unknown> | null = null;
    let uncertain = [{
      can_current_user_reconcile: true,
      created_at: "2026-08-30T09:00:00Z",
      execution_id: "sxe_01K39P9M5D6K4A91YEQ80EJZ0X",
      reconciliation: null,
      request_key: "b".repeat(64),
      run_id: "run_01K39P9M5D6K4A91YEQ80EJZ0X",
      status: "uncertain",
    }];
    const planBodies: Record<string, unknown>[] = [];
    const postPaths: string[] = [];
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const request = input as Request;
      const path = new URL(request.url).pathname;
      if (path === "/notifications") return jsonResponse([]);
      if (request.method === "POST") {
        postPaths.push(path);
        if (path.endsWith("/public-search-plan")) {
          const body = await request.json() as Record<string, unknown>;
          planBodies.push(body);
          plan = {
            case_id: caseId,
            confirmed_at: null,
            confirmed_by: null,
            created_at: "2026-08-30T09:00:00Z",
            expected_case_version: caseVersion,
            max_pages_read: body.max_pages_read,
            max_search_queries: body.max_search_queries,
            plan_hash: "c".repeat(64),
            plan_id: body.plan_id,
            product_category: body.product_category,
            provider: "tavily",
            queries: body.queries,
            search_depth: "basic",
            status: "pending_confirmation",
            target_countries: body.target_countries,
            usage_credits_remaining: body.usage_credits_remaining,
            version: body.version,
            worst_case_credits: body.worst_case_credits,
          };
          return jsonResponse(plan);
        }
        if (path.endsWith("/public-search-plan/confirm") && plan) {
          caseVersion += 1;
          plan = { ...plan, status: "authorized" };
          return jsonResponse(plan);
        }
        if (path.endsWith("/run") && plan) {
          plan = { ...plan, status: "running" };
          return jsonResponse(plan);
        }
        if (path.endsWith("/review")) {
          if (!review) {
            review = {
              alternate_option_ids: [], can_current_user_confirm: true, case_id: caseId,
              confirmed_at: null, confirmed_by: null, expected_case_version: caseVersion,
              primary_option_id: "sop_01K39P9M5D6K4A91YEQ80EJZ0X", reason: "规格核验完成",
              review_id: "srv_01K39P9M5D6K4A91YEQ80EJZ0X", submitted_at: "2026-08-30T09:00:00Z",
              submitted_by: "emp_01K39P9M5D6K4A91YEQ80EJZ0X",
            };
          } else review = { ...review, can_current_user_confirm: false, confirmed_by: "emp_boss", confirmed_at: "2026-08-30T10:00:00Z" };
          return jsonResponse(review);
        }
        if (path.endsWith("/reconcile-uncertain-request")) {
          uncertain = [];
          return new Response(null, { status: 200 });
        }
      }
      if (path === `/sourcing-cases/${caseId}`) {
        caseReads += 1;
        return jsonResponse({
          active_search_plan_id: plan?.plan_id ?? null, case_id: caseId, ladder_checked_to: 5,
          need_id: "need_01K39P9M5D6K4A91YEQ80EJZ0X", need_snapshot: null, opened_at: "2026-08-30T09:00:00Z",
          state: "candidates_ready", state_changed_at: null, stop: null, version: caseVersion, workflow_version: 2,
        });
      }
      if (path.endsWith("/candidates")) return jsonResponse([{
        candidate_id: "sup_01K39P9M5D6K4A91YEQ80EJZ0X", currency: "USD", evidence: [], indicative_price_tiers: [],
        match_inferences: {}, moq: 500, observed_facts: {}, price_basis: "indicative", price_unit: "piece", product_title: "Stainless hinge",
        rejection_reasons: ["ambiguous_price"], source_platform: "web", spec_comparisons: [{ customer_confirmation: null, level: "different", needs_customer_confirmation: true, offered: "5 inch", required: "4 inch", spec_name: "size", substitutable: false, substitution_impact: "安装孔不兼容" }],
        supplier_claims: {}, supplier_name: "Example supplier", supply_option: { is_qualified: true, option_id: "sop_01K39P9M5D6K4A91YEQ80EJZ0X", product_id: "prd_01K39P9M5D6K4A91YEQ80EJZ0X", source_kind: "supplier_candidate", supplier_candidate_id: "sup_01K39P9M5D6K4A91YEQ80EJZ0X" }, verification_missing: [], verification_status: "qualified",
      }]);
      if (path.endsWith("/ladder-checks")) return jsonResponse([]);
      if (path.endsWith("/public-search-plan")) return jsonResponse(plan);
      if (path.endsWith("/review")) return jsonResponse(review);
      if (path.endsWith("/current-quota")) return jsonResponse({ checked_at: "2026-08-30T09:00:00Z", cost_status: "unknown", paygo_enabled: null, remaining: null, reservations: null });
      if (path.endsWith("/uncertain-reconciliations")) return jsonResponse(uncertain);
      return jsonResponse({ code: "unexpected", message: "unexpected" }, 500);
    });

    const root = await mount(`/sourcing/${caseId}`, fetch);
    await eventually(() => {
      expect(root.textContent).toContain("规格逐项比较");
      expect(root.textContent).toContain("八项核验");
      expect(root.textContent).toContain("拒绝原因：ambiguous_price");
      expect(root.textContent).toContain("当前安全额度");
    });

    const planForm = root.querySelector<HTMLFormElement>(".plan-panel form")!;
    const planInputs = planForm.querySelectorAll<HTMLInputElement>("input");
    planInputs[2]!.value = "CN, DE";
    planInputs[2]!.dispatchEvent(new Event("input", { bubbles: true }));
    planInputs[3]!.value = "hinges";
    planInputs[3]!.dispatchEvent(new Event("input", { bubbles: true }));
    const queryArea = planForm.querySelector<HTMLTextAreaElement>("textarea")!;
    queryArea.value = "CN | stainless hinge\nDE | stainless hinge";
    queryArea.dispatchEvent(new Event("input", { bubbles: true }));
    planInputs[4]!.value = "2";
    planInputs[4]!.dispatchEvent(new Event("input", { bubbles: true }));
    root.querySelector<HTMLFormElement>(".plan-panel form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await eventually(() => expect(root.textContent).toContain("当前计划 v1"));
    root.querySelector<HTMLFormElement>(".plan-panel form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await eventually(() => expect(root.textContent).toContain("当前计划 v2"));
    expect(planBodies).toHaveLength(2);
    expect(planBodies[1]!.version).toBe(2);
    expect(planBodies[0]!.plan_id).not.toBe(planBodies[1]!.plan_id);

    [...root.querySelectorAll<HTMLButtonElement>("button")].find((button) => button.textContent?.includes("确认精确范围"))!.click();
    await eventually(() => expect(root.textContent).toContain("authorized"));
    [...root.querySelectorAll<HTMLButtonElement>("button")].find((button) => button.textContent?.includes("运行公开寻源"))!.click();
    await eventually(() => expect(root.textContent).toContain("running"));

    const reviewPanel = root.querySelector(".review-panel")!;
    const reviewSelect = reviewPanel.querySelector<HTMLSelectElement>("select")!;
    reviewSelect.value = "sop_01K39P9M5D6K4A91YEQ80EJZ0X";
    reviewSelect.dispatchEvent(new Event("change", { bubbles: true }));
    const reviewArea = reviewPanel.querySelector<HTMLTextAreaElement>("textarea")!;
    reviewArea.value = "规格核验完成";
    reviewArea.dispatchEvent(new Event("input", { bubbles: true }));
    reviewPanel.querySelector<HTMLFormElement>("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await eventually(() => expect(root.textContent).toContain("待老板按保存的精确主选"));
    [...root.querySelector(".review-panel")!.querySelectorAll<HTMLButtonElement>("button")].find((button) => button.textContent?.includes("按保存事实确认审核"))!.click();
    await eventually(() => expect(root.textContent).toContain("已由 emp_boss 确认"));

    const recoveryPanel = root.querySelector(".recovery-panel")!;
    const recoveryInput = recoveryPanel.querySelector<HTMLInputElement>("input")!;
    recoveryInput.value = "art_01K39P9M5D6K4A91YEQ80EJZ0X";
    recoveryInput.dispatchEvent(new Event("input", { bubbles: true }));
    const recoveryArea = recoveryPanel.querySelector<HTMLTextAreaElement>("textarea")!;
    recoveryArea.value = "人工核对确认已经扣除一次免费额度";
    recoveryArea.dispatchEvent(new Event("input", { bubbles: true }));
    recoveryPanel.querySelector<HTMLFormElement>("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await eventually(() => expect(root.textContent).toContain("没有可展示的不确定搜索请求"));

    expect(postPaths).toEqual(expect.arrayContaining([
      `/sourcing-cases/${caseId}/public-search-plan`,
      `/sourcing-cases/${caseId}/public-search-plan/confirm`,
      `/sourcing-cases/${caseId}/run`,
      `/sourcing-cases/${caseId}/review`,
      `/sourcing-cases/${caseId}/reconcile-uncertain-request`,
    ]));
    expect(caseReads).toBeGreaterThanOrEqual(8);
  });
});
