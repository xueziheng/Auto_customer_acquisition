import { createApp, nextTick, type App as VueApp } from "vue";
import { describe, expect, it, vi } from "vitest";

import type { components } from "../src/api/api";
import { createApiClient } from "../src/api/client";
import App from "../src/App.vue";
import router from "../src/router";

type RunDetail = components["schemas"]["RunDetailView"];
type RunSummary = components["schemas"]["RunSummaryView"];

const firstRun: RunSummary = {
  created_at: "2026-08-23T08:00:00Z",
  current_step: "discover",
  last_activity_at: "2026-08-23T08:05:00Z",
  last_error: null,
  next_poll_at: null,
  retry_count: 0,
  run_id: "run_01K39P9M5D6K4A91YEQ80EJZ0X",
  status: "running",
  subject_ref: "schedule:daily",
  workflow_type: "demand_discovery",
  workflow_version: 1,
};

const secondRun: RunSummary = {
  created_at: "2026-08-23T07:00:00Z",
  current_step: "handoff",
  last_activity_at: "2026-08-23T07:08:00Z",
  last_error: "RuntimeError",
  next_poll_at: null,
  retry_count: 1,
  run_id: "run_01K39P9M5D6K4A91YEQ80EJZ0Y",
  status: "failed",
  subject_ref: "opp_01K39P9M5D6K4A91YEQ80EJZ0X",
  workflow_type: "human_handoff",
  workflow_version: 1,
};

const firstDetail: RunDetail = {
  approvals: [{
    approval_id: "apr_01K39P9M5D6K4A91YEQ80EJZ0X",
    approval_type: "campaign_activation",
    created_at: "2026-08-23T08:01:00Z",
    decided_at: "2026-08-23T08:02:00Z",
    expires_at: "2026-08-24T08:01:00Z",
    state: "approved",
  }],
  artifacts: [{
    artifact_id: "art_01K39P9M5D6K4A91YEQ80EJZ0X",
    generated_at: "2026-08-23T08:03:00Z",
    generated_by: "outreach-agent",
    kind: "email_draft",
    mime_type: "application/vnd.tradeos.email-draft+json",
    subject_ref: "enr_01K39P9M5D6K4A91YEQ80EJZ0X",
  }],
  steps: [{
    attempt: 1,
    created_at: "2026-08-23T08:00:00Z",
    due_at: "2026-08-23T08:00:00Z",
    error: null,
    status: "completed",
    step_id: "wfs_01K39P9M5D6K4A91YEQ80EJZ0X",
    step_name: "discover",
    updated_at: "2026-08-23T08:05:00Z",
  }],
  summary: firstRun,
  tool_calls: [{
    attempt_count: 1,
    completed_at: "2026-08-23T08:04:00Z",
    cost_class: "low",
    created_at: "2026-08-23T08:03:00Z",
    error_category: null,
    risk_level: "low",
    status: "succeeded",
    tool_call_id: "tc_01K39P9M5D6K4A91YEQ80EJZ0X",
    tool_id: "web.search",
    tool_version: "1",
  }],
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

async function mountRuns(fetch: typeof globalThis.fetch): Promise<{
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
  await router.replace("/runs");
  app.mount(root);
  await new Promise((resolve) => setTimeout(resolve, 0));
  return { app, root };
}

describe("RunCenter", () => {
  it("renders the persisted run evidence chain and loads another run on selection", async () => {
    const requested: string[] = [];
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      if (!(input instanceof Request)) throw new TypeError("Request required");
      const url = new URL(input.url);
      requested.push(`${url.pathname}${url.search}`);
      if (url.pathname === "/runs") return jsonResponse([firstRun, secondRun]);
      if (url.pathname === `/runs/${firstRun.run_id}`) {
        return jsonResponse(firstDetail);
      }
      if (url.pathname === `/runs/${secondRun.run_id}`) {
        return jsonResponse({
          approvals: [],
          artifacts: [],
          steps: [],
          summary: secondRun,
          tool_calls: [],
        } satisfies RunDetail);
      }
      return jsonResponse({ code: "unexpected", message: "unexpected" }, 500);
    });

    const { root } = await mountRuns(fetch);

    await eventually(() => {
      expect(root.textContent).toContain("demand_discovery");
      expect(root.textContent).toContain("discover");
      expect(root.textContent).toContain("web.search");
      expect(root.textContent).toContain("email_draft");
      expect(root.textContent).toContain("campaign_activation");
    });
    expect(root.textContent).not.toContain("接口尚未装配");
    expect(root.textContent).not.toContain("must-not-leak");

    const secondButton = [...root.querySelectorAll<HTMLButtonElement>("button")]
      .find((button) => button.textContent?.includes("human_handoff"));
    expect(secondButton).toBeTruthy();
    secondButton!.click();

    await eventually(() => {
      expect(root.textContent).toContain("RuntimeError");
      expect(requested).toContain(`/runs/${secondRun.run_id}`);
    });
  });

  it("shows a truthful boss-only message when access is rejected", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>(async () =>
      jsonResponse({ code: "forbidden", message: "denied" }, 403),
    );

    const { root } = await mountRuns(fetch);

    await eventually(() => {
      expect(root.textContent).toContain("只有老板可以查看 Run 审计记录");
    });
  });
});
