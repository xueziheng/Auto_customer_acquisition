import { createApp, nextTick, type App as VueApp } from "vue";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "../src/App.vue";
import { createApiClient } from "../src/api/client";
import type { components } from "../src/api/api";
import router from "../src/router";

type HandoffPacketView = components["schemas"]["HandoffPacketView"];
type HandoffQueueItemView = components["schemas"]["HandoffQueueItemView"];
type Money = components["schemas"]["Money"];
type OpportunityView = components["schemas"]["OpportunityView"];
type ProvenanceSummary = components["schemas"]["ProvenanceSummary"];
type ScoreExplanation = components["schemas"]["ScoreExplanation"];
type SortKey = components["schemas"]["SortKey"];

const firstQueueItem: HandoffQueueItemView = {
  account_name: "澄湾设备（演示）",
  assigned_to: "employee-demo-one",
  country: "中国台湾",
  customer_verbatim: "QUEUE-ONLY-PRIVATE-ONE",
  evidence_links: ["evidence-private-one", "evidence-private-two"],
  handoff_id: "handoff-demo-one",
  missing_information: ["表面处理", "包装方式"],
  opportunity_id: "opportunity-demo-one",
  requested_at: "2026-08-09T01:30:00Z",
  state: "requested",
  suggested_next_step: "确认表面处理与包装要求",
  trigger: "客户请求报价",
  wait_seconds: 5058,
  why_valuable: "客户主动要求真人确认规格可行性",
};

const secondQueueItem: HandoffQueueItemView = {
  account_name: "远岚设施（演示）",
  assigned_to: "employee-demo-two",
  country: "新加坡",
  customer_verbatim: "QUEUE-ONLY-PRIVATE-TWO",
  evidence_links: ["evidence-private-three"],
  handoff_id: "handoff-demo-two",
  missing_information: ["季度数量", "到货时间", "包装标记"],
  opportunity_id: "opportunity-demo-two",
  requested_at: "2026-08-09T02:15:00Z",
  state: "requested",
  suggested_next_step: "补充季度数量与到货时间",
  trigger: "收到规格文件",
  wait_seconds: 2527,
  why_valuable: "客户上传了应用资料并请求补充沟通",
};

const firstPacket: HandoffPacketView = {
  account_name: "澄湾设备（演示）",
  already_sent: ["产品目录（演示）"],
  assigned_to_name: "林岚（演示）",
  commitments_made: ["无商业承诺"],
  conversation_summary: "客户上传规格后请求人工跟进。",
  country: "中国台湾",
  customer_verbatim: "PACKET-ONLY-CUSTOMER-ONE",
  evidence_links: ["packet-evidence-private-one", "packet-evidence-private-two"],
  handoff_id: "handoff-demo-one",
  how_we_found_them: "客户回复",
  missing_information: ["表面处理", "包装方式"],
  opportunity_id: "opportunity-demo-one",
  requested_at: "2026-08-09T01:30:00Z",
  state: "requested",
  suggested_next_step: "确认表面处理与包装要求",
  trigger: "客户请求报价",
  validated_need_summary: "工业铰链，户外使用场景，目的地为高雄港",
  wait_seconds: 5058,
  why_valuable: "客户主动要求真人确认规格可行性",
};

const secondPacket: HandoffPacketView = {
  account_name: "远岚设施（演示）",
  already_sent: ["基础资料（演示）"],
  assigned_to_name: "周屿（演示）",
  commitments_made: ["尚未承诺交期"],
  conversation_summary: "客户已确认使用场景，数量和时间仍待补充。",
  country: "新加坡",
  customer_verbatim: "PACKET-ONLY-CUSTOMER-TWO",
  evidence_links: ["packet-evidence-private-three"],
  handoff_id: "handoff-demo-two",
  how_we_found_them: "客户上传",
  missing_information: ["季度数量", "到货时间", "包装标记"],
  opportunity_id: "opportunity-demo-two",
  requested_at: "2026-08-09T02:15:00Z",
  state: "requested",
  suggested_next_step: "补充季度数量与到货时间",
  trigger: "收到规格文件",
  validated_need_summary: "商业设施门控维护需求",
  wait_seconds: 2527,
  why_valuable: "客户上传了应用资料并请求补充沟通",
};

const firstProvenance: ProvenanceSummary = {
  confirmed_at: "2026-08-09T02:06:00Z",
  confirmed_by: "employee-demo-one",
  extracted_at: "2026-08-09T01:42:00Z",
  extracted_by: "human",
  field_name: "spec_summary",
  page_hash: "hash-demo-one",
  source_id: "upload-demo-one",
  source_type: "upload",
  source_url: null,
};

const nestedMoney: Money = {
  amount: "123.45",
  currency: "USD",
};

const nestedSortKey: SortKey = {
  evidence_rank: 6,
  supply_rank: 2,
  value_band: 4,
};

const nestedScore: ScoreExplanation = {
  failed_gates: [],
  gate_reasons: {},
  passed_gates: ["verified_contact"],
  rank_bucket: "high",
  scored_at: "2026-08-09T02:00:00Z",
  scorer_version: "scoring-demo-v1",
  sort_key: nestedSortKey,
};

const firstOpportunity: OpportunityView = {
  account_id: "account-demo-one",
  account_name: "澄湾设备（演示）",
  can_source: true,
  country: "中国台湾",
  created_at: "2026-08-09T01:00:00Z",
  current_supply_problem: "需要确认户外涂层",
  destination: "高雄港",
  died_at_state: null,
  estimated_cost: null,
  estimated_profit: null,
  has_pending_handoff: true,
  loss_reason: null,
  need_id: "need-demo-one",
  next_action: "确认表面处理与包装要求",
  next_action_due: "2026-08-09T08:30:00Z",
  opportunity_id: "opportunity-demo-one",
  owner: "employee-demo-one",
  owner_name: "林岚（演示）",
  product_category: "工业铰链",
  provenance: [firstProvenance],
  quantity: 2400,
  required_by: "2026-10-01",
  score: null,
  spec_summary: "工业铰链，户外使用场景",
  state: "assigned",
  target_price: null,
};

const secondOpportunity: OpportunityView = {
  ...firstOpportunity,
  account_id: "account-demo-two",
  account_name: "远岚设施（演示）",
  country: "新加坡",
  need_id: "need-demo-two",
  opportunity_id: "opportunity-demo-two",
  owner: "employee-demo-two",
  owner_name: "周屿（演示）",
  product_category: "门控五金",
  provenance: [],
  quantity: 80,
  spec_summary: "商业设施门控维护",
};

const packetWithNullableAndAbsentOptionals: HandoffPacketView = {
  account_name: firstPacket.account_name,
  assigned_to_name: null,
  conversation_summary: null,
  country: firstPacket.country,
  customer_verbatim: firstPacket.customer_verbatim,
  handoff_id: firstPacket.handoff_id,
  how_we_found_them: null,
  opportunity_id: firstPacket.opportunity_id,
  requested_at: firstPacket.requested_at,
  state: firstPacket.state,
  suggested_next_step: null,
  trigger: firstPacket.trigger,
  validated_need_summary: null,
  wait_seconds: null,
  why_valuable: firstPacket.why_valuable,
};

const opportunityWithNestedValues: OpportunityView = {
  account_id: firstOpportunity.account_id,
  account_name: firstOpportunity.account_name,
  can_source: null,
  country: firstOpportunity.country,
  created_at: firstOpportunity.created_at,
  current_supply_problem: null,
  destination: null,
  died_at_state: null,
  estimated_cost: nestedMoney,
  estimated_profit: nestedMoney,
  has_pending_handoff: firstOpportunity.has_pending_handoff,
  loss_reason: null,
  need_id: firstOpportunity.need_id,
  opportunity_id: firstOpportunity.opportunity_id,
  owner: null,
  owner_name: null,
  product_category: firstOpportunity.product_category,
  provenance: [firstProvenance],
  quantity: null,
  required_by: null,
  score: nestedScore,
  spec_summary: null,
  state: firstOpportunity.state,
  target_price: nestedMoney,
};

function jsonResponse(body: unknown, status = 200, headers?: HeadersInit): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

function emptyResponse(status = 204, headers?: HeadersInit): Response {
  return new Response(null, { status, headers });
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

function queueCard(root: ParentNode, handoffId: string): HTMLButtonElement {
  const card = root.querySelector(`button[data-handoff-id="${handoffId}"]`);
  if (!(card instanceof HTMLButtonElement)) throw new Error(`queue card not found: ${handoffId}`);
  return card;
}

function buttonNamed(root: ParentNode, name: string): HTMLButtonElement {
  const button = [...root.querySelectorAll("button")].find((candidate) => candidate.textContent?.includes(name));
  if (!(button instanceof HTMLButtonElement)) throw new Error(`button not found: ${name}`);
  return button;
}

async function mountQueue(fetch: ReturnType<typeof vi.fn<typeof globalThis.fetch>>): Promise<{
  app: VueApp;
  root: HTMLElement;
}> {
  const root = document.createElement("div");
  document.body.replaceChildren(root);
  const app = createApp(App);
  app.provide("tradeos-api-client", createApiClient({ baseUrl: "https://tradeos.test", fetch }));
  app.use(router);
  app.mount(root);
  await router.replace("/crm/handoffs");
  await eventually(() => {
    expect(root.querySelectorAll('ol[aria-label="最久等待接管队列"] > li > button')).toHaveLength(2);
  });
  return { app, root };
}

function readFetch(
  queue: HandoffQueueItemView[] = [firstQueueItem, secondQueueItem],
  packets: HandoffPacketView[] = [firstPacket, secondPacket],
  opportunities: OpportunityView[] = [firstOpportunity, secondOpportunity],
): ReturnType<typeof vi.fn<typeof globalThis.fetch>> {
  return vi.fn<typeof globalThis.fetch>(async (input) => {
    const request = asRequest(input);
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/crm/handoffs") return jsonResponse(queue);
    const handoffMatch = url.pathname.match(/^\/crm\/handoffs\/([^/]+)$/);
    if (request.method === "GET" && handoffMatch) {
      return jsonResponse(packets.find((item) => item.handoff_id === handoffMatch[1]) ?? {}, 200);
    }
    const opportunityMatch = url.pathname.match(/^\/crm\/opportunities\/([^/]+)$/);
    if (request.method === "GET" && opportunityMatch) {
      return jsonResponse(opportunities.find((item) => item.opportunity_id === opportunityMatch[1]) ?? {}, 200);
    }
    return jsonResponse({ code: "unexpected", message: "server private message" }, 500);
  });
}

function withoutField(record: object, fieldName: string): Record<string, unknown> {
  const result = { ...record } as Record<string, unknown>;
  delete result[fieldName];
  return result;
}

function untrustedCombinationFetch(
  untrustedPacket: unknown,
  untrustedOpportunity: unknown,
): ReturnType<typeof vi.fn<typeof globalThis.fetch>> {
  return vi.fn<typeof globalThis.fetch>(async (input) => {
    const request = asRequest(input);
    const path = new URL(request.url).pathname;
    if (request.method === "GET" && path === "/crm/handoffs") return jsonResponse([firstQueueItem, secondQueueItem]);
    if (request.method === "GET" && path === "/crm/handoffs/handoff-demo-one") return jsonResponse(untrustedPacket);
    if (request.method === "GET" && path === "/crm/opportunities/opportunity-demo-one") {
      return jsonResponse(untrustedOpportunity);
    }
    return jsonResponse({}, 500);
  });
}

afterEach(() => {
  document.body.replaceChildren();
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

describe("handoff queue", () => {
  it("registers the exact handoff route, sends limit=50, preserves backend order, and loads the first packet", async () => {
    vi.stubEnv("VITE_TENANT_ID", "tenant-demo");
    vi.stubEnv("VITE_EMPLOYEE_ID", "employee-demo");
    const fetch = readFetch();
    const { app, root } = await mountQueue(fetch);

    const cards = [...root.querySelectorAll('ol[aria-label="最久等待接管队列"] > li > button')];
    expect(router.resolve("/crm/handoffs").matched.map((route) => route.path)).toContain("/crm/handoffs");
    expect(cards.map((card) => card.textContent)).toEqual([
      expect.stringContaining("澄湾设备（演示）"),
      expect.stringContaining("远岚设施（演示）"),
    ]);
    expect(cards[0]?.getAttribute("aria-current")).toBe("true");
    await eventually(() => expect(root.textContent).toContain("PACKET-ONLY-CUSTOMER-ONE"));

    const requests = fetch.mock.calls.map(([input]) => asRequest(input));
    expect(new URL(requests[0]!.url).pathname).toBe("/crm/handoffs");
    expect(new URL(requests[0]!.url).searchParams.get("limit")).toBe("50");
    expect(requests.map((request) => new URL(request.url).pathname)).toEqual([
      "/crm/handoffs",
      "/crm/handoffs/handoff-demo-one",
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

  it("keeps private queue fields out of cards and confines value and next step to the inference panel", async () => {
    const { app, root } = await mountQueue(readFetch());
    const firstCard = queueCard(root, "handoff-demo-one");

    expect(firstCard.textContent).toContain("客户请求报价");
    expect(firstCard.textContent).toContain("employee-demo-one");
    expect(firstCard.textContent).toContain("2026-08-09T01:30:00Z");
    expect(firstCard.textContent).toContain("状态 requested");
    expect(firstCard.textContent).toContain("推断 / 价值与建议");
    expect(firstCard.textContent).toContain("缺失信息 2 项");
    expect(firstCard.textContent).toContain("证据入口 2 项");
    expect(firstCard.textContent).not.toContain("QUEUE-ONLY-PRIVATE-ONE");
    expect(firstCard.textContent).not.toContain("evidence-private-one");
    expect(firstCard.textContent).not.toContain("林岚（演示）");
    expect(firstCard.querySelector(".handoff-inference")?.textContent).toContain("客户主动要求真人确认规格可行性");
    expect(firstCard.querySelector(".handoff-fact")).toBeNull();
    app.unmount();
  });

  it("maps a packet only after its matching opportunity response, including all packet fields and real provenance", async () => {
    const { app, root } = await mountQueue(readFetch());
    await eventually(() => expect(root.textContent).toContain("PACKET-ONLY-CUSTOMER-ONE"));

    const packet = root.querySelector("article[aria-label=\"完整接管包\"]");
    for (const value of [
      "handoff-demo-one",
      "opportunity-demo-one",
      "客户请求报价",
      "requested",
      "2026-08-09T01:30:00Z",
      "5058",
      "林岚（演示）",
      "客户回复",
      "PACKET-ONLY-CUSTOMER-ONE",
      "工业铰链，户外使用场景，目的地为高雄港",
      "客户上传规格后请求人工跟进。",
      "产品目录（演示）",
      "无商业承诺",
      "工业铰链",
    ]) {
      expect(packet?.textContent).toContain(value);
    }
    expect(packet?.textContent).toContain("证据入口 2 项（受权限保护）");
    expect(packet?.textContent).not.toContain("packet-evidence-private-one");
    const provenanceButton = buttonNamed(packet!, "查看来源");
    provenanceButton.focus();
    provenanceButton.click();
    const dialog = root.querySelector('[role="dialog"]') as HTMLElement;
    await eventually(() => {
      expect(dialog.hidden).toBe(false);
      expect(dialog.textContent).toContain("upload-demo-one");
      expect(dialog.textContent).toContain("employee-demo-one");
      expect(document.activeElement?.getAttribute("aria-label")).toBe("关闭来源");
    });
    dialog.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key: "Escape" }));
    await eventually(() => expect(document.activeElement).toBe(provenanceButton));
    app.unmount();
  });

  it("fails closed when packet and opportunity IDs do not form one matching opportunity", async () => {
    const mismatchedPacket: HandoffPacketView = { ...firstPacket, opportunity_id: "opportunity-not-in-queue" };
    const fetch = readFetch([firstQueueItem, secondQueueItem], [mismatchedPacket, secondPacket]);
    const { app, root } = await mountQueue(fetch);

    await eventually(() => expect(root.textContent).toContain("请求未完成，请刷新后重试"));
    expect(root.textContent).not.toContain("PACKET-ONLY-CUSTOMER-ONE");
    expect(buttonNamed(root, "接受接管").disabled).toBe(true);
    app.unmount();
  });

  it("fences stale packet and opportunity reads so a rapid selection cannot overwrite the newest selection", async () => {
    const firstPacketResponse = deferred<Response>();
    const firstOpportunityResponse = deferred<Response>();
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const request = asRequest(input);
      const path = new URL(request.url).pathname;
      if (request.method === "GET" && path === "/crm/handoffs") return jsonResponse([firstQueueItem, secondQueueItem]);
      if (request.method === "GET" && path === "/crm/handoffs/handoff-demo-one") return firstPacketResponse.promise;
      if (request.method === "GET" && path === "/crm/handoffs/handoff-demo-two") return jsonResponse(secondPacket);
      if (request.method === "GET" && path === "/crm/opportunities/opportunity-demo-one") return firstOpportunityResponse.promise;
      if (request.method === "GET" && path === "/crm/opportunities/opportunity-demo-two") return jsonResponse(secondOpportunity);
      return jsonResponse({}, 500);
    });
    const { app, root } = await mountQueue(fetch);

    queueCard(root, "handoff-demo-two").click();
    await eventually(() => expect(root.textContent).toContain("PACKET-ONLY-CUSTOMER-TWO"));
    firstPacketResponse.resolve(jsonResponse(firstPacket));
    firstOpportunityResponse.resolve(jsonResponse(firstOpportunity));
    await eventually(() => {
      expect(root.textContent).toContain("PACKET-ONLY-CUSTOMER-TWO");
      expect(root.textContent).not.toContain("PACKET-ONLY-CUSTOMER-ONE");
    });
    app.unmount();
  });

  it("clears protected packet data on 403 and enters unavailable on 503 without leaking server bodies", async () => {
    let firstPacketRequest = true;
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const request = asRequest(input);
      const path = new URL(request.url).pathname;
      if (request.method === "GET" && path === "/crm/handoffs") return jsonResponse([firstQueueItem, secondQueueItem]);
      if (request.method === "GET" && path === "/crm/handoffs/handoff-demo-one" && firstPacketRequest) {
        firstPacketRequest = false;
        return jsonResponse({ code: "private-handoff-one", message: "PACKET-ONLY-CUSTOMER-ONE" }, 403);
      }
      if (request.method === "GET" && path === "/crm/handoffs/handoff-demo-one") return jsonResponse(firstPacket);
      if (request.method === "GET" && path === "/crm/opportunities/opportunity-demo-one") return jsonResponse(firstOpportunity);
      if (request.method === "GET" && path === "/crm/handoffs/handoff-demo-two") {
        return jsonResponse({ code: "server-private-two", message: "PACKET-ONLY-CUSTOMER-TWO" }, 503, { "retry-after": "12" });
      }
      return jsonResponse({}, 500);
    });
    const { app, root } = await mountQueue(fetch);

    await eventually(() => expect(root.textContent).toContain("没有权限"));
    expect(root.textContent).not.toContain("private-handoff-one");
    expect(root.textContent).not.toContain("PACKET-ONLY-CUSTOMER-ONE");
    expect(queueCard(root, "handoff-demo-two").disabled).toBe(true);

    buttonNamed(root, "手工重试").click();
    await eventually(() => expect(queueCard(root, "handoff-demo-two").disabled).toBe(false));
    queueCard(root, "handoff-demo-two").click();
    await eventually(() => expect(root.textContent).toContain("服务暂时不可用，请稍后重试"));
    expect(root.textContent).toContain("12 秒后可手工重试");
    expect(root.textContent).not.toContain("server-private-two");
    expect(root.textContent).not.toContain("PACKET-ONLY-CUSTOMER-TWO");
    expect(queueCard(root, "handoff-demo-one").disabled).toBe(true);
    app.unmount();
  });

  it("posts once without a body, locks the captured item, then removes only it and refreshes before selecting its next index", async () => {
    let resolveAccept: (() => void) | undefined;
    const queueAfterAccept = [secondQueueItem];
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const request = asRequest(input);
      const path = new URL(request.url).pathname;
      if (request.method === "GET" && path === "/crm/handoffs") {
        return jsonResponse(resolveAccept ? queueAfterAccept : [firstQueueItem, secondQueueItem]);
      }
      if (request.method === "GET" && path === "/crm/handoffs/handoff-demo-one") return jsonResponse(firstPacket);
      if (request.method === "GET" && path === "/crm/handoffs/handoff-demo-two") return jsonResponse(secondPacket);
      if (request.method === "GET" && path === "/crm/opportunities/opportunity-demo-one") return jsonResponse(firstOpportunity);
      if (request.method === "GET" && path === "/crm/opportunities/opportunity-demo-two") return jsonResponse(secondOpportunity);
      if (request.method === "POST" && path === "/crm/handoffs/handoff-demo-one/accept") {
        return new Promise<Response>((resolve) => {
          resolveAccept = () => resolve(emptyResponse());
        });
      }
      return jsonResponse({}, 500);
    });
    const { app, root } = await mountQueue(fetch);
    const accept = buttonNamed(root, "接受接管");

    accept.click();
    accept.click();
    await eventually(() => expect(fetch.mock.calls.filter(([input]) => asRequest(input).method === "POST")).toHaveLength(1));
    expect(queueCard(root, "handoff-demo-one").disabled).toBe(true);
    expect(queueCard(root, "handoff-demo-two").disabled).toBe(true);
    const post = asRequest(fetch.mock.calls.find(([input]) => asRequest(input).method === "POST")![0]);
    expect(new URL(post.url).pathname).toBe("/crm/handoffs/handoff-demo-one/accept");
    expect(await post.clone().text()).toBe("");

    resolveAccept?.();
    await eventually(() => {
      expect(root.textContent).toContain("已接受接管");
      expect(root.querySelectorAll('ol[aria-label="最久等待接管队列"] > li > button')).toHaveLength(1);
      expect(queueCard(root, "handoff-demo-two").getAttribute("aria-current")).toBe("true");
      expect(root.textContent).toContain("PACKET-ONLY-CUSTOMER-TWO");
      expect(buttonNamed(root, "接受接管").disabled).toBe(false);
    });
    await eventually(() => expect(document.activeElement).toBe(queueCard(root, "handoff-demo-two")));
    expect(fetch.mock.calls.map(([input]) => asRequest(input)).filter((request) => new URL(request.url).pathname === "/crm/handoffs")).toHaveLength(2);
    app.unmount();
  });

  it("handles 409 as one normal concurrent result and preserves the captured removal if refresh fails", async () => {
    let queueReads = 0;
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const request = asRequest(input);
      const path = new URL(request.url).pathname;
      if (request.method === "GET" && path === "/crm/handoffs") {
        queueReads += 1;
        return queueReads === 1
          ? jsonResponse([firstQueueItem, secondQueueItem])
          : jsonResponse({ code: "private-refresh-failure", message: "PACKET-ONLY-CUSTOMER-ONE" }, 503);
      }
      if (request.method === "GET" && path === "/crm/handoffs/handoff-demo-one") return jsonResponse(firstPacket);
      if (request.method === "GET" && path === "/crm/opportunities/opportunity-demo-one") return jsonResponse(firstOpportunity);
      if (request.method === "POST" && path === "/crm/handoffs/handoff-demo-one/accept") {
        return jsonResponse({ code: "already-accepted-private", message: "PACKET-ONLY-CUSTOMER-ONE" }, 409);
      }
      return jsonResponse({}, 500);
    });
    const { app, root } = await mountQueue(fetch);

    buttonNamed(root, "接受接管").click();
    await eventually(() => expect(root.textContent).toContain("已被接受"));
    expect(fetch.mock.calls.filter(([input]) => asRequest(input).method === "POST")).toHaveLength(1);
    expect(root.querySelector('button[data-handoff-id="handoff-demo-one"]')).toBeNull();
    expect(root.textContent).toContain("刷新未完成");
    expect(root.textContent).not.toContain("already-accepted-private");
    expect(root.textContent).not.toContain("PACKET-ONLY-CUSTOMER-ONE");
    expect(buttonNamed(root, "接受接管").disabled).toBe(true);
    app.unmount();
  });

  it("keeps the last authorized packet but blocks a second accept after an unexpected HTTP response until manual recovery", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const request = asRequest(input);
      const path = new URL(request.url).pathname;
      if (request.method === "GET" && path === "/crm/handoffs") return jsonResponse([firstQueueItem, secondQueueItem]);
      if (request.method === "GET" && path === "/crm/handoffs/handoff-demo-one") return jsonResponse(firstPacket);
      if (request.method === "GET" && path === "/crm/opportunities/opportunity-demo-one") return jsonResponse(firstOpportunity);
      if (request.method === "POST" && path === "/crm/handoffs/handoff-demo-one/accept") {
        return jsonResponse({ code: "private-accept-failure", message: "PACKET-ONLY-CUSTOMER-ONE" }, 500);
      }
      return jsonResponse({}, 500);
    });
    const { app, root } = await mountQueue(fetch);

    buttonNamed(root, "接受接管").click();
    await eventually(() => {
      expect(root.textContent).toContain("请求未完成，请刷新后重试");
      expect(root.textContent).toContain("PACKET-ONLY-CUSTOMER-ONE");
      expect(buttonNamed(root, "接受接管").disabled).toBe(true);
    });
    buttonNamed(root, "接受接管").click();
    expect(fetch.mock.calls.filter(([input]) => asRequest(input).method === "POST")).toHaveLength(1);

    buttonNamed(root, "手工重试").click();
    await eventually(() => expect(buttonNamed(root, "接受接管").disabled).toBe(false));
    app.unmount();
  });

  it("fails closed for every required packet and opportunity field plus malformed optional DTO shapes", async () => {
    const packetRequiredFields = [
      "account_name",
      "country",
      "customer_verbatim",
      "handoff_id",
      "opportunity_id",
      "requested_at",
      "state",
      "trigger",
      "why_valuable",
    ];
    const opportunityRequiredFields = [
      "account_id",
      "account_name",
      "country",
      "created_at",
      "has_pending_handoff",
      "need_id",
      "opportunity_id",
      "product_category",
      "state",
    ];
    const malformedCombinations: Array<{ opportunity: unknown; packet: unknown }> = [
      ...packetRequiredFields.map((fieldName) => ({
        opportunity: firstOpportunity,
        packet: withoutField(firstPacket, fieldName),
      })),
      ...opportunityRequiredFields.map((fieldName) => ({
        opportunity: withoutField(firstOpportunity, fieldName),
        packet: firstPacket,
      })),
      {
        opportunity: firstOpportunity,
        packet: { ...firstPacket, already_sent: ["产品目录（演示）", 42] },
      },
      {
        opportunity: { ...firstOpportunity, has_pending_handoff: "true" },
        packet: firstPacket,
      },
      {
        opportunity: { ...firstOpportunity, quantity: "2400" },
        packet: firstPacket,
      },
      {
        opportunity: {
          ...firstOpportunity,
          provenance: [{ ...firstProvenance, confirmed_at: 1 }],
        },
        packet: firstPacket,
      },
    ];

    for (const malformed of malformedCombinations) {
      const { app, root } = await mountQueue(untrustedCombinationFetch(malformed.packet, malformed.opportunity));
      await eventually(() => expect(root.textContent).toContain("请求未完成，请刷新后重试"));
      expect(root.textContent).not.toContain("PACKET-ONLY-CUSTOMER-ONE");
      expect(buttonNamed(root, "接受接管").disabled).toBe(true);
      app.unmount();
    }
  });

  it("accepts complete nested DTO values while allowing generated nullable and absent optionals", async () => {
    const { app, root } = await mountQueue(
      untrustedCombinationFetch(packetWithNullableAndAbsentOptionals, opportunityWithNestedValues),
    );

    await eventually(() => expect(root.textContent).toContain("PACKET-ONLY-CUSTOMER-ONE"));
    expect(root.textContent).toContain("工业铰链");
    expect(buttonNamed(root, "接受接管").disabled).toBe(false);
    app.unmount();
  });

  it("fails closed when a nested Money, ProvenanceSummary, ScoreExplanation, or SortKey required key is absent", async () => {
    const rejectMalformedOpportunity = async (opportunity: unknown): Promise<void> => {
      const { app, root } = await mountQueue(untrustedCombinationFetch(firstPacket, opportunity));
      await eventually(() => expect(root.textContent).toContain("请求未完成，请刷新后重试"));
      expect(root.textContent).not.toContain("PACKET-ONLY-CUSTOMER-ONE");
      expect(buttonNamed(root, "接受接管").disabled).toBe(true);
      app.unmount();
    };

    for (const missingKey of ["amount", "currency"]) {
      await rejectMalformedOpportunity({
        ...opportunityWithNestedValues,
        estimated_cost: withoutField(nestedMoney, missingKey),
      });
    }

    for (const missingKey of [
      "confirmed_at",
      "confirmed_by",
      "extracted_at",
      "extracted_by",
      "field_name",
      "page_hash",
      "source_id",
      "source_type",
      "source_url",
    ]) {
      await rejectMalformedOpportunity({
        ...opportunityWithNestedValues,
        provenance: [withoutField(firstProvenance, missingKey)],
      });
    }

    for (const missingKey of [
      "failed_gates",
      "gate_reasons",
      "passed_gates",
      "rank_bucket",
      "scored_at",
      "scorer_version",
      "sort_key",
    ]) {
      await rejectMalformedOpportunity({
        ...opportunityWithNestedValues,
        score: withoutField(nestedScore, missingKey),
      });
    }

    for (const missingKey of ["evidence_rank", "supply_rank", "value_band"]) {
      await rejectMalformedOpportunity({
        ...opportunityWithNestedValues,
        score: { ...nestedScore, sort_key: withoutField(nestedSortKey, missingKey) },
      });
    }
  });

  it("fails closed for representative nullable packet and optional opportunity wrong shapes", async () => {
    const malformedCombinations = [
      {
        opportunity: firstOpportunity,
        packet: { ...firstPacket, assigned_to_name: 1 },
      },
      {
        opportunity: firstOpportunity,
        packet: { ...firstPacket, wait_seconds: "5058" },
      },
      {
        opportunity: { ...firstOpportunity, can_source: "true" },
        packet: firstPacket,
      },
      {
        opportunity: { ...firstOpportunity, owner_name: 1 },
        packet: firstPacket,
      },
    ];

    for (const malformed of malformedCombinations) {
      const { app, root } = await mountQueue(untrustedCombinationFetch(malformed.packet, malformed.opportunity));
      await eventually(() => expect(root.textContent).toContain("请求未完成，请刷新后重试"));
      expect(root.textContent).not.toContain("PACKET-ONLY-CUSTOMER-ONE");
      expect(buttonNamed(root, "接受接管").disabled).toBe(true);
      app.unmount();
    }
  });

  it("keeps the fixed 503 reason assertive after a 204 post-write refresh failure", async () => {
    let queueReads = 0;
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const request = asRequest(input);
      const path = new URL(request.url).pathname;
      if (request.method === "GET" && path === "/crm/handoffs") {
        queueReads += 1;
        return queueReads === 1
          ? jsonResponse([firstQueueItem, secondQueueItem])
          : jsonResponse({ code: "private-refresh-failure", message: "PACKET-ONLY-CUSTOMER-ONE" }, 503, { "retry-after": "9" });
      }
      if (request.method === "GET" && path === "/crm/handoffs/handoff-demo-one") return jsonResponse(firstPacket);
      if (request.method === "GET" && path === "/crm/opportunities/opportunity-demo-one") return jsonResponse(firstOpportunity);
      if (request.method === "POST" && path === "/crm/handoffs/handoff-demo-one/accept") return emptyResponse();
      return jsonResponse({}, 500);
    });
    const { app, root } = await mountQueue(fetch);

    buttonNamed(root, "接受接管").click();
    await eventually(() => {
      const live = root.querySelector(".live-region");
      expect(root.textContent).toContain("服务暂时不可用，请稍后重试");
      expect(root.textContent).toContain("刷新未完成");
      expect(root.textContent).toContain("数据可能已过期");
      expect(root.textContent).toContain("9 秒后可手工重试");
      expect(live?.getAttribute("role")).toBe("alert");
      expect(live?.getAttribute("aria-live")).toBe("assertive");
      expect(live?.textContent).toContain("服务暂时不可用，请稍后重试");
    });
    expect(root.querySelector('button[data-handoff-id="handoff-demo-one"]')).toBeNull();
    expect(buttonNamed(root, "接受接管").disabled).toBe(true);
    app.unmount();
  });

  it("keeps the fixed unexpected reason assertive after a 204 post-write refresh failure", async () => {
    let queueReads = 0;
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const request = asRequest(input);
      const path = new URL(request.url).pathname;
      if (request.method === "GET" && path === "/crm/handoffs") {
        queueReads += 1;
        return queueReads === 1
          ? jsonResponse([firstQueueItem, secondQueueItem])
          : jsonResponse({ code: "private-refresh-failure", message: "PACKET-ONLY-CUSTOMER-ONE" }, 500);
      }
      if (request.method === "GET" && path === "/crm/handoffs/handoff-demo-one") return jsonResponse(firstPacket);
      if (request.method === "GET" && path === "/crm/opportunities/opportunity-demo-one") return jsonResponse(firstOpportunity);
      if (request.method === "POST" && path === "/crm/handoffs/handoff-demo-one/accept") return emptyResponse();
      return jsonResponse({}, 500);
    });
    const { app, root } = await mountQueue(fetch);

    buttonNamed(root, "接受接管").click();
    await eventually(() => {
      const live = root.querySelector(".live-region");
      expect(root.textContent).toContain("请求未完成，请刷新后重试");
      expect(root.textContent).toContain("刷新未完成");
      expect(live?.getAttribute("role")).toBe("alert");
      expect(live?.getAttribute("aria-live")).toBe("assertive");
      expect(live?.textContent).toContain("请求未完成，请刷新后重试");
    });
    expect(root.querySelector('button[data-handoff-id="handoff-demo-one"]')).toBeNull();
    expect(buttonNamed(root, "接受接管").disabled).toBe(true);
    app.unmount();
  });
});
