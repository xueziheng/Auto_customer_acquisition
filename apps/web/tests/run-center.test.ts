import { createApp, nextTick, type App as VueApp } from "vue";
import { afterEach, describe, expect, it, vi } from "vitest";

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

const mountedApps: VueApp[] = [];
afterEach(() => { mountedApps.splice(0).forEach((app) => app.unmount()); });

async function mountRuns(fetch: typeof globalThis.fetch, path = "/runs"): Promise<{
  app: VueApp;
  root: HTMLElement;
}> {
  const root = document.createElement("div");
  document.body.replaceChildren(root);
  const app = createApp(App);
  app.provide(
    "tradeos-api-client",
    createApiClient({
      baseUrl: "https://tradeos.test",
      fetch: async (input) => {
        if (input instanceof Request && new URL(input.url).pathname === "/notifications") {
          return jsonResponse([]);
        }
        return fetch(input);
      },
    }),
  );
  await router.replace(path);
  app.use(router);
  app.mount(root);
  mountedApps.push(app);
  await new Promise((resolve) => setTimeout(resolve, 0));
  return { app, root };
}

function emptyDetail(summary: RunSummary): RunDetail {
  return { approvals: [], artifacts: [], steps: [], summary, tool_calls: [] };
}

function deferredResponse() {
  let resolve!: (response: Response) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<Response>((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

async function settle(): Promise<void> {
  for (let i = 0; i < 10; i += 1) {
    await nextTick();
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
}

describe("RunCenter", () => {
  it("renders only the safe sourcing Run summary without query, page or quote payloads", async () => {
    const sourcingDetail: RunDetail = {
      ...firstDetail,
      summary: {
        ...firstRun,
        sourcing: {
          alternate_count: 1,
          candidate_count: 2,
          case_id: "src_01K39P9M5D6K4A91YEQ80EJZ0X",
          consumed_credits: 1,
          ladder: [{ outcome: "no_qualified_supply", rung: 1 }],
          page_attempt_count: 3,
          plan_status: "running",
          primary_count: 1,
          reserved_credits: 1,
          search_attempt_count: 2,
          stop_reason: null,
          uncertain_credits: 0,
        },
      },
    };
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const path = new URL((input as Request).url).pathname;
      if (path === "/runs") return jsonResponse([firstRun]);
      if (path === `/runs/${firstRun.run_id}`) return jsonResponse(sourcingDetail);
      return jsonResponse({ code: "unexpected", message: "unexpected" }, 500);
    });

    const { root } = await mountRuns(fetch, `/runs?run=${firstRun.run_id}`);

    await eventually(() => {
      expect(root.textContent).toContain("寻源运行摘要");
      expect(root.textContent).toContain("已预留 1 / 已消耗 1 / 不确定 0");
      expect(root.textContent).toContain("第 1 级：no_qualified_supply");
    });
    expect(root.textContent).not.toContain("stainless hinge query");
  });

  it.each([0, 50])("精确读取不在最近 %s 条列表中的深链 Run，刷新仍保留目标", async (count) => {
    const requested: string[] = [];
    const olderRun = { ...secondRun, run_id: "run_older", subject_ref: "dpr_older" };
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const path = new URL((input as Request).url).pathname;
      requested.push(path);
      if (path === "/runs") return jsonResponse(Array.from({ length: count }, (_, index) => ({
        ...firstRun, run_id: `run_recent_${index}`,
      })));
      if (path === "/runs/run_older") return jsonResponse(emptyDetail(olderRun));
      return jsonResponse(firstDetail);
    });

    const { root } = await mountRuns(fetch, "/runs?run=run_older");
    await eventually(() => {
      expect(root.querySelector(".run-detail")?.textContent).toContain("dpr_older");
    });
    [...root.querySelectorAll<HTMLButtonElement>("button")]
      .find((button) => button.textContent?.includes("刷新记录"))!.click();
    await eventually(() => { expect(requested.filter((path) => path === "/runs/run_older")).toHaveLength(2); });
    await settle();
    expect(root.querySelector(".run-detail")?.textContent).toContain("dpr_older");
    expect(requested.some((path) => path.startsWith("/runs/run_recent_"))).toBe(false);
  });

  it.each([403, 404, 503, "network"])("深链读取失败 %s 不回退到其他 Run", async (failure) => {
    const requested: string[] = [];
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const path = new URL((input as Request).url).pathname;
      requested.push(path);
      if (path === "/runs") return jsonResponse([firstRun]);
      if (path === "/runs/run_unknown") {
        if (failure === "network") throw new TypeError("network");
        return jsonResponse({}, Number(failure));
      }
      return jsonResponse(firstDetail);
    });
    const { root } = await mountRuns(fetch, "/runs?run=run_unknown");

    await eventually(() => { expect(root.querySelector('[role="alert"]')).not.toBeNull(); });
    expect(root.querySelector(".run-detail")?.textContent).not.toContain(firstRun.run_id);
    expect(root.querySelector(".run-detail")?.textContent).not.toContain("email_draft");
    expect(requested).toContain("/runs/run_unknown");
    expect(requested).not.toContain(`/runs/${firstRun.run_id}`);
  });

  it("同页路由参数变化重新读取指定 Run，并在未知响应前清除旧证据", async () => {
    const pending = deferredResponse();
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const path = new URL((input as Request).url).pathname;
      if (path === "/runs") return jsonResponse([firstRun, secondRun]);
      if (path === `/runs/${firstRun.run_id}`) return jsonResponse(firstDetail);
      if (path === `/runs/${secondRun.run_id}`) return jsonResponse(emptyDetail(secondRun));
      return pending.promise;
    });
    const { root } = await mountRuns(fetch, `/runs?run=${firstRun.run_id}`);
    await eventually(() => { expect(root.querySelector(".run-detail")?.textContent).toContain("email_draft"); });

    await router.push(`/runs?run=${secondRun.run_id}`);
    await eventually(() => { expect(root.querySelector(".run-detail")?.textContent).toContain(secondRun.subject_ref); });
    expect(root.querySelector(".run-detail")?.textContent).not.toContain("email_draft");

    await router.push("/runs?run=run_unknown");
    await nextTick();
    expect(root.querySelector(".run-detail")?.textContent).not.toContain(secondRun.run_id);
    expect(root.querySelector(".run-detail")?.textContent).not.toContain(secondRun.subject_ref);
    pending.resolve(jsonResponse({}, 404));
    await eventually(() => { expect(root.textContent).toContain("该 Run 已不存在或不属于当前租户"); });
    expect(root.querySelector(".run-detail")?.textContent).not.toContain(secondRun.subject_ref);
  });

  it.each(["success", "404", "network"])("旧详情的迟到 %s 不覆盖新路由证据", async (outcome) => {
    const older = deferredResponse();
    const requested: string[] = [];
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const path = new URL((input as Request).url).pathname;
      requested.push(path);
      if (path === "/runs") return jsonResponse([firstRun, secondRun]);
      if (path === `/runs/${firstRun.run_id}`) return older.promise;
      return jsonResponse(emptyDetail(secondRun));
    });
    const { root } = await mountRuns(fetch, `/runs?run=${firstRun.run_id}`);
    await eventually(() => { expect(requested).toContain(`/runs/${firstRun.run_id}`); });
    await router.push(`/runs?run=${secondRun.run_id}`);
    await eventually(() => { expect(root.querySelector(".run-detail")?.textContent).toContain(secondRun.run_id); });

    if (outcome === "network") older.reject(new TypeError("network"));
    else older.resolve(jsonResponse(outcome === "404" ? {} : firstDetail, outcome === "404" ? 404 : 200));
    await settle();
    expect(root.querySelector(".run-detail")?.textContent).toContain(secondRun.run_id);
    expect(root.querySelector(".run-detail")?.textContent).not.toContain("email_draft");
    expect(root.querySelector('[role="alert"]')).toBeNull();
  });

  it("旧请求结束不提前结束新 Run 的加载状态", async () => {
    const older = deferredResponse();
    const newer = deferredResponse();
    const requested: string[] = [];
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const path = new URL((input as Request).url).pathname;
      requested.push(path);
      if (path === "/runs") return jsonResponse([firstRun, secondRun]);
      if (path === `/runs/${firstRun.run_id}`) return older.promise;
      return newer.promise;
    });
    const { root } = await mountRuns(fetch, `/runs?run=${firstRun.run_id}`);
    await eventually(() => { expect(requested).toContain(`/runs/${firstRun.run_id}`); });
    await router.push(`/runs?run=${secondRun.run_id}`);
    await eventually(() => { expect(requested).toContain(`/runs/${secondRun.run_id}`); });
    older.resolve(jsonResponse(firstDetail));
    await settle();
    expect(root.querySelector(".run-detail")?.textContent).toContain("正在读取证据链");
    expect(root.querySelector(".run-detail")?.textContent).not.toContain(firstRun.run_id);
    newer.resolve(jsonResponse(emptyDetail(secondRun)));
    await eventually(() => { expect(root.querySelector(".run-detail")?.textContent).toContain(secondRun.run_id); });
  });

  it("列表仍在加载时切换深链，迟到列表不重新选择或覆盖目标", async () => {
    const listing = deferredResponse();
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const path = new URL((input as Request).url).pathname;
      if (path === "/runs") return listing.promise;
      if (path === `/runs/${firstRun.run_id}`) return jsonResponse(firstDetail);
      return jsonResponse(emptyDetail(secondRun));
    });
    const { root } = await mountRuns(fetch, `/runs?run=${firstRun.run_id}`);
    await router.push(`/runs?run=${secondRun.run_id}`);
    await eventually(() => { expect(root.querySelector(".run-detail")?.textContent).toContain(secondRun.run_id); });
    listing.resolve(jsonResponse([firstRun]));
    await settle();
    expect(root.querySelector(".run-detail")?.textContent).toContain(secondRun.run_id);
    expect(root.querySelector(".run-detail")?.textContent).not.toContain("email_draft");
  });

  it.each([503, "network"])("旧列表迟到的 %s 不清除新深链的证据", async (outcome) => {
    const listing = deferredResponse();
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const path = new URL((input as Request).url).pathname;
      if (path === "/runs") return listing.promise;
      return jsonResponse(emptyDetail(secondRun));
    });
    const { root } = await mountRuns(fetch, `/runs?run=${secondRun.run_id}`);
    await eventually(() => { expect(root.querySelector(".run-detail")?.textContent).toContain(secondRun.run_id); });
    if (outcome === "network") listing.reject(new TypeError("network"));
    else listing.resolve(jsonResponse({}, 503));
    await settle();
    expect(root.querySelector(".run-detail")?.textContent).toContain(secondRun.run_id);
    expect(root.querySelector('[role="alert"]')).not.toBeNull();
  });

  it.each(["?run=", "?run=one&run=two"])("无效深链 %s 清除旧证据并报错", async (query) => {
    const requested: string[] = [];
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const path = new URL((input as Request).url).pathname;
      requested.push(path);
      return jsonResponse(path === "/runs" ? [firstRun] : firstDetail);
    });
    const { root } = await mountRuns(fetch, `/runs?run=${firstRun.run_id}`);
    await eventually(() => { expect(root.querySelector(".run-detail")?.textContent).toContain("email_draft"); });
    await router.push(`/runs${query}`);
    await nextTick();
    expect(root.querySelector(".run-detail")?.textContent).not.toContain("email_draft");
    expect(root.querySelector('[role="alert"]')?.textContent).toContain("Run 链接无效");
    expect(requested.filter((path) => path.startsWith("/runs/"))).toHaveLength(1);
  });

  it("移除深链参数恢复列表默认选择，不保留前一深链证据", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const path = new URL((input as Request).url).pathname;
      if (path === "/runs") return jsonResponse([firstRun]);
      return jsonResponse(path === `/runs/${firstRun.run_id}` ? firstDetail : emptyDetail(secondRun));
    });
    const { root } = await mountRuns(fetch, `/runs?run=${secondRun.run_id}`);
    await eventually(() => { expect(root.querySelector(".run-detail")?.textContent).toContain(secondRun.run_id); });
    await router.push("/runs");
    await eventually(() => { expect(root.querySelector(".run-detail")?.textContent).toContain(firstRun.run_id); });
    expect(root.querySelector(".run-detail")?.textContent).not.toContain(secondRun.run_id);
  });

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

it("Task10 Run审批链接只使用该Run返回的精确approval_id", async () => {
  const { root } = await mountRuns(vi.fn(async (raw) => jsonResponse(new URL((raw as Request).url).pathname === '/runs' ? [firstRun] : firstDetail)), `/runs?run=${firstRun.run_id}`);
  await eventually(() => expect(root.textContent).toContain(firstDetail.approvals[0]!.approval_id));
  expect(root.querySelector(`a[href="/approvals?approval_id=${firstDetail.approvals[0]!.approval_id}"]`)).not.toBeNull();
});
