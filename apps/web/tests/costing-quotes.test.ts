import { createApp, nextTick, type App as VueApp } from "vue";
import { describe, expect, it, vi } from "vitest";

import { createApiClient } from "../src/api/client";
import App from "../src/App.vue";
import router from "../src/router";

const opportunityId = "opp_01M0PWRX23T9DP9ENM9PW5GFC8";
const costSheetId = "cost_01M0PWSK0GJTR12HTQC29BSDBG";

const sheet = {
  base_currency: "USD",
  cost_sheet_id: costSheetId,
  created_at: "2026-08-23T14:00:00Z",
  fx_rates: [],
  fx_snapshot_id: "fx-manual-one",
  has_indicative_items: false,
  is_locked: false,
  items: [{
    amount: { amount: "12.50", currency: "USD" },
    entered_by_id: "emp_01M0PWRX23T9DP9ENM9PW5GFC8",
    entered_by_name: null,
    is_pending_confirmation: false,
    is_per_unit: true,
    item_label: "产品采购",
    item_type: "product_purchase",
    note: "100 件阶梯价",
    price_basis: "quoted",
    source_ref: "supplier-quote-artifact-1",
  }],
  margin_rate: null,
  minimum_sellable_price: null,
  opportunity_id: opportunityId,
  quantity: 100,
  quote_currency: "EUR",
  risk_accepted_by: null,
  unit_full_cost: { amount: "12.50", currency: "USD" },
  version_number: 1,
  version_type: "quoted",
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function asRequest(input: URL | RequestInfo): Request {
  if (input instanceof Request) return input;
  throw new TypeError("fake transport requires a Request");
}

async function eventually(assertion: () => void): Promise<void> {
  let latestError: unknown;
  for (let attempt = 0; attempt < 80; attempt += 1) {
    await nextTick();
    await new Promise((resolve) => setTimeout(resolve, 0));
    try {
      assertion();
      return;
    } catch (error) {
      latestError = error;
    }
  }
  throw latestError;
}

async function mountPage(fetch: typeof globalThis.fetch): Promise<{
  app: VueApp;
  root: HTMLElement;
}> {
  const root = document.createElement("div");
  document.body.replaceChildren(root);
  const app = createApp(App);
  app.provide(
    "tradeos-api-client",
    createApiClient({ baseUrl: "https://tradeos.test", fetch }),
  );
  app.use(router);
  app.mount(root);
  await router.replace("/costing-quotes");
  await new Promise((resolve) => setTimeout(resolve, 0));
  return { app, root };
}

function input(root: HTMLElement, selector: string, value: string): void {
  const element = root.querySelector<HTMLInputElement>(selector);
  expect(element).not.toBeNull();
  if (!element) return;
  element.value = value;
  element.dispatchEvent(new Event("input"));
}

function click(root: HTMLElement, label: string): void {
  const button = [...root.querySelectorAll("button")].find((candidate) =>
    candidate.textContent?.includes(label),
  );
  expect(button).toBeTruthy();
  (button as HTMLButtonElement).click();
}

describe("CostingQuotes", () => {
  it("loads versions, records sourced Decimal cost and shows read-only blockers", async () => {
    const itemBodies: unknown[] = [];
    const fetch = vi.fn<typeof globalThis.fetch>(async (rawInput) => {
      const request = asRequest(rawInput);
      const url = new URL(request.url);
      if (
        request.method === "GET"
        && url.pathname === `/costing-quotes/opportunities/${opportunityId}/cost-sheets`
      ) return jsonResponse([sheet]);
      if (
        request.method === "POST"
        && url.pathname === `/costing-quotes/cost-sheets/${costSheetId}/items`
      ) {
        itemBodies.push(await request.json());
        return new Response(null, { status: 204 });
      }
      if (
        request.method === "POST"
        && url.pathname === `/costing-quotes/cost-sheets/${costSheetId}/readiness`
      ) {
        return jsonResponse({
          blockers: ["成本表缺少业务场景要求的成本项"],
          indicative_items: [],
          missing_items: ["packaging"],
          ready: false,
        });
      }
      return jsonResponse({ code: "unexpected", message: url.pathname }, 500);
    });
    const { app, root } = await mountPage(fetch);

    input(root, '[name="opportunity-id"]', opportunityId);
    click(root, "读取成本版本");
    await eventually(() => {
      expect(root.textContent).toContain("产品采购");
      expect(root.textContent).toContain("supplier-quote-artifact-1");
    });

    input(root, '[name="item-amount"]', "7.25");
    input(root, '[name="item-source"]', "supplier-quote-artifact-2");
    click(root, "保存成本项");
    await eventually(() => {
      expect(itemBodies).toEqual([{
        amount: "7.25",
        currency: "USD",
        is_per_unit: true,
        item_type: "product_purchase",
        note: null,
        price_basis: "quoted",
        source_ref: "supplier-quote-artifact-2",
      }]);
      expect(root.textContent).toContain("成本项已保存");
    });

    input(root, '[name="expected-items"]', "product_purchase, packaging");
    click(root, "检查报价就绪");
    await eventually(() => {
      expect(root.textContent).toContain("packaging");
      expect(root.textContent).toContain("只读检查，不会锁定成本表");
    });
    expect(
      [...root.querySelectorAll("button")].some((button) =>
        button.textContent?.includes("接受参考价风险"),
      ),
    ).toBe(false);
    app.unmount();
  });
});
