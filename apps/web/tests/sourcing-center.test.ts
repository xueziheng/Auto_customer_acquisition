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
});
