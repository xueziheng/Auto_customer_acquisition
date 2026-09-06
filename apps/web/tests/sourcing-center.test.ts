import { createApp, nextTick, type App as VueApp } from "vue";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "../src/App.vue";
import { createApiClient } from "../src/api/client";
import router from "../src/router";
import type { components } from "../src/api/api";

const caseId = "src_01K39P9M5D6K4A91YEQ80EJZ0X";
type Admission = components["schemas"]["SourcingAdmissionReadView"];
type AdmissionPolicy = components["schemas"]["SourcingAdmissionPolicyView"];
type SourcingCase = components["schemas"]["SourcingCaseReadView"];

const waitingEightId = "sad_01K39P9M5D6K4A91YEQ80EJZ0A";
const waitingOneId = "sad_01K39P9M5D6K4A91YEQ80EJZ0B";
const blockedId = "sad_01K39P9M5D6K4A91YEQ80EJZ0C";
const admittedId = "sad_01K39P9M5D6K4A91YEQ80EJZ0D";
const startingId = "sad_01K39P9M5D6K4A91YEQ80EJZ0E";
const legacyCaseId = "src_01K39P9M5D6K4A91YEQ80EJZ0F";
const overflowCaseId = "src_01K39P9M5D6K4A91YEQ80EJZ0G";

const enabledPolicy: AdmissionPolicy = {
  automatic_admission_enabled: true,
  batch_limit: 2,
  directive_id: "dir_01K39P9M5D6K4A91YEQ80EJZ0X",
  directive_version: 7,
  status: "enabled",
};

function admissionFixture(
  admissionId: string,
  state: Admission["state"],
  overrides: Partial<Admission> = {},
): Admission {
  return {
    admission_id: admissionId,
    admitted_at: state === "admitted" ? "2026-09-02T09:05:00Z" : null,
    admitted_by: state === "admitted" ? "emp_sourcing" : null,
    blocked_reason: state === "blocked" ? "case_state_mismatch" : null,
    can_current_user_manual_start: state === "waiting",
    case_id: `src_${admissionId.slice(4)}`,
    cluster_id: null,
    cluster_member_count: 1,
    explanation: "该需求尚未归入多成员需求簇；按等待时间排序。",
    facts_observed_at: "2026-09-02T09:00:00Z",
    need_id: `vnd_${admissionId.slice(4)}`,
    ranking_version: "need-cluster-admission-v1",
    ready_at: "2026-09-02T08:00:00Z",
    snapshot_id: `sps_${admissionId.slice(4)}`,
    state,
    waiting_duration_seconds: 3600,
    ...overrides,
  };
}

function openedCase(caseId: string): SourcingCase {
  return {
    active_search_plan_id: null,
    case_id: caseId,
    ladder_checked_to: null,
    need_id: `vnd_${caseId.slice(4)}`,
    need_snapshot: null,
    opened_at: "2026-09-02T08:00:00Z",
    state: "opened",
    state_changed_at: null,
    stop: null,
    version: 1,
    workflow_version: 1,
  };
}

function admissionCenterFetch(options: {
  admittedOverrides?: Partial<Admission>;
  extraCases?: SourcingCase[];
  failedState?: Admission["state"];
  manualKeys?: string[];
  manualStatus?: number;
  pendingManual?: Promise<Response>;
  stateItems?: Partial<Record<Admission["state"], Admission[]>>;
} = {}): typeof globalThis.fetch {
  const waiting = [
    admissionFixture(waitingOneId, "waiting"),
    admissionFixture(waitingEightId, "waiting", {
      cluster_id: "ncl_01K39P9M5D6K4A91YEQ80EJZ0X",
      cluster_member_count: 8,
      explanation: "该需求簇当前有 8 条已验证需求；同规模需求按等待时间排序。",
      waiting_duration_seconds: 7200,
    }),
  ];
  const byState: Record<string, Admission[]> = {
    admitted: [admissionFixture(admittedId, "admitted", options.admittedOverrides)],
    blocked: [admissionFixture(blockedId, "blocked")],
    starting: [admissionFixture(startingId, "starting")],
    waiting,
    ...options.stateItems,
  };
  const openCases = [
    openedCase(admissionFixture(waitingEightId, "waiting").case_id),
    openedCase(legacyCaseId),
    openedCase(admissionFixture(blockedId, "blocked").case_id),
    openedCase(admissionFixture(startingId, "starting").case_id),
    openedCase(admissionFixture(admittedId, "admitted").case_id),
    openedCase(admissionFixture(waitingOneId, "waiting").case_id),
    ...(options.extraCases ?? []),
  ];
  return vi.fn<typeof globalThis.fetch>(async (input) => {
    const request = input as Request;
    const url = new URL(request.url);
    if (url.pathname === "/notifications") return jsonResponse([]);
    if (request.method === "GET" && url.pathname === "/sourcing-admissions") {
      const state = url.searchParams.get("state") ?? "waiting";
      if (state === options.failedState) return jsonResponse({ code: "unavailable" }, 503);
      return jsonResponse({ items: byState[state], policy: enabledPolicy });
    }
    if (request.method === "GET" && url.pathname === "/sourcing-cases") return jsonResponse(openCases);
    if (request.method === "POST" && url.pathname === `/sourcing-admissions/${waitingOneId}/admit`) {
      const key = request.headers.get("Idempotency-Key");
      if (key) options.manualKeys?.push(key);
      if (options.pendingManual) return options.pendingManual;
      if (options.manualStatus) return jsonResponse({ code: "manual_failed" }, options.manualStatus);
      return jsonResponse(admissionFixture(waitingOneId, "admitted"));
    }
    return jsonResponse({ code: "unexpected", message: url.pathname }, 500);
  });
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

type SecondaryProjection = "candidates" | "plan" | "review" | "quota" | "uncertain";
type FailureMode = "forbidden" | "unavailable" | "network";
type QuotaScenario = "forbidden" | "unavailable" | "network" | "paid" | "unknown" | "exhausted";

const projectionExpectation: Record<SecondaryProjection, {
  forbidden: string;
  retry: string;
  unavailable: string;
  unaffected: string;
}> = {
  candidates: {
    forbidden: "当前身份无权读取候选",
    retry: "重试读取候选",
    unavailable: "候选数据暂不可用",
    unaffected: "当前计划 v1",
  },
  plan: {
    forbidden: "当前身份无权读取寻源计划",
    retry: "重试读取寻源计划",
    unavailable: "寻源计划暂不可用",
    unaffected: "Example supplier",
  },
  quota: {
    forbidden: "当前身份无权读取当前额度",
    retry: "重试读取当前额度",
    unavailable: "当前额度暂不可用",
    unaffected: "Example supplier",
  },
  review: {
    forbidden: "当前身份无权读取审核",
    retry: "重试读取审核",
    unavailable: "审核数据暂不可用",
    unaffected: "Example supplier",
  },
  uncertain: {
    forbidden: "当前身份无权读取不确定请求",
    retry: "重试读取不确定请求",
    unavailable: "不确定请求暂不可用",
    unaffected: "Example supplier",
  },
};

function secondaryFailureResponse(mode: FailureMode): Response | Error {
  if (mode === "forbidden") return jsonResponse({ code: "forbidden" }, 403);
  if (mode === "unavailable") return jsonResponse({ code: "unavailable" }, 503);
  return new TypeError("network unavailable");
}

function partialFailureFetch(
  failedProjection: SecondaryProjection | null,
  mode: FailureMode,
  emptyProjection?: SecondaryProjection,
): typeof globalThis.fetch {
  return vi.fn<typeof globalThis.fetch>(async (input) => {
    const path = new URL((input as Request).url).pathname;
    if (path === "/notifications") return jsonResponse([]);
    if (path === `/sourcing-cases/${caseId}`) return jsonResponse({
      active_search_plan_id: "spl_01K39P9M5D6K4A91YEQ80EJZ0X",
      case_id: caseId,
      ladder_checked_to: 5,
      need_id: "need_01K39P9M5D6K4A91YEQ80EJZ0X",
      need_snapshot: null,
      opened_at: "2026-08-30T09:00:00Z",
      state: "candidates_ready",
      state_changed_at: null,
      stop: null,
      version: 5,
      workflow_version: 2,
    });
    const endpoint: Record<SecondaryProjection, string> = {
      candidates: `/sourcing-cases/${caseId}/candidates`,
      plan: `/sourcing-cases/${caseId}/public-search-plan`,
      quota: `/sourcing-cases/${caseId}/current-quota`,
      review: `/sourcing-cases/${caseId}/review`,
      uncertain: `/sourcing-cases/${caseId}/uncertain-reconciliations`,
    };
    const projection = (Object.keys(endpoint) as SecondaryProjection[]).find(
      (key) => endpoint[key] === path,
    );
    if (projection !== undefined && projection === failedProjection) {
      const failure = secondaryFailureResponse(mode);
      if (failure instanceof Error) throw failure;
      return failure;
    }
    if (path === endpoint.candidates) return jsonResponse(emptyProjection === "candidates" ? [] : [{
      candidate_id: "sup_01K39P9M5D6K4A91YEQ80EJZ0X",
      currency: "USD",
      evidence: [],
      indicative_price_tiers: [],
      match_inferences: {},
      moq: 500,
      observed_facts: {},
      price_basis: "indicative",
      price_unit: "piece",
      product_title: "Stainless hinge",
      rejection_reasons: [],
      source_platform: "web",
      spec_comparisons: [],
      supplier_claims: {},
      supplier_name: "Example supplier",
      supply_option: {
        is_qualified: true,
        option_id: "sop_01K39P9M5D6K4A91YEQ80EJZ0X",
        product_id: "prd_01K39P9M5D6K4A91YEQ80EJZ0X",
        source_kind: "supplier_candidate",
        supplier_candidate_id: "sup_01K39P9M5D6K4A91YEQ80EJZ0X",
      },
      verification_missing: [],
      verification_status: "qualified",
    }]);
    if (path === "/sourcing-cases/" + caseId + "/ladder-checks") return jsonResponse([]);
    if (path === endpoint.plan) return jsonResponse(emptyProjection === "plan" ? null : {
      case_id: caseId,
      confirmed_at: null,
      confirmed_by: null,
      created_at: "2026-08-30T09:00:00Z",
      expected_case_version: 5,
      max_pages_read: 1,
      max_search_queries: 1,
      plan_hash: "c".repeat(64),
      plan_id: "spl_01K39P9M5D6K4A91YEQ80EJZ0X",
      product_category: "hinges",
      provider: "tavily",
      queries: [{ lane: null, query_text: "stainless hinge", target_country: "CN" }],
      search_depth: "basic",
      status: "pending_confirmation",
      target_countries: ["CN"],
      usage_credits_remaining: 2,
      version: 1,
      worst_case_credits: 1,
    });
    if (path === endpoint.quota) return jsonResponse({
      checked_at: "2026-08-30T09:00:00Z",
      cost_status: "free",
      paygo_enabled: false,
      remaining: 2,
      reservations: 0,
    });
    if (path === endpoint.review) return jsonResponse(null);
    if (path === endpoint.uncertain) return jsonResponse(emptyProjection === "uncertain" ? [] : [{
      can_current_user_reconcile: true,
      created_at: "2026-08-30T09:00:00Z",
      execution_id: "sxe_01K39P9M5D6K4A91YEQ80EJZ0X",
      reconciliation: null,
      request_key: "b".repeat(64),
      run_id: "run_01K39P9M5D6K4A91YEQ80EJZ0X",
      status: "uncertain",
    }]);
    return jsonResponse({ code: "unexpected", message: "unexpected" }, 500);
  });
}

const quotaScenarioExpectation: Record<QuotaScenario, {
  blockedRun: string;
  quotaMessage: string;
  runVisible: boolean;
}> = {
  forbidden: {
    blockedRun: "",
    quotaMessage: "当前身份无权读取当前额度",
    runVisible: false,
  },
  unavailable: {
    blockedRun: "运行公开寻源暂不可用；请重试读取当前额度。",
    quotaMessage: "当前额度暂不可用",
    runVisible: false,
  },
  network: {
    blockedRun: "运行公开寻源暂不可用；请重试读取当前额度。",
    quotaMessage: "当前额度暂不可用",
    runVisible: false,
  },
  paid: {
    blockedRun: "运行公开寻源已阻止：当前额度状态为 paid 或已启用付费，不会走付费回退。",
    quotaMessage: "",
    runVisible: true,
  },
  unknown: {
    blockedRun: "运行公开寻源已阻止：当前额度状态未知，不能推断为免费。",
    quotaMessage: "",
    runVisible: true,
  },
  exhausted: {
    blockedRun: "运行公开寻源已阻止：当前免费额度不足以覆盖最坏消耗。",
    quotaMessage: "",
    runVisible: true,
  },
};

function quotaScenarioFetch(scenario: QuotaScenario): {
  fetch: typeof globalThis.fetch;
  postPaths: string[];
  quotaReads: { value: number };
} {
  const fallback = partialFailureFetch(null, "unavailable");
  const postPaths: string[] = [];
  const quotaReads = { value: 0 };
  let plan = {
    case_id: caseId,
    confirmed_at: null,
    confirmed_by: null,
    created_at: "2026-08-30T09:00:00Z",
    expected_case_version: 5,
    max_pages_read: 1,
    max_search_queries: 1,
    plan_hash: "c".repeat(64),
    plan_id: "spl_01K39P9M5D6K4A91YEQ80EJZ0X",
    product_category: "hinges",
    provider: "tavily",
    queries: [{ lane: null, query_text: "stainless hinge", target_country: "CN" }],
    search_depth: "basic",
    status: "pending_confirmation",
    target_countries: ["CN"],
    usage_credits_remaining: 2,
    version: 1,
    worst_case_credits: 1,
  };
  const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
    const request = input as Request;
    const path = new URL(request.url).pathname;
    if (request.method === "POST") {
      postPaths.push(path);
      if (path.endsWith("/public-search-plan/confirm")) {
        plan = { ...plan, status: "authorized" };
      }
      return jsonResponse(plan);
    }
    if (path.endsWith("/public-search-plan")) return jsonResponse(plan);
    if (path.endsWith("/current-quota")) {
      quotaReads.value += 1;
      if (scenario === "forbidden") return jsonResponse({ code: "forbidden" }, 403);
      if (scenario === "unavailable") return jsonResponse({ code: "unavailable" }, 503);
      if (scenario === "network") throw new TypeError("network unavailable");
      if (scenario === "paid") return jsonResponse({
        checked_at: "2026-08-30T09:00:00Z", cost_status: "paid", paygo_enabled: true, remaining: 2, reservations: 0,
      });
      if (scenario === "unknown") return jsonResponse({
        checked_at: null, cost_status: "unknown", paygo_enabled: null, remaining: null, reservations: null,
      });
      return jsonResponse({
        checked_at: "2026-08-30T09:00:00Z", cost_status: "free", paygo_enabled: false, remaining: 0, reservations: 0,
      });
    }
    return fallback(input);
  });
  return { fetch, postPaths, quotaReads };
}

function planButton(root: HTMLElement, name: string): HTMLButtonElement | undefined {
  return [...root.querySelectorAll<HTMLButtonElement>(".plan-panel button")].find(
    (button) => button.textContent?.includes(name),
  );
}

describe("Sourcing and Product centers", () => {
  it.each([
    ["candidates", "forbidden"], ["candidates", "unavailable"], ["candidates", "network"],
    ["plan", "forbidden"], ["plan", "unavailable"], ["plan", "network"],
    ["review", "forbidden"], ["review", "unavailable"], ["review", "network"],
    ["quota", "forbidden"], ["quota", "unavailable"], ["quota", "network"],
    ["uncertain", "forbidden"], ["uncertain", "unavailable"], ["uncertain", "network"],
  ] as const)("does not convert %s %s into a business-empty Case detail state", async (projection, mode) => {
    const root = await mount(`/sourcing/${caseId}`, partialFailureFetch(projection, mode));
    const expected = projectionExpectation[projection];

    await eventually(() => {
      expect(root.textContent).toContain(
        mode === "forbidden" ? expected.forbidden : expected.unavailable,
      );
      expect(root.textContent).toContain(expected.unaffected);
    });
    if (mode !== "forbidden") expect(root.textContent).toContain(expected.retry);

    if (projection === "review") {
      expect(root.textContent).not.toContain("提交人工选择");
    }
    if (projection === "candidates") {
      expect(root.textContent).not.toContain("提交人工选择");
      expect(root.textContent).not.toContain("没有候选；未知不是“合格”。");
    }
    if (projection === "plan") {
      expect(root.querySelector(".plan-panel form")).toBeNull();
    }
    if (projection === "quota") {
      expect(planButton(root, "创建新的计划版本")?.disabled).toBe(false);
      expect(planButton(root, "确认精确范围")?.disabled).toBe(false);
      expect(planButton(root, "运行公开寻源")).toBeUndefined();
    }
    if (projection === "uncertain") {
      expect(root.textContent).not.toContain("没有可展示的不确定搜索请求。");
    }
  });

  it("renders an empty candidate list only after its 200 projection succeeds", async () => {
    const root = await mount(
      `/sourcing/${caseId}`,
      partialFailureFetch(null, "unavailable", "candidates"),
    );

    await eventually(() => {
      expect(root.textContent).toContain("没有候选；未知不是“合格”。");
      expect(root.textContent).toContain("当前计划 v1");
    });
    expect(root.textContent).not.toContain("候选数据暂不可用");
  });

  it("retries only an unavailable projection without erasing a successful plan", async () => {
    const successfulFetch = partialFailureFetch(null, "unavailable");
    let candidateRequests = 0;
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const path = new URL((input as Request).url).pathname;
      if (path === `/sourcing-cases/${caseId}/candidates` && candidateRequests++ === 0) {
        return jsonResponse({ code: "unavailable" }, 503);
      }
      return successfulFetch(input);
    });
    const root = await mount(`/sourcing/${caseId}`, fetch);

    await eventually(() => {
      expect(root.textContent).toContain("候选数据暂不可用");
      expect(root.textContent).toContain("当前计划 v1");
    });
    [...root.querySelectorAll<HTMLButtonElement>("button")].find(
      (button) => button.textContent?.includes("重试读取候选"),
    )!.click();

    await eventually(() => {
      expect(root.textContent).toContain("Example supplier");
      expect(root.textContent).toContain("当前计划 v1");
    });
  });

  it("replays only the saved review after Opportunity is supplied for a cost-handoff stop", async () => {
    const fallback = partialFailureFetch(null, "unavailable");
    const retryBodies: Record<string, unknown>[] = [];
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const request = input as Request;
      const path = new URL(request.url).pathname;
      if (path === "/notifications") return jsonResponse([]);
      if (request.method === "POST" && path === `/sourcing-cases/${caseId}/review`) {
        retryBodies.push(await request.json() as Record<string, unknown>);
        return jsonResponse({
          alternate_option_ids: [],
          can_current_user_confirm: false,
          case_id: caseId,
          confirmed_at: "2026-08-30T10:00:00Z",
          confirmed_by: "emp_boss",
          expected_case_version: 5,
          primary_option_id: "sop_01K39P9M5D6K4A91YEQ80EJZ0X",
          reason: "已核验公开证据，只做内部估算。",
          review_id: "srv_01K39P9M5D6K4A91YEQ80EJZ0X",
          submitted_at: "2026-08-30T09:00:00Z",
          submitted_by: "emp_01K39P9M5D6K4A91YEQ80EJZ0X",
        });
      }
      if (path === `/sourcing-cases/${caseId}`) return jsonResponse({
        active_search_plan_id: "spl_01K39P9M5D6K4A91YEQ80EJZ0X",
        case_id: caseId,
        ladder_checked_to: 5,
        need_id: "need_01K39P9M5D6K4A91YEQ80EJZ0X",
        need_snapshot: null,
        opened_at: "2026-08-30T09:00:00Z",
        state: "candidates_ready",
        state_changed_at: null,
        stop: { code: "opportunity_required", stage: "cost_handoff" },
        version: 5,
        workflow_version: 2,
      });
      if (path === `/sourcing-cases/${caseId}/review`) return jsonResponse({
        alternate_option_ids: [],
        can_current_user_confirm: false,
        case_id: caseId,
        confirmed_at: "2026-08-30T10:00:00Z",
        confirmed_by: "emp_boss",
        expected_case_version: 5,
        primary_option_id: "sop_01K39P9M5D6K4A91YEQ80EJZ0X",
        reason: "已核验公开证据，只做内部估算。",
        review_id: "srv_01K39P9M5D6K4A91YEQ80EJZ0X",
        submitted_at: "2026-08-30T09:00:00Z",
        submitted_by: "emp_01K39P9M5D6K4A91YEQ80EJZ0X",
      });
      return fallback(input);
    });

    const root = await mount(`/sourcing/${caseId}`, fetch);
    await eventually(() => expect(root.textContent).toContain("Opportunity 缺失，需先补齐后再尝试成本交接。"));
    [...root.querySelectorAll<HTMLButtonElement>("button")].find(
      (button) => button.textContent?.includes("在补齐 Opportunity 后重试成本交接"),
    )!.click();

    await eventually(() => expect(retryBodies).toEqual([{
      alternate_option_ids: [],
      expected_case_version: 5,
      primary_option_id: "sop_01K39P9M5D6K4A91YEQ80EJZ0X",
      reason: "已核验公开证据，只做内部估算。",
    }]));
  });

  it.each(["forbidden", "unavailable", "network", "paid", "unknown", "exhausted"] as const)(
    "keeps draft and confirm quota-independent while %s blocks run",
    async (scenario) => {
      const { fetch, postPaths, quotaReads } = quotaScenarioFetch(scenario);
      const root = await mount(`/sourcing/${caseId}`, fetch);
      const expected = quotaScenarioExpectation[scenario];

      await eventually(() => {
        if (expected.quotaMessage) expect(root.textContent).toContain(expected.quotaMessage);
        expect(planButton(root, "创建新的计划版本")?.disabled).toBe(false);
        expect(planButton(root, "确认精确范围")?.disabled).toBe(false);
      });
      const initialQuotaReads = quotaReads.value;
      root.querySelector<HTMLFormElement>(".plan-panel form")!.dispatchEvent(
        new Event("submit", { bubbles: true, cancelable: true }),
      );
      await eventually(() => expect(postPaths).toContain(`/sourcing-cases/${caseId}/public-search-plan`));
      planButton(root, "确认精确范围")!.click();
      await eventually(() => expect(root.textContent).toContain("authorized"));

      expect(quotaReads.value).toBe(initialQuotaReads);
      expect(postPaths).not.toContain(`/sourcing-cases/${caseId}/run`);
      if (expected.runVisible) {
        const run = planButton(root, "运行公开寻源");
        expect(run?.disabled).toBe(true);
        expect(root.textContent).toContain(expected.blockedRun);
        run?.click();
      } else {
        expect(planButton(root, "运行公开寻源")).toBeUndefined();
        expect(root.textContent).toContain(expected.blockedRun);
      }
      await nextTick();
      expect(postPaths).not.toContain(`/sourcing-cases/${caseId}/run`);
    },
  );

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
      if (path.endsWith("/current-quota")) return jsonResponse({ checked_at: "2026-08-30T09:00:00Z", cost_status: "free", paygo_enabled: false, remaining: 2, reservations: 0 });
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
    const recoverySelect = recoveryPanel.querySelector<HTMLSelectElement>("select")!;
    recoverySelect.value = recoverySelect.options[1]!.value;
    recoverySelect.dispatchEvent(new Event("change", { bubbles: true }));
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

describe("Sourcing admission queue", () => {
  it("keeps server order and separates waiting admission from in-progress cases", async () => {
    const root = await mount("/sourcing", admissionCenterFetch());

    await eventually(() => {
      expect(root.textContent).toContain("等待准入");
      expect(root.textContent).toContain("处理中");
      expect(root.textContent).toContain("8 条已验证需求");
      expect(root.textContent).toContain("尚未归簇（按 1 条需求排序）");
      expect(root.textContent).toContain("案例状态不匹配");
      expect(root.textContent).toContain("Directive v7");
    });
    const waitingRows = [...root.querySelectorAll<HTMLElement>('[data-section="waiting-admission"] [data-admission-id]')];
    expect(waitingRows.map((row) => row.dataset.admissionId)).toEqual([
      waitingOneId,
      waitingEightId,
      blockedId,
    ]);
    const representedCaseIds = [waitingOneId, waitingEightId, blockedId, startingId, admittedId]
      .map((id) => admissionFixture(id, id === blockedId ? "blocked" : id === admittedId ? "admitted" : id === startingId ? "starting" : "waiting").case_id);
    const admissionSections = [
      root.querySelector('[data-section="waiting-admission"]')!,
      root.querySelector('[data-section="active-admissions"]')!,
    ];
    for (const representedCaseId of representedCaseIds) {
      const rows = admissionSections.flatMap((section) => [...section.querySelectorAll<HTMLElement>("article.case-row, article.admission-row")])
        .filter((row) => row.textContent?.includes(representedCaseId));
      expect(rows, representedCaseId).toHaveLength(1);
    }
    const processingRows = [...root.querySelectorAll<HTMLElement>('[data-section="active-admissions"] .case-row')];
    expect(processingRows.map((row) => row.textContent)).toEqual([
      expect.stringContaining(admissionFixture(startingId, "starting").case_id),
      expect.stringContaining(admissionFixture(admittedId, "admitted").case_id),
    ]);
    expect(processingRows[0]?.textContent).toContain("自就绪起 1 小时");
    expect(processingRows[0]?.textContent).toContain("该需求尚未归入多成员需求簇");
    expect(processingRows[1]?.textContent).toContain("准入等待用时 1 小时 5 分钟");
    expect(processingRows[1]?.textContent).toContain("emp_sourcing");
    expect(processingRows[1]?.textContent).toContain("2026-09-02 09:05:00 UTC");
    const blockedRow = root.querySelector<HTMLElement>(`[data-admission-id="${blockedId}"]`);
    expect(blockedRow?.textContent).toContain("自就绪起 1 小时");
    const workbenchRows = [...root.querySelectorAll<HTMLElement>('[data-section="case-workbench"] .case-row')];
    expect(workbenchRows.map((row) => row.textContent)).toEqual([
      expect.stringContaining(admissionFixture(waitingEightId, "waiting").case_id),
      expect.stringContaining(legacyCaseId),
      expect.stringContaining(admissionFixture(blockedId, "blocked").case_id),
      expect.stringContaining(admissionFixture(startingId, "starting").case_id),
      expect.stringContaining(admissionFixture(admittedId, "admitted").case_id),
      expect.stringContaining(admissionFixture(waitingOneId, "waiting").case_id),
    ]);
    expect(root.textContent).toContain("一个 Need 对应一个 Case；需求簇不是合并订单");
  });

  it("offers separate admission audit and Case workspace links for active admissions", async () => {
    const root = await mount("/sourcing", admissionCenterFetch());
    await eventually(() => expect(root.textContent).toContain("处理中"));

    for (const [id, state] of [[startingId, "starting"], [admittedId, "admitted"]] as const) {
      const item = admissionFixture(id, state);
      const row = [...root.querySelectorAll<HTMLElement>('[data-section="active-admissions"] .case-row')]
        .find((candidate) => candidate.textContent?.includes(item.case_id));
      expect(row).toBeDefined();
      expect(row?.textContent).toContain("准入审计详情");
      expect(row?.textContent).toContain("Case 工作台");
      expect([...row!.querySelectorAll<HTMLAnchorElement>("a")].map((link) => link.getAttribute("href")))
        .toEqual([`/sourcing/${id}`, `/sourcing/${item.case_id}`]);
    }
  });

  it("fails closed when a 50-item admission page may be truncated", async () => {
    const truncatedWaiting = Array.from({ length: 50 }, (_, index) => admissionFixture(
      `sad_truncated_${String(index).padStart(2, "0")}`,
      "waiting",
    ));
    const root = await mount("/sourcing", admissionCenterFetch({
      extraCases: [openedCase(overflowCaseId)],
      stateItems: { waiting: truncatedWaiting },
    }));

    await eventually(() => expect(root.textContent).toContain("准入队列可能已截断，无法安全展示等待与处理中的准入记录"));
    expect(root.querySelector('[data-section="waiting-admission"]')).toBeNull();
    expect(root.querySelector('[data-section="active-admissions"]')).toBeNull();
    expect(root.querySelector('[data-section="case-workbench"]')?.textContent).toContain(overflowCaseId);
    expect(root.querySelector('[data-section="case-workbench"]')?.textContent).toContain(legacyCaseId);
  });

  it("fails closed when any admission state cannot be read", async () => {
    const root = await mount("/sourcing", admissionCenterFetch({ failedState: "blocked" }));

    await eventually(() => expect(root.textContent).toContain("寻源准入队列暂不可用"));
    expect(root.querySelector('[data-section="waiting-admission"]')).toBeNull();
    expect(root.querySelector('[data-section="active-admissions"]')).toBeNull();
    expect(root.querySelector('[data-section="case-workbench"]')?.textContent)
      .toContain(admissionFixture(blockedId, "blocked").case_id);
    expect(root.querySelector('[data-section="case-workbench"]')?.textContent).toContain(legacyCaseId);
  });

  it("fails closed when one admission appears in two state responses from the same refresh", async () => {
    const duplicateStarting = admissionFixture(waitingOneId, "starting");
    const root = await mount("/sourcing", admissionCenterFetch({
      stateItems: { starting: [duplicateStarting] },
    }));

    await eventually(() => expect(root.textContent).toContain("准入队列状态不一致，已停止展示，请刷新重试"));
    expect(root.querySelector('[data-section="waiting-admission"]')).toBeNull();
    expect(root.querySelector('[data-section="active-admissions"]')).toBeNull();
  });

  it("keeps a Case neutral when a state migration makes its admission absent from all four responses", async () => {
    const root = await mount("/sourcing", admissionCenterFetch({
      stateItems: {
        waiting: [admissionFixture(waitingEightId, "waiting")],
        starting: [admissionFixture(startingId, "starting")],
      },
    }));

    await eventually(() => expect(root.querySelector('[data-section="case-workbench"]')).not.toBeNull());
    const omittedCaseId = admissionFixture(waitingOneId, "waiting").case_id;
    const admissionSections = [
      root.querySelector('[data-section="waiting-admission"]')?.textContent ?? "",
      root.querySelector('[data-section="active-admissions"]')?.textContent ?? "",
    ].join(" ");
    const workbench = root.querySelector('[data-section="case-workbench"]')!;

    expect(admissionSections).not.toContain(omittedCaseId);
    expect(workbench.textContent).toContain(omittedCaseId);
    expect(workbench.textContent).toContain("Case 状态：opened");
    expect(workbench.textContent).not.toContain("处理中");
  });

  it("closes a stale manual dialog when a refresh cannot rebuild the admission partition", async () => {
    const options: Parameters<typeof admissionCenterFetch>[0] = {};
    const root = await mount("/sourcing", admissionCenterFetch(options));
    await eventually(() => expect(root.querySelector("button[data-manual-admit]")).not.toBeNull());
    root.querySelector<HTMLButtonElement>("button[data-manual-admit]")!.click();
    await eventually(() => expect(root.querySelector('[role="dialog"]')).not.toBeNull());

    options.failedState = "blocked";
    [...root.querySelectorAll<HTMLButtonElement>("button")]
      .find((button) => button.textContent?.includes("刷新"))!.click();

    await eventually(() => expect(root.textContent).toContain("寻源准入队列暂不可用"));
    expect(root.querySelector('[role="dialog"]')).toBeNull();
    expect(root.querySelector('[data-section="waiting-admission"]')).toBeNull();
  });

  it("reloads canonical server order when a pending manual admission overlaps refresh", async () => {
    let resolveManual!: (response: Response) => void;
    const pendingManual = new Promise<Response>((resolve) => { resolveManual = resolve; });
    let manualStarted = false;
    let manualResolved = false;
    const fallback = admissionCenterFetch({ pendingManual });
    const promotedStarting = admissionFixture(waitingOneId, "starting");
    const promotedAdmitted = admissionFixture(waitingOneId, "admitted");
    const existingAdmitted = admissionFixture(admittedId, "admitted");
    const stateAfterRefresh: Record<Admission["state"], Admission[]> = {
      waiting: [admissionFixture(waitingEightId, "waiting")],
      blocked: [admissionFixture(blockedId, "blocked")],
      starting: [promotedStarting],
      admitted: [existingAdmitted],
    };
    const canonicalState: Record<Admission["state"], Admission[]> = {
      waiting: [admissionFixture(waitingEightId, "waiting")],
      blocked: [admissionFixture(blockedId, "blocked")],
      starting: [],
      admitted: [existingAdmitted, promotedAdmitted],
    };
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const request = input as Request;
      const url = new URL(request.url);
      if (request.method === "POST" && url.pathname === `/sourcing-admissions/${waitingOneId}/admit`) {
        manualStarted = true;
        return fallback(input);
      }
      if (request.method === "GET" && url.pathname === "/sourcing-admissions" && manualStarted) {
        const state = url.searchParams.get("state") as Admission["state"];
        return jsonResponse({
          items: (manualResolved ? canonicalState : stateAfterRefresh)[state],
          policy: enabledPolicy,
        });
      }
      return fallback(input);
    });
    const root = await mount("/sourcing", fetch);
    await eventually(() => expect(root.querySelectorAll<HTMLButtonElement>("button[data-manual-admit]")).toHaveLength(2));

    root.querySelector<HTMLButtonElement>("button[data-manual-admit]")!.click();
    await eventually(() => expect(root.querySelector('[role="dialog"]')).not.toBeNull());
    [...root.querySelectorAll<HTMLButtonElement>('[role="dialog"] button')]
      .find((button) => button.textContent?.includes("确认准入"))!.click();
    await eventually(() => expect(manualStarted).toBe(true));

    [...root.querySelectorAll<HTMLButtonElement>("button")]
      .find((button) => button.textContent?.includes("刷新"))!.click();
    await eventually(() => {
      const activeText = root.querySelector('[data-section="active-admissions"]')?.textContent ?? "";
      expect(activeText).toContain(promotedStarting.case_id);
      expect(activeText).toContain(existingAdmitted.case_id);
    });

    manualResolved = true;
    resolveManual(jsonResponse(promotedAdmitted));
    await eventually(() => {
      const processingRows = [...root.querySelectorAll<HTMLElement>('[data-section="active-admissions"] article.case-row')];
      expect(processingRows.map((row) => row.textContent)).toEqual([
        expect.stringContaining(existingAdmitted.case_id),
        expect.stringContaining(promotedAdmitted.case_id),
      ]);
      expect(processingRows.filter((row) => row.textContent?.includes(promotedAdmitted.case_id))).toHaveLength(1);
    });
  });

  it("removes a pending dialog when refresh must fail closed", async () => {
    let resolveManual!: (response: Response) => void;
    const pendingManual = new Promise<Response>((resolve) => { resolveManual = resolve; });
    const options: Parameters<typeof admissionCenterFetch>[0] = { pendingManual };
    const root = await mount("/sourcing", admissionCenterFetch(options));
    await eventually(() => expect(root.querySelector("button[data-manual-admit]")).not.toBeNull());

    root.querySelector<HTMLButtonElement>("button[data-manual-admit]")!.click();
    await eventually(() => expect(root.querySelector('[role="dialog"]')).not.toBeNull());
    [...root.querySelectorAll<HTMLButtonElement>('[role="dialog"] button')]
      .find((button) => button.textContent?.includes("确认准入"))!.click();
    await eventually(() => expect(root.textContent).toContain("正在准入…"));

    options.failedState = "blocked";
    [...root.querySelectorAll<HTMLButtonElement>("button")]
      .find((button) => button.textContent?.includes("刷新"))!.click();

    await eventually(() => expect(root.textContent).toContain("寻源准入队列暂不可用"));
    expect(root.querySelector('[role="dialog"]')).toBeNull();
    expect(root.querySelector('[data-section="waiting-admission"]')).toBeNull();
    expect(root.querySelector('[data-section="active-admissions"]')).toBeNull();

    resolveManual(jsonResponse(admissionFixture(waitingOneId, "admitted")));
    await eventually(() => expect(root.textContent).toContain("已准入；这只代表该 Case 获准启动"));
    expect(root.querySelector('[data-section="active-admissions"]')).toBeNull();
  });

  it.each([
    [{ ready_at: "2026-09-02T08:00:00", admitted_at: "2026-09-02T09:05:00Z" }, "准入等待用时 未知"],
    [{ ready_at: "2026-09-02T08:00:00+08:00", admitted_at: "2026-09-02T01:05:00Z" }, "准入等待用时 1 小时 5 分钟"],
  ] as const)("requires zoned ISO instants for active admitted timing", async (overrides, expected) => {
    const root = await mount("/sourcing", admissionCenterFetch({ admittedOverrides: overrides }));

    await eventually(() => expect(root.textContent).toContain(expected));
  });

  it("disables only the selected row while manual admission is pending", async () => {
    let resolveManual!: (response: Response) => void;
    const pendingManual = new Promise<Response>((resolve) => { resolveManual = resolve; });
    const root = await mount("/sourcing", admissionCenterFetch({ pendingManual }));
    await eventually(() => expect(root.querySelectorAll<HTMLButtonElement>("button[data-manual-admit]")).toHaveLength(2));

    const buttons = [...root.querySelectorAll<HTMLButtonElement>("button[data-manual-admit]")];
    buttons[0]!.click();
    await eventually(() => expect(root.querySelector('[role="dialog"]')).not.toBeNull());
    [...root.querySelectorAll<HTMLButtonElement>('[role="dialog"] button')].find((button) => button.textContent?.includes("确认准入"))!.click();

    await eventually(() => expect(buttons[0]!.disabled).toBe(true));
    expect(buttons[1]!.disabled).toBe(false);
    resolveManual(jsonResponse(admissionFixture(waitingOneId, "admitted")));
    await eventually(() => expect(root.textContent).toContain("已准入；这只代表该 Case 获准启动"));
  });

  it.each([
    [409, "准入状态已变化，请刷新队列后重试"],
    [503, "寻源准入服务暂不可用，请稍后重试"],
  ] as const)("restores row controls after a %s manual response", async (status, message) => {
    const manualKeys: string[] = [];
    const root = await mount("/sourcing", admissionCenterFetch({ manualKeys, manualStatus: status }));
    await eventually(() => expect(root.querySelectorAll<HTMLButtonElement>("button[data-manual-admit]")).toHaveLength(2));

    const button = root.querySelector<HTMLButtonElement>("button[data-manual-admit]")!;
    button.click();
    await eventually(() => expect(root.querySelector('[role="dialog"]')).not.toBeNull());
    [...root.querySelectorAll<HTMLButtonElement>('[role="dialog"] button')].find((item) => item.textContent?.includes("确认准入"))!.click();

    await eventually(() => expect(root.textContent).toContain(message));
    expect(button.disabled).toBe(false);

    button.click();
    await eventually(() => expect(root.querySelector('[role="dialog"]')).not.toBeNull());
    [...root.querySelectorAll<HTMLButtonElement>('[role="dialog"] button')].find((item) => item.textContent?.includes("确认准入"))!.click();
    await eventually(() => expect(manualKeys).toHaveLength(2));
    expect(manualKeys[1]).toBe(manualKeys[0]);
    expect(root.textContent).not.toContain(manualKeys[0]);
  });
});
