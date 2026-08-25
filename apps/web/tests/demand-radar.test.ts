import { createApp, nextTick } from "vue";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { components } from "../src/api/api";
import { createApiClient } from "../src/api/client";
import App from "../src/App.vue";
import router from "../src/router";

type Signal = components["schemas"]["DemandSignalView"];
type Hypothesis = components["schemas"]["HypothesisView"];

const signal: Signal = {
  entity_name: "Northwind Hardware",
  is_inference: false,
  observed_at: "2026-08-21T10:00:00Z",
  page_hash: null,
  possible_need: "可能需要补充工业铰链供应",
  raw_observation: "公开扩建公告已发布",
  signal_id: "sig_01K39P9M5D6K4A91YEQ80EJZ0X",
  signal_type: "company_expansion",
  source_ref: "artifact:public-page-1",
  source_type: "web_page",
  source_url: "https://example.test/news/expansion",
  snapshot_artifact_ref: null,
  status: "captured",
};

const hypothesis: Hypothesis = {
  account_id: "acc_01K39P9M5D6K4A91YEQ80EJZ0Y",
  account_name: signal.entity_name,
  category: "industrial_hinges",
  confidence_explanation: "包含一条公开企业变化证据",
  confidence_tier: "medium",
  created_at: "2026-08-21T10:01:00Z",
  evidence: [{
    level: "company_change",
    observed_at: signal.observed_at,
    source_ref: signal.source_ref,
    source_url: signal.source_url,
    summary: signal.raw_observation,
  }],
  hypothesis_id: "hyp_01K39P9M5D6K4A91YEQ80EJZ0Z",
  is_inference: true,
  reasoning: "扩建可能带来新的门控五金采购需求",
  status: "active",
};

function response(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

async function eventually(assertion: () => void): Promise<void> {
  let error: unknown;
  for (let attempt = 0; attempt < 80; attempt += 1) {
    await nextTick();
    await new Promise((resolve) => setTimeout(resolve, 0));
    try {
      assertion();
      return;
    } catch (caught) {
      error = caught;
    }
  }
  throw error;
}

afterEach(() => {
  router.replace("/");
});

describe("DemandRadar", () => {
  it("keeps source facts visually separate from inferential hypotheses and shows only a discrete tier", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      if (!(input instanceof Request)) throw new TypeError("Request required");
      const path = new URL(input.url).pathname;
      if (path === "/demand/signals") return response([signal]);
      if (path === "/demand/hypotheses") return response([hypothesis]);
      if (path === "/demand/needs" || path === "/demand/clusters" || path === "/notifications") return response([]);
      return response([]);
    });
    const root = document.createElement("div");
    document.body.replaceChildren(root);
    const app = createApp(App);
    app.provide("tradeos-api-client", createApiClient({ baseUrl: "https://tradeos.test", fetch }));
    app.use(router);
    app.mount(root);
    await router.replace("/demand");

    await eventually(() => expect(root.textContent).toContain(signal.raw_observation));
    expect(root.textContent).toContain(signal.signal_id);
    expect(root.textContent).toContain("事实：公开来源中的原始观察");
    expect(root.textContent).toContain("查看原始来源");

    const hypothesisTab = [...root.querySelectorAll("button")].find((button) =>
      button.textContent?.includes("需求假设"),
    );
    expect(hypothesisTab).toBeTruthy();
    (hypothesisTab as HTMLButtonElement).click();
    await nextTick();

    const hypothesisCard = root.querySelector(".hypothesis-card");
    expect(hypothesisCard?.textContent).toContain("推断");
    expect(hypothesisCard?.textContent).toContain(hypothesis.hypothesis_id);
    expect(hypothesisCard?.textContent).toContain(hypothesis.account_id);
    expect(hypothesisCard?.textContent).toContain("置信档位：中档");
    expect(hypothesisCard?.textContent).toContain(signal.source_ref);
    expect(hypothesisCard?.textContent).not.toMatch(/\b0(?:\.\d+)?\b|\b1\.0+\b|\d+%/);
    app.unmount();
  });
});
