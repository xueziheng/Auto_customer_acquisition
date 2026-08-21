import { createApp, nextTick } from "vue";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { components } from "../src/api/api";
import { createApiClient } from "../src/api/client";
import App from "../src/App.vue";
import router from "../src/router";

type ValidatedNeed = components["schemas"]["ValidatedNeedView"];

const needId = "need_01K39P9M5D6K4A91YEQ80EJZ0X";
const need: ValidatedNeed = {
  account_id: "acc_01K39P9M5D6K4A91YEQ80EJZ0Y",
  account_name: "Northwind Hardware",
  completeness: 4,
  created_at: "2026-08-21T10:00:00Z",
  destination: "Rotterdam",
  fields: [
    {
      confirmed_by: "emp-reviewer",
      name: "quantity",
      source_quote: "We need around 20,000 pieces for our September shipment.",
      source_ref: "msg_01K39P9M5D6K4A91YEQ80EJZ0Z",
      value: "20000",
    },
    {
      confirmed_by: null,
      name: "destination",
      source_quote: "Please deliver to Rotterdam.",
      source_ref: "artifact:reply-rotterdam",
      value: "Rotterdam",
    },
  ],
  missing_for_sourcing: ["specification"],
  need_id: needId,
  product_category: "hinges",
  quantity: 20000,
  required_by: "2026-09-01",
  status: "active",
  target_price: { amount: "1.25", currency: "USD" },
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
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

afterEach(() => {
  router.replace("/");
});

describe("ValidatedNeedDetail", () => {
  it("renders customer quotes and field-level provenance from the detail API", async () => {
    const requestedUrls: string[] = [];
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      if (!(input instanceof Request)) throw new TypeError("Request required");
      const url = new URL(input.url);
      requestedUrls.push(url.toString());
      if (url.pathname === `/demand/needs/${needId}`) return jsonResponse(need);
      if (url.pathname === "/notifications") return jsonResponse([]);
      return jsonResponse({ code: "unexpected", message: "unexpected" }, 500);
    });
    const root = document.createElement("div");
    document.body.replaceChildren(root);
    const app = createApp(App);
    app.provide(
      "tradeos-api-client",
      createApiClient({ baseUrl: "https://tradeos.test", fetch }),
    );
    app.use(router);
    app.mount(root);

    await router.replace(`/demand/needs/${needId}`);
    await eventually(() => {
      expect(root.textContent).toContain("Northwind Hardware");
      expect(root.textContent).toContain("We need around 20,000 pieces");
      expect(root.textContent).toContain("emp-reviewer");
      expect(root.textContent).toContain("artifact:reply-rotterdam");
      expect(root.textContent).toContain("寻源前仍缺：规格");
    });
    expect(root.querySelector("[v-html]")).toBeNull();
    expect(requestedUrls).toContain(`https://tradeos.test/demand/needs/${needId}`);
    app.unmount();
  });
});
