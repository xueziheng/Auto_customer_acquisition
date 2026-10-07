import { createApp, nextTick, type App as VueApp } from "vue";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "../src/api/client";
import type { components } from "../src/api/api";
import App from "../src/App.vue";
import router from "../src/router";

type OpportunityView = components["schemas"]["OpportunityView"];
type ProvenanceSummary = components["schemas"]["domains__opportunities__schemas__ProvenanceSummary"];

const firstProvenance: ProvenanceSummary = {
  confirmed_at: "2026-08-09T02:06:00Z",
  confirmed_by: "employee-demo-one",
  extracted_at: "2026-08-09T01:42:00Z",
  extracted_by: "human",
  field_name: "spec_summary",
  page_hash: "hash-demo-one",
  source_id: "upload-demo-one",
  source_type: "upload",
  source_url: "https://evidence.invalid/demo-one",
};

const secondProvenance: ProvenanceSummary = {
  confirmed_at: "2026-08-09T04:30:00Z",
  confirmed_by: "employee-demo-two",
  extracted_at: "2026-08-09T04:00:00Z",
  extracted_by: "human-review",
  field_name: "spec_summary",
  page_hash: "hash-demo-two",
  source_id: "message-demo-two",
  source_type: "conversation",
  source_url: null,
};

const targetPriceProvenance: ProvenanceSummary = {
  confirmed_at: "2026-08-09T02:08:00Z",
  confirmed_by: "employee-demo-one",
  extracted_at: "2026-08-09T01:44:00Z",
  extracted_by: "human",
  field_name: "target_price",
  page_hash: "hash-demo-price",
  source_id: "quote-demo-price",
  source_type: "upload",
  source_url: null,
};

const firstOpportunity: OpportunityView = {
  account_id: "account-demo-one",
  account_name: "澄湾设备（演示）",
  can_source: true,
  country: "中国台湾",
  created_at: "2026-08-09T01:30:00Z",
  current_supply_problem: "需要确认户外涂层",
  destination: "高雄港",
  died_at_state: null,
  estimated_cost: { amount: "9007199254740993.1200", currency: "USD" },
  estimated_profit: { amount: "1234567890.0009", currency: "USD" },
  has_pending_handoff: false,
  loss_reason: null,
  need_id: "need-demo-one",
  next_action: "确认表面处理与包装要求",
  next_action_due: "2026-08-09T08:30:00Z",
  opportunity_id: "opportunity-demo-one",
  owner: "employee-demo-one",
  owner_name: "林岚（演示）",
  product_category: "工业铰链",
  provenance: [firstProvenance, targetPriceProvenance],
  quantity: 2400,
  required_by: "2026-10-01",
  score: {
    failed_gates: ["packaging_unknown"],
    gate_reasons: { packaging_unknown: "包装要求仍待客户确认" },
    passed_gates: ["validated_need", "reachable_contact"],
    rank_bucket: "unexpected-new-bucket",
    scored_at: "2026-08-09T02:10:00Z",
    scorer_version: "rules-demo-v1",
    sort_key: { evidence_rank: 7, supply_rank: 2, value_band: 3 },
  },
  spec_summary: "工业铰链，户外使用场景",
  state: "assigned",
  target_price: { amount: "12345678901234567890.0040", currency: "USD" },
};

const secondOpportunity: OpportunityView = {
  account_id: "account-demo-two",
  account_name: "远岚设施（演示）",
  can_source: null,
  country: "新加坡",
  created_at: "2026-08-08T06:20:00Z",
  current_supply_problem: "季度数量与时间尚未确认",
  destination: "新加坡港",
  died_at_state: null,
  estimated_cost: null,
  estimated_profit: null,
  has_pending_handoff: true,
  loss_reason: null,
  need_id: "need-demo-two",
  next_action: "补充季度数量与到货时间",
  next_action_due: "2026-08-10T02:00:00Z",
  opportunity_id: "opportunity-demo-two",
  owner: "employee-demo-two",
  owner_name: "周屿（演示）",
  product_category: "门控五金",
  provenance: [secondProvenance],
  quantity: 80,
  required_by: null,
  score: {
    failed_gates: ["quantity_and_timing"],
    gate_reasons: { quantity_and_timing: "季度数量与时间尚未确认" },
    passed_gates: ["validated_need"],
    rank_bucket: "high",
    scored_at: "2026-08-08T06:40:00Z",
    scorer_version: "rules-demo-v1",
    sort_key: { evidence_rank: 4, supply_rank: 1, value_band: 1 },
  },
  spec_summary: "商业设施门控维护",
  state: "contacted",
  target_price: null,
};

const lossReasons = [
  "unreachable",
  "no_reply",
  "need_not_real",
  "no_supply_found",
  "price_too_high",
  "lost_to_competitor",
  "customer_went_silent",
  "timing_mismatch",
  "compliance_blocked",
  "margin_too_low",
  "internal_no_capacity",
  "duplicate",
] as const;

function jsonResponse(body: unknown, status = 200, headers?: HeadersInit): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

function asRequest(input: URL | RequestInfo): Request {
  if (input instanceof Request) return input;
  throw new TypeError("fake transport requires the generated client to send a Request");
}

function deferred<T>(): {
  promise: Promise<T>;
  reject: (reason?: unknown) => void;
  resolve: (value: T) => void;
} {
  let reject!: (reason?: unknown) => void;
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

function opportunityForRequest(request: Request, records: OpportunityView[]): Response | undefined {
  const url = new URL(request.url);
  const match = url.pathname.match(/^\/crm\/opportunities\/([^/]+)$/);
  if (request.method === "GET" && match) {
    const record = records.find((item) => item.opportunity_id === match[1]);
    return record ? jsonResponse(record) : jsonResponse({ code: "not_found", message: "not found" }, 404);
  }
  return undefined;
}

function makeReadFetch(
  records: OpportunityView[] = [firstOpportunity, secondOpportunity],
): ReturnType<typeof vi.fn<typeof fetch>> {
  return vi.fn<typeof fetch>(async (input) => {
    const request = asRequest(input);
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/notifications") return jsonResponse([]);
    if (request.method === "GET" && url.pathname === "/crm/opportunities") {
      return jsonResponse(records);
    }
    const detail = opportunityForRequest(request, records);
    return detail ?? jsonResponse({ code: "unexpected", message: "unexpected request" }, 500);
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

async function mountBoard(fetch: ReturnType<typeof vi.fn<typeof globalThis.fetch>>, expectedCount=2): Promise<{
  app: VueApp;
  root: HTMLElement;
}> {
  const root = document.createElement("div");
  document.body.replaceChildren(root);
  const client = createApiClient({ baseUrl: "https://tradeos.test", fetch });
  const app = createApp(App);
  app.provide("tradeos-api-client", client);
  app.use(router);
  app.mount(root);
  await router.replace("/crm/opportunities");
  await eventually(() => {
    expect(root.querySelectorAll('ol[aria-label="机会列表"] > li > button')).toHaveLength(expectedCount);
  });
  return { app, root };
}

function buttonNamed(root: ParentNode, name: string): HTMLButtonElement {
  const button = [...root.querySelectorAll("button")].find((candidate) => candidate.textContent?.includes(name));
  if (!(button instanceof HTMLButtonElement)) {
    throw new Error(`button not found: ${name}`);
  }
  return button;
}

afterEach(() => {
  document.body.replaceChildren();
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

describe("opportunity board", () => {
  it("registers the exact opportunity and handoff routes while leaving analytics unregistered", () => {
    const opportunityRoute = router.resolve("/crm/opportunities");
    const handoffRoute = router.resolve("/crm/handoffs");
    const analyticsRoute = router.resolve("/crm/analytics/loss-reasons");

    expect(opportunityRoute.matched.map((route) => route.path)).toContain("/crm/opportunities");
    expect(handoffRoute.matched.map((route) => route.path)).toContain("/crm/handoffs");
    expect(analyticsRoute.matched).toHaveLength(0);
  });

  it("keeps backend order, selects the first response item, and displays generated values without numeric conversion", async () => {
    vi.stubEnv("VITE_TENANT_ID", "tenant-demo");
    vi.stubEnv("VITE_EMPLOYEE_ID", "employee-demo");
    const fetch = makeReadFetch();
    const { app, root } = await mountBoard(fetch);

    const cards = [...root.querySelectorAll('ol[aria-label="机会列表"] > li > button')];
    expect(cards.map((card) => card.textContent)).toEqual([
      expect.stringContaining("澄湾设备（演示）"),
      expect.stringContaining("远岚设施（演示）"),
    ]);
    expect(cards[0]?.getAttribute("aria-current")).toBe("true");
    expect((cards[0] as HTMLElement).dataset.opportunityId).toBe("opportunity-demo-one");
    expect(root.querySelector("article")?.textContent).toContain("澄湾设备（演示）");
    expect(root.textContent).not.toContain("演示数据");
    expect(root.textContent).toContain("排序桶：未知");
    expect(root.textContent).toContain("12345678901234567890.0040 USD");
    expect(root.textContent).toContain("9007199254740993.1200 USD");

    await eventually(() => {
      expect(fetch).toHaveBeenCalledTimes(3);
    });
    const requests = fetch.mock.calls.map(([request]) => asRequest(request));
    expect(requests.map((request) => new URL(request.url).pathname)).toEqual([
      "/notifications",
      "/crm/opportunities",
      "/crm/opportunities/opportunity-demo-one",
    ]);
    for (const request of requests) {
      expect(request.headers.get("x-tenant-id")).toBe("tenant-demo");
      expect(request.headers.get("x-employee-id")).toBe("employee-demo");
      expect(request.headers.has("x-role")).toBe(false);
      expect(request.headers.has("x-scope")).toBe(false);
    }

    app.unmount();
  });

  it("replaces detail, fact, and provenance state when a different backend record is selected", async () => {
    const fetch = makeReadFetch();
    const { app, root } = await mountBoard(fetch);
    const cards = root.querySelectorAll('ol[aria-label="机会列表"] > li > button');

    (cards[1] as HTMLButtonElement).click();
    await eventually(() => {
      expect(root.querySelector("article")?.textContent).toContain("商业设施门控维护");
    });
    expect(root.querySelector("article")?.textContent).not.toContain("工业铰链，户外使用场景");
    expect(root.querySelector("article")?.textContent).toContain("来源摘要暂不可用");

    buttonNamed(root.querySelector("article")!, "查看来源").click();
    const dialog = root.querySelector('[role="dialog"]');
    await eventually(() => {
      expect(dialog?.textContent).toContain("message-demo-two");
    });
    expect(dialog?.textContent).not.toContain("upload-demo-one");
    expect(dialog?.textContent).toContain("employee-demo-two");

    app.unmount();
  });

  it("shows all six provenance fields and traps focus until Escape returns it to the trigger", async () => {
    const openSpy = vi.spyOn(window, "open");
    const { app, root } = await mountBoard(makeReadFetch());
    const trigger = buttonNamed(root.querySelector("article")!, "查看来源");

    trigger.focus();
    trigger.click();
    const dialog = root.querySelector('[role="dialog"]') as HTMLElement;
    await eventually(() => {
      expect(dialog.hidden).toBe(false);
      expect(document.activeElement?.getAttribute("aria-label")).toBe("关闭来源");
    });
    for (const visibleText of [
      "来源类型",
      "upload",
      "来源标识",
      "upload-demo-one",
      "提取者",
      "human",
      "提取时间",
      "2026-08-09T01:42:00Z",
      "确认人",
      "employee-demo-one",
      "确认时间",
      "2026-08-09T02:06:00Z",
    ]) {
      expect(dialog.textContent).toContain(visibleText);
    }
    expect(openSpy).not.toHaveBeenCalled();

    const close = root.querySelector('[aria-label="关闭来源"]') as HTMLButtonElement;
    const openSource = buttonNamed(dialog, "打开来源");
    openSource.focus();
    openSource.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key: "Tab" }));
    expect(document.activeElement).toBe(close);
    close.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key: "Tab", shiftKey: true }));
    expect(document.activeElement).toBe(openSource);
    dialog.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key: "Escape" }));
    await eventually(() => {
      expect(dialog.hidden).toBe(true);
      expect(document.activeElement).toBe(trigger);
    });

    app.unmount();
  });

  it("exposes account and country provenance when they are the only sourced facts", async () => {
    const accountProvenance: ProvenanceSummary = {
      confirmed_at: null,
      confirmed_by: null,
      extracted_at: "2026-08-10T02:00:00Z",
      extracted_by: "human",
      field_name: "account_name",
      page_hash: null,
      source_id: "e2e-message-identity",
      source_type: "conversation",
      source_url: null,
    };
    const countryProvenance: ProvenanceSummary = {
      ...accountProvenance,
      field_name: "country",
      source_id: "e2e-message-country",
    };
    const identityOnly: OpportunityView = {
      ...firstOpportunity,
      can_source: null,
      current_supply_problem: null,
      destination: null,
      estimated_cost: null,
      estimated_profit: null,
      provenance: [accountProvenance, countryProvenance],
      quantity: null,
      required_by: null,
      spec_summary: null,
      target_price: null,
    };
    const { app, root } = await mountBoard(
      makeReadFetch([identityOnly, { ...secondOpportunity, provenance: [] }]),
    );
    const article = root.querySelector("article")!;

    const sourceButtons = [...article.querySelectorAll("button")].filter((button) =>
      button.textContent?.includes("查看来源"),
    ) as HTMLButtonElement[];
    expect(sourceButtons).toHaveLength(2);

    const accountTrigger = sourceButtons[0]!;
    accountTrigger.focus();
    accountTrigger.click();
    const accountDialog = article.querySelector('[role="dialog"]') as HTMLElement;
    await eventually(() => {
      expect(accountDialog.hidden).toBe(false);
      expect(document.activeElement?.getAttribute("aria-label")).toBe("关闭来源");
    });
    for (const visibleText of [
      "客户名称 · 来源记录",
      "来源类型",
      "客户会话",
      "来源标识",
      "e2e-message-identity",
      "提取者",
      "human",
      "提取时间",
      "2026-08-10T02:00:00Z",
      "确认人",
      "尚未确认",
      "确认时间",
    ]) {
      expect(accountDialog.textContent).toContain(visibleText);
    }
    accountDialog.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key: "Escape" }));
    await eventually(() => {
      expect(accountDialog.hidden).toBe(true);
      expect(document.activeElement).toBe(accountTrigger);
    });

    sourceButtons[1]!.click();
    const dialogs = article.querySelectorAll<HTMLElement>('[role="dialog"]');
    await eventually(() => {
      expect(dialogs[1]?.hidden).toBe(false);
      expect(dialogs[1]?.textContent).toContain("国家 / 地区 · 来源记录");
      expect(dialogs[1]?.textContent).toContain("e2e-message-country");
    });
    app.unmount();
  });

  it("locks both write entries, ignores duplicate writes, and refetches instead of optimistically changing state", async () => {
    let finishWrite: (() => void) | undefined;
    let transitioned = false;
    const requests: Array<{ body: string; method: string; path: string }> = [];
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const request = asRequest(input);
      const url = new URL(request.url);
      requests.push({ body: await request.clone().text(), method: request.method, path: url.pathname });
      const updated = { ...firstOpportunity, state: "contacted" };
      const records = [transitioned ? updated : firstOpportunity, secondOpportunity];
      if (request.method === "GET" && url.pathname === "/crm/opportunities") {
        return jsonResponse(records);
      }
      const detail = opportunityForRequest(request, records);
      if (detail) return detail;
      if (request.method === "POST" && url.pathname.endsWith("/transition")) {
        return new Promise<Response>((resolve) => {
          finishWrite = () => {
            transitioned = true;
            resolve(jsonResponse({}));
          };
        });
      }
      return jsonResponse({ code: "unexpected", message: "unexpected request" }, 500);
    });
    const { app, root } = await mountBoard(fetch);
    const transition = buttonNamed(root, "推进状态");
    const markLost = buttonNamed(root, "确认标记流失");

    transition.click();
    transition.click();
    markLost.click();
    await eventually(() => {
      expect(requests.filter((request) => request.method === "POST")).toHaveLength(1);
    });
    expect(transition.disabled).toBe(true);
    expect(markLost.disabled).toBe(true);
    expect(root.querySelector("article")?.textContent).toContain("已分配");
    expect(requests.find((request) => request.method === "POST")).toEqual({
      body: JSON.stringify({ target: "contacted" }),
      method: "POST",
      path: "/crm/opportunities/opportunity-demo-one/transition",
    });

    finishWrite?.();
    await eventually(() => {
      expect(requests.filter((request) => request.method === "GET" && request.path === "/crm/opportunities")).toHaveLength(2);
      expect(
        requests.filter(
          (request) => request.method === "GET" && request.path === "/crm/opportunities/opportunity-demo-one",
        ),
      ).toHaveLength(2);
      expect(root.querySelector("article")?.textContent).toContain("已联系");
    });
    expect(buttonNamed(root, "推进状态").disabled).toBe(false);
    expect(buttonNamed(root, "确认标记流失").disabled).toBe(false);

    app.unmount();
  });

  it("submits one exact public LossReason and renders a fixed 409 without leaking its payload", async () => {
    let postedBody = "";
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const request = asRequest(input);
      const url = new URL(request.url);
      if (request.method === "GET" && url.pathname === "/crm/opportunities") {
        return jsonResponse([firstOpportunity, secondOpportunity]);
      }
      const detail = opportunityForRequest(request, [firstOpportunity, secondOpportunity]);
      if (detail) return detail;
      if (request.method === "POST" && url.pathname.endsWith("/mark-lost")) {
        postedBody = await request.clone().text();
        return jsonResponse(
          { code: "conflict_for_opportunity-demo-one", message: "secret customer content" },
          409,
        );
      }
      return jsonResponse({ code: "unexpected", message: "unexpected request" }, 500);
    });
    const { app, root } = await mountBoard(fetch);
    const reason = root.querySelector('select[aria-label="流失原因"]') as HTMLSelectElement;
    const detail = root.querySelector('textarea[aria-label="补充说明（可选）"]') as HTMLTextAreaElement;

    expect([...reason.options].map((option) => option.value)).toEqual(lossReasons);
    reason.value = "margin_too_low";
    reason.dispatchEvent(new Event("change", { bubbles: true }));
    detail.value = "演示说明";
    detail.dispatchEvent(new Event("input", { bubbles: true }));
    buttonNamed(root, "确认标记流失").click();

    await eventually(() => {
      expect(root.textContent).toContain("当前状态不允许此操作");
    });
    expect(JSON.parse(postedBody)).toEqual({ detail: "演示说明", reason: "margin_too_low" });
    expect(root.textContent).not.toContain("conflict_for_opportunity-demo-one");
    expect(root.textContent).not.toContain("secret customer content");

    app.unmount();
  });

  it("clears protected detail on 403 and never renders the server error body", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const request = asRequest(input);
      const url = new URL(request.url);
      if (request.method === "GET" && url.pathname === "/crm/opportunities") {
        return jsonResponse([firstOpportunity, secondOpportunity]);
      }
      if (request.method === "GET" && url.pathname.endsWith("opportunity-demo-two")) {
        return jsonResponse({ code: "forbidden-opportunity-demo-two", message: "private evidence url" }, 403);
      }
      return opportunityForRequest(request, [firstOpportunity]) ?? jsonResponse({}, 500);
    });
    const { app, root } = await mountBoard(fetch);
    const cards = root.querySelectorAll('ol[aria-label="机会列表"] > li > button');

    (cards[1] as HTMLButtonElement).click();
    await eventually(() => {
      expect(root.textContent).toContain("没有权限");
    });
    expect(root.querySelector("article")).toBeNull();
    expect(root.textContent).not.toContain("工业铰链，户外使用场景");
    expect(root.textContent).not.toContain("forbidden-opportunity-demo-two");
    expect(root.textContent).not.toContain("private evidence url");

    app.unmount();
  });

  it("keeps the last successful list stale after 503 and only offers a manual Retry-After retry", async () => {
    let listRequests = 0;
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const request = asRequest(input);
      const url = new URL(request.url);
      if (request.method === "GET" && url.pathname === "/crm/opportunities") {
        listRequests += 1;
        if (listRequests === 1) return jsonResponse([firstOpportunity, secondOpportunity]);
        return jsonResponse(
          { code: "service-secret-opportunity-demo-one", message: "private customer content" },
          503,
          { "retry-after": "120" },
        );
      }
      return opportunityForRequest(request, [firstOpportunity, secondOpportunity]) ?? jsonResponse({}, 500);
    });
    const { app, root } = await mountBoard(fetch);

    buttonNamed(root, "刷新机会列表").click();
    await eventually(() => {
      expect(root.textContent).toContain("数据可能已过期");
      expect(root.textContent).toContain("服务暂时不可用");
      expect(root.textContent).toContain("120 秒后可手工重试");
    });
    expect(root.textContent).toContain("澄湾设备（演示）");
    expect(root.textContent).not.toContain("service-secret-opportunity-demo-one");
    expect(root.textContent).not.toContain("private customer content");
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(listRequests).toBe(2);

    app.unmount();
  });

  it("keeps list Retry-After guidance out of an existing forbidden detail error", async () => {
    let listRequests = 0;
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const request = asRequest(input);
      const url = new URL(request.url);
      if (request.method === "GET" && url.pathname === "/crm/opportunities") {
        listRequests += 1;
        if (listRequests === 1) return jsonResponse([firstOpportunity, secondOpportunity]);
        return jsonResponse({ code: "hidden", message: "hidden" }, 503, { "retry-after": "120" });
      }
      if (request.method === "GET" && url.pathname.endsWith("opportunity-demo-two")) {
        return jsonResponse({ code: "forbidden", message: "forbidden" }, 403);
      }
      return opportunityForRequest(request, [firstOpportunity]) ?? jsonResponse({}, 500);
    });
    const { app, root } = await mountBoard(fetch);

    const cards = root.querySelectorAll('ol[aria-label="机会列表"] > li > button');
    (cards[1] as HTMLButtonElement).click();
    await eventually(() => expect(root.querySelector('[aria-label="机会详情"]')?.textContent).toContain("没有权限"));
    buttonNamed(root, "刷新机会列表").click();
    await eventually(() => expect(root.querySelector('[aria-labelledby="board-title"]')?.textContent).toContain("120 秒后可手工重试"));

    expect(root.querySelector('[aria-label="机会详情"]')?.textContent).not.toContain("120 秒后可手工重试");
    app.unmount();
  });

  it.each([
    [400, "请求参数无效"],
    [503, "服务暂时不可用"],
    [500, "请求未完成，请刷新后重试"],
  ])("maps a %i list failure to fixed safe copy", async (status, safeCopy) => {
    const fetch = vi.fn<typeof globalThis.fetch>(async () =>
      jsonResponse({ code: `dynamic-${status}`, message: `customer content ${status}` }, status),
    );
    const root = document.createElement("div");
    document.body.replaceChildren(root);
    const app = createApp(App);
    app.provide("tradeos-api-client", createApiClient({ baseUrl: "https://tradeos.test", fetch }));
    app.use(router);
    app.mount(root);
    await router.replace("/crm/opportunities");

    await eventually(() => {
      expect(root.textContent).toContain(safeCopy);
    });
    expect(root.textContent).not.toContain(`dynamic-${status}`);
    expect(root.textContent).not.toContain(`customer content ${status}`);
    app.unmount();
  });

  it.each(["transition", "mark-lost"] as const)(
    "keeps a fixed %s 403 visible after clearing the protected detail",
    async (action) => {
      const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
        const request = asRequest(input);
        const url = new URL(request.url);
        if (request.method === "GET" && url.pathname === "/crm/opportunities") {
          return jsonResponse([firstOpportunity, secondOpportunity]);
        }
        const detail = opportunityForRequest(request, [firstOpportunity, secondOpportunity]);
        if (detail) return detail;
        if (request.method === "POST" && url.pathname.endsWith(`/${action}`)) {
          return jsonResponse(
            { code: `forbidden-${action}-secret`, message: `private ${action} customer content` },
            403,
          );
        }
        return jsonResponse({ code: "unexpected", message: "unexpected request" }, 500);
      });
      const { app, root } = await mountBoard(fetch);

      buttonNamed(root, action === "transition" ? "推进状态" : "确认标记流失").click();

      await eventually(() => {
        expect(root.querySelector('[aria-label="机会详情"]')?.textContent).toContain("没有权限");
      });
      expect(root.querySelector("article")).toBeNull();
      expect(root.textContent).not.toContain(`forbidden-${action}-secret`);
      expect(root.textContent).not.toContain(`private ${action} customer content`);
      app.unmount();
    },
  );

  it.each(["older-200", "older-503"] as const)(
    "ignores an %s list response that finishes after a newer successful refresh",
    async (olderResult) => {
      const pendingLists: Array<ReturnType<typeof deferred<Response>>> = [];
      let listRequests = 0;
      const newestFirst = { ...firstOpportunity, account_name: "最新机会列表（演示）" };
      const olderFirst = { ...firstOpportunity, account_name: "过期机会列表（演示）" };
      const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
        const request = asRequest(input);
        const url = new URL(request.url);
        if (request.method === "GET" && url.pathname === "/crm/opportunities") {
          listRequests += 1;
          if (listRequests === 1) return jsonResponse([firstOpportunity, secondOpportunity]);
          const pending = deferred<Response>();
          pendingLists.push(pending);
          return pending.promise;
        }
        return opportunityForRequest(request, [firstOpportunity, secondOpportunity])
          ?? jsonResponse({ code: "unexpected", message: "unexpected request" }, 500);
      });
      const { app, root } = await mountBoard(fetch);

      buttonNamed(root, "刷新机会列表").click();
      buttonNamed(root, "刷新机会列表").click();
      await eventually(() => expect(pendingLists).toHaveLength(2));
      pendingLists[1]?.resolve(jsonResponse([newestFirst, secondOpportunity]));
      await eventually(() => {
        expect(root.querySelector('[aria-labelledby="board-title"]')?.textContent).toContain("最新机会列表（演示）");
      });

      pendingLists[0]?.resolve(
        olderResult === "older-200"
          ? jsonResponse([olderFirst, secondOpportunity])
          : jsonResponse({ code: "old-service-secret", message: "old private content" }, 503),
      );
      await new Promise((resolve) => setTimeout(resolve, 20));

      const listPane = root.querySelector('[aria-labelledby="board-title"]');
      expect(listPane?.textContent).toContain("最新机会列表（演示）");
      expect(listPane?.textContent).not.toContain("过期机会列表（演示）");
      expect(listPane?.textContent).not.toContain("服务暂时不可用");
      expect(listPane?.textContent).not.toContain("数据可能已过期");
      app.unmount();
    },
  );

  it("does not let a pre-write list read overwrite the mandatory post-write refresh", async () => {
    const pendingLists: Array<ReturnType<typeof deferred<Response>>> = [];
    let listRequests = 0;
    let transitioned = false;
    const updatedFirst = {
      ...firstOpportunity,
      account_name: "写后最新机会（演示）",
      state: "contacted" as const,
    };
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const request = asRequest(input);
      const url = new URL(request.url);
      if (request.method === "GET" && url.pathname === "/crm/opportunities") {
        listRequests += 1;
        if (listRequests === 1) return jsonResponse([firstOpportunity, secondOpportunity]);
        const pending = deferred<Response>();
        pendingLists.push(pending);
        return pending.promise;
      }
      if (request.method === "POST" && url.pathname.endsWith("/transition")) {
        transitioned = true;
        return jsonResponse({});
      }
      const records = transitioned ? [updatedFirst, secondOpportunity] : [firstOpportunity, secondOpportunity];
      return opportunityForRequest(request, records)
        ?? jsonResponse({ code: "unexpected", message: "unexpected request" }, 500);
    });
    const { app, root } = await mountBoard(fetch);

    buttonNamed(root, "刷新机会列表").click();
    buttonNamed(root, "推进状态").click();
    await eventually(() => expect(pendingLists).toHaveLength(2));

    pendingLists[1]?.resolve(jsonResponse([updatedFirst, secondOpportunity]));
    await eventually(() => {
      expect(root.querySelector('[aria-labelledby="board-title"]')?.textContent).toContain("写后最新机会（演示）");
      expect(root.textContent).toContain("状态已按后端最新结果刷新");
    });
    pendingLists[0]?.resolve(jsonResponse([firstOpportunity, secondOpportunity]));
    await new Promise((resolve) => setTimeout(resolve, 20));

    const listPane = root.querySelector('[aria-labelledby="board-title"]');
    expect(listPane?.textContent).toContain("写后最新机会（演示）");
    expect(listPane?.textContent).not.toContain("澄湾设备（演示）");
    app.unmount();
  });

  it.each([
    ["list", "response", "服务暂时不可用"],
    ["detail", "response", "服务暂时不可用"],
    ["list", "throw", "请求未完成，请刷新后重试"],
    ["detail", "throw", "请求未完成，请刷新后重试"],
  ] as const)(
    "reports a successful write with a failed %s refresh (%s) as incomplete",
    async (failedRead, failureMode, safeCopy) => {
      let listRequests = 0;
      let detailRequests = 0;
      let transitioned = false;
      const updatedFirst = { ...firstOpportunity, state: "contacted" as const };
      const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
        const request = asRequest(input);
        const url = new URL(request.url);
        if (request.method === "GET" && url.pathname === "/crm/opportunities") {
          listRequests += 1;
          if (listRequests === 1) return jsonResponse([firstOpportunity, secondOpportunity]);
          if (failedRead === "list") {
            if (failureMode === "throw") throw new Error("private thrown list content");
            return jsonResponse({ code: "refresh-list-secret", message: "private list content" }, 503);
          }
          return jsonResponse([updatedFirst, secondOpportunity]);
        }
        if (request.method === "GET" && url.pathname.endsWith("opportunity-demo-one")) {
          detailRequests += 1;
          if (failedRead === "detail" && detailRequests === 2) {
            if (failureMode === "throw") throw new Error("private thrown detail content");
            return jsonResponse({ code: "refresh-detail-secret", message: "private detail content" }, 503);
          }
          return jsonResponse(transitioned ? updatedFirst : firstOpportunity);
        }
        if (request.method === "POST" && url.pathname.endsWith("/transition")) {
          transitioned = true;
          return jsonResponse({});
        }
        return jsonResponse({ code: "unexpected", message: "unexpected request" }, 500);
      });
      const { app, root } = await mountBoard(fetch);

      buttonNamed(root, "推进状态").click();
      await eventually(() => {
        expect(root.textContent).toContain("写入成功，但刷新未完成，请手工重试");
      });

      expect(root.textContent).toContain(safeCopy);
      expect(root.textContent).not.toContain("状态已按后端最新结果刷新");
      expect(root.textContent).not.toContain(`refresh-${failedRead}-secret`);
      expect(root.textContent).not.toContain(`private ${failedRead} content`);
      expect(root.textContent).not.toContain(`private thrown ${failedRead} content`);
      app.unmount();
    },
  );

  it.each(["list", "detail"] as const)(
    "clears the incomplete-write warning after a current successful manual %s recovery",
    async (failedRead) => {
      let listRequests = 0;
      let detailRequests = 0;
      let transitioned = false;
      const updatedFirst = { ...firstOpportunity, state: "contacted" as const };
      const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
        const request = asRequest(input);
        const url = new URL(request.url);
        if (request.method === "GET" && url.pathname === "/crm/opportunities") {
          listRequests += 1;
          if (failedRead === "list" && listRequests === 2) {
            return jsonResponse({ code: "manual-list-secret", message: "private list content" }, 503);
          }
          return jsonResponse(transitioned ? [updatedFirst, secondOpportunity] : [firstOpportunity, secondOpportunity]);
        }
        if (request.method === "GET" && url.pathname.endsWith("opportunity-demo-one")) {
          detailRequests += 1;
          if (failedRead === "detail" && detailRequests === 2) {
            return jsonResponse({ code: "manual-detail-secret", message: "private detail content" }, 503);
          }
          return jsonResponse(transitioned ? updatedFirst : firstOpportunity);
        }
        if (request.method === "POST" && url.pathname.endsWith("/transition")) {
          transitioned = true;
          return jsonResponse({});
        }
        return jsonResponse({ code: "unexpected", message: "unexpected request" }, 500);
      });
      const { app, root } = await mountBoard(fetch);

      buttonNamed(root, "推进状态").click();
      await eventually(() => {
        expect(root.textContent).toContain("写入成功，但刷新未完成，请手工重试");
        expect(root.textContent).toContain("服务暂时不可用");
      });

      const retryScope = failedRead === "list"
        ? root.querySelector('[aria-labelledby="board-title"]')!
        : root.querySelector('[aria-label="机会详情"]')!;
      buttonNamed(retryScope, "重试").click();

      await eventually(() => {
        expect(root.textContent).not.toContain("写入成功，但刷新未完成，请手工重试");
        expect(root.textContent).toContain("已通过手工重试获取后端最新结果");
        expect(root.querySelector("article")?.textContent).toContain("已联系");
      });
      expect(root.textContent).not.toContain(`manual-${failedRead}-secret`);
      expect(root.textContent).not.toContain(`private ${failedRead} content`);
      app.unmount();
    },
  );

  it("keeps list-only recovery outstanding when its current bundled detail read fails", async () => {
    let listRequests = 0;
    let detailRequests = 0;
    let transitioned = false;
    const updatedFirst = { ...firstOpportunity, state: "contacted" as const };
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const request = asRequest(input);
      const url = new URL(request.url);
      if (request.method === "GET" && url.pathname === "/crm/opportunities") {
        listRequests += 1;
        if (listRequests === 2) {
          return jsonResponse({ code: "list-only-secret", message: "private list-only content" }, 503);
        }
        return jsonResponse(transitioned ? [updatedFirst, secondOpportunity] : [firstOpportunity, secondOpportunity]);
      }
      if (request.method === "GET" && url.pathname.endsWith("opportunity-demo-one")) {
        detailRequests += 1;
        if (detailRequests === 3) {
          return jsonResponse({ code: "bundled-detail-secret", message: "private bundled detail content" }, 503);
        }
        return jsonResponse(transitioned ? updatedFirst : firstOpportunity);
      }
      if (request.method === "POST" && url.pathname.endsWith("/transition")) {
        transitioned = true;
        return jsonResponse({});
      }
      return jsonResponse({ code: "unexpected", message: "unexpected request" }, 500);
    });
    const { app, root } = await mountBoard(fetch);

    buttonNamed(root, "推进状态").click();
    await eventually(() => {
      expect(root.textContent).toContain("写入成功，但刷新未完成，请手工重试");
      expect(root.querySelector('[aria-labelledby="board-title"]')?.textContent).toContain("服务暂时不可用");
      expect(root.querySelector("article")?.textContent).toContain("已联系");
    });

    buttonNamed(root.querySelector('[aria-labelledby="board-title"]')!, "重试").click();
    await eventually(() => {
      expect(root.querySelector('[aria-label="机会详情"]')?.textContent).toContain("服务暂时不可用");
      expect(root.textContent).toContain("写入成功，但刷新未完成，请手工重试");
    });
    expect(root.textContent).not.toContain("已通过手工重试获取后端最新结果");

    buttonNamed(root.querySelector('[aria-label="机会详情"]')!, "重试").click();
    await eventually(() => {
      expect(root.textContent).not.toContain("写入成功，但刷新未完成，请手工重试");
      expect(root.textContent).toContain("已通过手工重试获取后端最新结果");
      expect(root.querySelector("article")?.textContent).toContain("已联系");
    });
    expect(listRequests).toBe(3);
    expect(detailRequests).toBe(4);
    expect(root.textContent).not.toContain("list-only-secret");
    expect(root.textContent).not.toContain("bundled-detail-secret");
    app.unmount();
  });

  it("keeps the incomplete-write warning until both failed post-write read channels recover", async () => {
    let listRequests = 0;
    let detailRequests = 0;
    let transitioned = false;
    const updatedFirst = { ...firstOpportunity, state: "contacted" as const };
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const request = asRequest(input);
      const url = new URL(request.url);
      if (request.method === "GET" && url.pathname === "/crm/opportunities") {
        listRequests += 1;
        if (listRequests === 2) {
          return jsonResponse({ code: "combined-list-secret", message: "private combined list content" }, 503);
        }
        return jsonResponse(transitioned ? [updatedFirst, secondOpportunity] : [firstOpportunity, secondOpportunity]);
      }
      if (request.method === "GET" && url.pathname.endsWith("opportunity-demo-one")) {
        detailRequests += 1;
        if (detailRequests === 2) {
          return jsonResponse({ code: "combined-detail-secret", message: "private combined detail content" }, 503);
        }
        return jsonResponse(transitioned ? updatedFirst : firstOpportunity);
      }
      if (request.method === "POST" && url.pathname.endsWith("/transition")) {
        transitioned = true;
        return jsonResponse({});
      }
      return jsonResponse({ code: "unexpected", message: "unexpected request" }, 500);
    });
    const { app, root } = await mountBoard(fetch);

    buttonNamed(root, "推进状态").click();
    await eventually(() => {
      expect(root.textContent).toContain("写入成功，但刷新未完成，请手工重试");
      expect(root.querySelector('[aria-labelledby="board-title"]')?.textContent).toContain("服务暂时不可用");
      expect(root.querySelector('[aria-label="机会详情"]')?.textContent).toContain("服务暂时不可用");
    });

    buttonNamed(root.querySelector('[aria-label="机会详情"]')!, "重试").click();
    await eventually(() => expect(root.querySelector("article")?.textContent).toContain("已联系"));
    expect(root.textContent).toContain("写入成功，但刷新未完成，请手工重试");
    expect(root.textContent).not.toContain("已通过手工重试获取后端最新结果");
    expect(root.querySelector('[aria-labelledby="board-title"]')?.textContent).toContain("服务暂时不可用");

    buttonNamed(root.querySelector('[aria-labelledby="board-title"]')!, "重试").click();
    await eventually(() => {
      expect(root.textContent).not.toContain("写入成功，但刷新未完成，请手工重试");
      expect(root.textContent).toContain("已通过手工重试获取后端最新结果");
      expect(root.querySelector("article")?.textContent).toContain("已联系");
    });
    expect(listRequests).toBe(3);
    expect(detailRequests).toBe(4);
    expect(root.textContent).not.toContain("combined-list-secret");
    expect(root.textContent).not.toContain("combined-detail-secret");
    expect(root.textContent).not.toContain("private combined list content");
    expect(root.textContent).not.toContain("private combined detail content");
    app.unmount();
  });

  it("renders complete opportunity identity, timeline, and pending-handoff fields", async () => {
    const { app, root } = await mountBoard(makeReadFetch());

    await eventually(() => {
      const article = root.querySelector("article");
      expect(article?.textContent).toContain("account-demo-one");
      expect(article?.textContent).toContain("need-demo-one");
      expect(article?.textContent).toContain("2026-08-09T01:30:00Z");
      expect(article?.textContent).toContain("无待处理接管");
    });

    const cards = root.querySelectorAll('ol[aria-label="机会列表"] > li > button');
    (cards[1] as HTMLButtonElement).click();
    await eventually(() => {
      const article = root.querySelector("article");
      expect(article?.textContent).toContain("account-demo-two");
      expect(article?.textContent).toContain("need-demo-two");
      expect(article?.textContent).toContain("2026-08-08T06:20:00Z");
      expect(article?.textContent).toContain("有待处理接管");
    });
    app.unmount();
  });

  it("keeps validated need facts out of the amber score and gate explanation", async () => {
    const { app, root } = await mountBoard(makeReadFetch());

    await eventually(() => expect(root.querySelector("article")).not.toBeNull());
    const factSection = root.querySelector('[aria-labelledby="facts-title"]');
    const inferenceSection = root.querySelector('[aria-labelledby="inference-title"]');
    expect(factSection?.textContent).toContain("需要确认户外涂层");
    expect(inferenceSection?.textContent).not.toContain("需要确认户外涂层");
    expect(inferenceSection?.textContent).toContain("包装要求仍待客户确认");
    expect(inferenceSection?.textContent).toContain("validated_need");
    app.unmount();
  });

  it("opens complete provenance for an amount whose field_name matches", async () => {
    const { app, root } = await mountBoard(makeReadFetch());

    await eventually(() => expect(root.querySelector('[aria-labelledby="amount-title"]')).not.toBeNull());
    const amountSection = root.querySelector('[aria-labelledby="amount-title"]')!;
    buttonNamed(amountSection, "查看来源").click();
    const dialog = amountSection.querySelector('[role="dialog"]') as HTMLElement;
    await eventually(() => {
      expect(dialog.hidden).toBe(false);
      expect(dialog.textContent).toContain("目标价格 · 来源记录");
      expect(dialog.textContent).toContain("quote-demo-price");
      expect(dialog.textContent).toContain("employee-demo-one");
    });
    app.unmount();
  });

  it("ignores a late detail response after the user selects another record", async () => {
    const secondDetail = deferred<Response>();
    let firstDetailRequests = 0;
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const request = asRequest(input);
      const url = new URL(request.url);
      if (request.method === "GET" && url.pathname === "/crm/opportunities") {
        return jsonResponse([firstOpportunity, secondOpportunity]);
      }
      if (request.method === "GET" && url.pathname.endsWith("opportunity-demo-one")) {
        firstDetailRequests += 1;
        return jsonResponse(firstOpportunity);
      }
      if (request.method === "GET" && url.pathname.endsWith("opportunity-demo-two")) {
        return secondDetail.promise;
      }
      return jsonResponse({ code: "unexpected", message: "unexpected request" }, 500);
    });
    const { app, root } = await mountBoard(fetch);

    const cards = root.querySelectorAll('ol[aria-label="机会列表"] > li > button');
    (cards[1] as HTMLButtonElement).click();
    (cards[0] as HTMLButtonElement).click();
    await eventually(() => {
      expect(firstDetailRequests).toBe(2);
      expect(root.querySelector("article")?.textContent).toContain("澄湾设备（演示）");
    });
    secondDetail.resolve(jsonResponse(secondOpportunity));
    await new Promise((resolve) => setTimeout(resolve, 20));

    expect(root.querySelector("article")?.textContent).toContain("澄湾设备（演示）");
    expect(root.querySelector("article")?.textContent).not.toContain("远岚设施（演示）");
    app.unmount();
  });

  it("refetches list and detail after a successful mark-lost response", async () => {
    let listRequests = 0;
    let detailRequests = 0;
    let markedLost = false;
    let postedBody = "";
    const lostFirst = {
      ...firstOpportunity,
      died_at_state: "assigned" as const,
      loss_reason: "margin_too_low" as const,
      state: "lost" as const,
    };
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const request = asRequest(input);
      const url = new URL(request.url);
      const records = [markedLost ? lostFirst : firstOpportunity, secondOpportunity];
      if (request.method === "GET" && url.pathname === "/crm/opportunities") {
        listRequests += 1;
        return jsonResponse(records);
      }
      if (request.method === "GET" && url.pathname.endsWith("opportunity-demo-one")) {
        detailRequests += 1;
        return jsonResponse(records[0]);
      }
      if (request.method === "POST" && url.pathname.endsWith("/mark-lost")) {
        postedBody = await request.clone().text();
        markedLost = true;
        return jsonResponse({});
      }
      return jsonResponse({ code: "unexpected", message: "unexpected request" }, 500);
    });
    const { app, root } = await mountBoard(fetch);
    const reason = root.querySelector('select[aria-label="流失原因"]') as HTMLSelectElement;
    reason.value = "margin_too_low";
    reason.dispatchEvent(new Event("change", { bubbles: true }));

    buttonNamed(root, "确认标记流失").click();
    await eventually(() => {
      expect(root.querySelector("article")?.textContent).toContain("已流失");
      expect(root.querySelector("article")?.textContent).toContain("利润空间过低");
      expect(listRequests).toBe(2);
      expect(detailRequests).toBe(2);
    });
    expect(JSON.parse(postedBody)).toEqual({ detail: null, reason: "margin_too_low" });
    app.unmount();
  });

  it.each([
    [400, "请求参数无效"],
    [500, "请求未完成，请刷新后重试"],
    ["throw", "请求未完成，请刷新后重试"],
  ] as const)("maps a %s transition failure to fixed action copy", async (failure, safeCopy) => {
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const request = asRequest(input);
      const url = new URL(request.url);
      if (request.method === "GET" && url.pathname === "/crm/opportunities") {
        return jsonResponse([firstOpportunity, secondOpportunity]);
      }
      const detail = opportunityForRequest(request, [firstOpportunity, secondOpportunity]);
      if (detail) return detail;
      if (request.method === "POST" && url.pathname.endsWith("/transition")) {
        if (failure === "throw") throw new Error("private thrown customer content");
        return jsonResponse({ code: `action-${failure}-secret`, message: "private action content" }, failure);
      }
      return jsonResponse({ code: "unexpected", message: "unexpected request" }, 500);
    });
    const { app, root } = await mountBoard(fetch);

    buttonNamed(root, "推进状态").click();
    await eventually(() => expect(root.textContent).toContain(safeCopy));
    expect(root.textContent).not.toContain(`action-${failure}-secret`);
    expect(root.textContent).not.toContain("private action content");
    expect(root.textContent).not.toContain("private thrown customer content");
    app.unmount();
  });

  it("keeps loss form input after an action 503 and isolates manual Retry-After guidance", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const request = asRequest(input);
      const url = new URL(request.url);
      if (request.method === "GET" && url.pathname === "/crm/opportunities") {
        return jsonResponse([firstOpportunity, secondOpportunity]);
      }
      const detail = opportunityForRequest(request, [firstOpportunity, secondOpportunity]);
      if (detail) return detail;
      if (request.method === "POST" && url.pathname.endsWith("/mark-lost")) {
        return jsonResponse({ code: "action-503-secret", message: "private action content" }, 503, {
          "retry-after": "45",
        });
      }
      return jsonResponse({ code: "unexpected", message: "unexpected request" }, 500);
    });
    const { app, root } = await mountBoard(fetch);
    const reason = root.querySelector('select[aria-label="流失原因"]') as HTMLSelectElement;
    const detail = root.querySelector('textarea[aria-label="补充说明（可选）"]') as HTMLTextAreaElement;
    reason.value = "timing_mismatch";
    reason.dispatchEvent(new Event("change", { bubbles: true }));
    detail.value = "保留这条演示输入";
    detail.dispatchEvent(new Event("input", { bubbles: true }));

    buttonNamed(root, "确认标记流失").click();
    await eventually(() => {
      expect(root.querySelector('[aria-label="机会详情"]')?.textContent).toContain("服务暂时不可用");
      expect(root.querySelector('[aria-label="机会详情"]')?.textContent).toContain("45 秒后可手工重试");
    });
    expect((root.querySelector('select[aria-label="流失原因"]') as HTMLSelectElement).value).toBe("timing_mismatch");
    expect((root.querySelector('textarea[aria-label="补充说明（可选）"]') as HTMLTextAreaElement).value).toBe(
      "保留这条演示输入",
    );
    expect(root.querySelector('[aria-labelledby="board-title"]')?.textContent).not.toContain("45 秒后可手工重试");
    expect(root.textContent).not.toContain("action-503-secret");
    expect(root.textContent).not.toContain("private action content");
    app.unmount();
  });

  it("offers no won transition from the negotiating terminal UI state", async () => {
    const negotiating = { ...firstOpportunity, state: "negotiating" as const };
    const { app, root } = await mountBoard(makeReadFetch([negotiating, secondOpportunity]));

    await eventually(() => expect(root.querySelector("article")?.textContent).toContain("洽谈中"));
    const targets = root.querySelector('select[aria-label="选择合法目标状态"]') as HTMLSelectElement;
    expect([...targets.options].map((option) => option.value)).not.toContain("won");
    expect(targets.disabled).toBe(true);
    expect(buttonNamed(root, "推进状态").disabled).toBe(true);
    expect(root.textContent).not.toContain("推进为：已成交");
    app.unmount();
  });
});

it("Task10 机会详情链接精确Need和当前机会成本", async () => {
  const { app, root } = await mountBoard(makeReadFetch());
  await eventually(() => expect(root.querySelector('.opportunity-record')).not.toBeNull());
  expect(root.querySelector(`a[href="/demand/needs/${firstOpportunity.need_id}"]`)).not.toBeNull();
  expect(root.querySelector(`a[href="/costing-quotes?opportunity_id=${firstOpportunity.opportunity_id}"]`)).not.toBeNull();
  app.unmount();
});

it("Task10 关键字段和金额有来源不代表已验证事实", async () => {
  const { app, root } = await mountBoard(makeReadFetch());
  await eventually(() => expect(root.querySelector('.opportunity-record')).not.toBeNull());
  const record = root.querySelector('.opportunity-record')!;
  expect(record.textContent).toContain("关键字段");
  expect([...record.querySelectorAll('.fact-label')].length).toBeGreaterThan(0);
  expect([...record.querySelectorAll('.fact-label')].every((label) => label.textContent?.includes("来源记录"))).toBe(true);
  expect(record.textContent).not.toContain("已验证事实");
  app.unmount();
});


it("来源链接读取精确机会，即使该对象不在当前列表", async () => {
  const requests:string[]=[];
  const fetch=vi.fn<typeof globalThis.fetch>(async input=>{
    const request=asRequest(input);requests.push(new URL(request.url).pathname);
    if(new URL(request.url).pathname==="/crm/opportunities")return jsonResponse([firstOpportunity]);
    return opportunityForRequest(request,[firstOpportunity,secondOpportunity])??jsonResponse({},500);
  });
  const {app,root}=await mountBoard(fetch,1);
  await router.push("/crm/opportunities?opportunity="+secondOpportunity.opportunity_id);
  await eventually(()=>expect(requests).toContain("/crm/opportunities/"+secondOpportunity.opportunity_id));
  await eventually(()=>expect(root.textContent).toContain(secondOpportunity.spec_summary!));
  app.unmount();
});
