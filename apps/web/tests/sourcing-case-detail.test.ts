import { createApp, nextTick, type App as VueApp } from "vue";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "../src/App.vue";
import type { components } from "../src/api/api";
import { createApiClient } from "../src/api/client";
import router from "../src/router";

type AdmissionDetail = components["schemas"]["SourcingAdmissionDetailView"];

const admissionId = "sad_01K39P9M5D6K4A91YEQ80EJZ0D";
const caseId = "src_01K39P9M5D6K4A91YEQ80EJZ0D";

function admissionDetail(
  state: AdmissionDetail["admission"]["state"],
  overrides: Partial<AdmissionDetail["admission"]> = {},
): AdmissionDetail {
  return {
    admission: {
      admission_id: admissionId,
      admitted_at: state === "admitted" ? "2026-09-02T09:05:00Z" : null,
      admitted_by: state === "admitted" ? "emp_sourcing" : null,
      blocked_reason: state === "blocked" ? "case_state_mismatch" : null,
      can_current_user_manual_start: state === "waiting",
      case_id: caseId,
      cluster_id: "ncl_01K39P9M5D6K4A91YEQ80EJZ0X",
      cluster_member_count: 8,
      explanation: "该需求簇当前有 8 条已验证需求；同规模需求按等待时间排序。",
      facts_observed_at: "2026-09-02T08:59:00Z",
      need_id: "vnd_01K39P9M5D6K4A91YEQ80EJZ0D",
      ranking_version: "need-cluster-admission-v1",
      ready_at: "2026-09-02T08:00:00Z",
      snapshot_id: "sps_01K39P9M5D6K4A91YEQ80EJZ0D",
      state,
      waiting_duration_seconds: 3600,
      ...overrides,
    },
    policy: {
      automatic_admission_enabled: true,
      batch_limit: 2,
      directive_id: "dir_01K39P9M5D6K4A91YEQ80EJZ0X",
      directive_version: 7,
      status: "enabled",
    },
  };
}

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

async function mount(fetch: typeof globalThis.fetch): Promise<HTMLElement> {
  const root = document.createElement("div");
  document.body.replaceChildren(root);
  const app = createApp(App);
  app.provide("tradeos-api-client", createApiClient({ baseUrl: "https://tradeos.test", fetch }));
  app.use(router);
  await router.replace(`/sourcing/${admissionId}`);
  app.mount(root);
  mountedApps.push(app);
  return root;
}

describe("Sourcing admission detail", () => {
  it("renders the immutable priority snapshot and admitted audit without private execution fields", async () => {
    const detail = admissionDetail("admitted", { waiting_duration_seconds: 999_999 });
    const unsafePayload = {
      ...detail,
      admission: {
        ...detail.admission,
        claim_token: "claim-secret-value",
        claim_expires_at: "2026-09-02T09:10:00Z",
        requested_by: "emp_hidden",
        workflow_context: { keywords: ["private-keyword"] },
        raw_exception: "database password leaked",
      },
    };
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const path = new URL((input as Request).url).pathname;
      if (path === "/notifications") return jsonResponse([]);
      if (path === `/sourcing-admissions/${admissionId}`) return jsonResponse(unsafePayload);
      return jsonResponse({ code: "unexpected" }, 500);
    });
    const root = await mount(fetch);

    await eventually(() => {
      expect(root.textContent).toContain("不可变排序快照");
      expect(root.textContent).toContain("sps_01K39P9M5D6K4A91YEQ80EJZ0D");
      expect(root.textContent).toContain("8 条已验证需求");
      expect(root.textContent).toContain("emp_sourcing");
      expect(root.textContent).toContain("2026-09-02");
    });
    const snapshot = root.querySelector<HTMLElement>(".immutable-snapshot")!;
    expect(snapshot.textContent).not.toContain("已等待");
    const timing = root.querySelector<HTMLElement>(".admission-timing")!;
    expect(timing.textContent).toContain("准入等待用时");
    expect(timing.textContent).toContain("1 小时 5 分钟");
    expect(root.textContent).toContain("一个 Need 对应一个 Case；需求簇不是合并订单");
    expect(root.textContent).not.toContain("claim-secret-value");
    expect(root.textContent).not.toContain("claim_expires_at");
    expect(root.textContent).not.toContain("emp_hidden");
    expect(root.textContent).not.toContain("private-keyword");
    expect(root.textContent).not.toContain("database password leaked");
  });

  it.each([
    ["waiting", "当前已等待"],
    ["starting", "自就绪起"],
    ["blocked", "自就绪起"],
  ] as const)("labels %s timing without calling it an immutable snapshot fact", async (state, label) => {
    const detail = admissionDetail(state);
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const path = new URL((input as Request).url).pathname;
      if (path === "/notifications") return jsonResponse([]);
      if (path === `/sourcing-admissions/${admissionId}`) return jsonResponse(detail);
      return jsonResponse({ code: "unexpected" }, 500);
    });
    const root = await mount(fetch);

    await eventually(() => expect(root.querySelector(".admission-timing")).not.toBeNull());
    expect(root.querySelector(".admission-timing")?.textContent).toContain(label);
    expect(root.querySelector(".immutable-snapshot")?.textContent).not.toContain(label);
  });

  it("falls back safely when admitted time is before ready time", async () => {
    const detail = admissionDetail("admitted", { admitted_at: "2026-09-02T07:59:59Z" });
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const path = new URL((input as Request).url).pathname;
      if (path === "/notifications") return jsonResponse([]);
      if (path === `/sourcing-admissions/${admissionId}`) return jsonResponse(detail);
      return jsonResponse({ code: "unexpected" }, 500);
    });
    const root = await mount(fetch);

    await eventually(() => expect(root.querySelector(".admission-timing")).not.toBeNull());
    expect(root.querySelector(".admission-timing")?.textContent).toContain("准入等待用时未知");
  });

  it.each([
    [{ ready_at: "2026-09-02T08:00:00", admitted_at: "2026-09-02T09:05:00Z" }, "准入等待用时未知"],
    [{ ready_at: "September 2, 2026 08:00 UTC", admitted_at: "2026-09-02T09:05:00Z" }, "准入等待用时未知"],
    [{ ready_at: "2026-09-02T08:00:00+08:00", admitted_at: "2026-09-02T01:05:00Z" }, "准入等待用时1 小时 5 分钟"],
  ] as const)("requires zoned ISO instants for admitted duration", async (overrides, expected) => {
    const detail = admissionDetail("admitted", overrides);
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const path = new URL((input as Request).url).pathname;
      if (path === "/notifications") return jsonResponse([]);
      if (path === `/sourcing-admissions/${admissionId}`) return jsonResponse(detail);
      return jsonResponse({ code: "unexpected" }, 500);
    });
    const root = await mount(fetch);

    await eventually(() => expect(root.querySelector(".admission-timing")).not.toBeNull());
    expect(root.querySelector(".admission-timing")?.textContent).toContain(expected);
  });
});
