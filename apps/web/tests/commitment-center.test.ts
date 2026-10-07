import { createApp, nextTick, type App as VueApp } from "vue";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { components } from "../src/api/api";
import { createApiClient } from "../src/api/client";
import App from "../src/App.vue";
import router from "../src/router";

type Commitment = components["schemas"]["CommitmentView"];

const pending: Commitment = {
  account_id: null,
  action: "发送更新报价",
  commitment_id: "com_01K39P9M5D6K4A91YEQ80EJZ0X",
  commitment_type: "employee",
  confirmed_at: null,
  confirmed_by: null,
  created_at: "2026-08-22T08:00:00Z",
  due_at: "2026-08-23T09:00:00+08:00",
  due_at_uncertain: true,
  escalated_at: null,
  extracted_by: "team-operations-v1",
  fulfilled_at: null,
  opportunity_id: null,
  owner: "emp_01K39P9M5D6K4A91YEQ80EJZ0Y",
  source_message_id: "msg_01K39P9M5D6K4A91YEQ80EJZ0Z",
  status: "pending",
  verbatim: "I will send the revised quotation tomorrow.",
};

const overdue: Commitment = {
  ...pending,
  action: "跟进客户确认数量",
  commitment_id: "com_01K39P9M5D6K4A91YEQ80EJZ10",
  commitment_type: "customer",
  confirmed_at: "2026-08-21T08:00:00Z",
  confirmed_by: pending.owner,
  due_at: "2026-08-21T09:00:00+08:00",
  due_at_uncertain: false,
  status: "overdue",
  verbatim: "We will confirm the quantity on Friday.",
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: status === 204 ? undefined : { "content-type": "application/json" },
  });
}

function asRequest(input: URL | RequestInfo): Request {
  if (input instanceof Request) return input;
  throw new TypeError("fake transport requires a Request");
}

function makeFetch(): {
  fetch: ReturnType<typeof vi.fn<typeof globalThis.fetch>>;
  confirmations: Array<{ id: string; correctedDueAt: string | null }>;
  fulfillments: string[];
} {
  const confirmations: Array<{ id: string; correctedDueAt: string | null }> = [];
  const fulfillments: string[] = [];
  const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
    const request = asRequest(input);
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/commitments") {
      return jsonResponse([pending, overdue]);
    }
    const confirm = url.pathname.match(/^\/commitments\/([^/]+)\/confirm$/);
    if (request.method === "POST" && confirm) {
      const body = await request.json() as { corrected_due_at: string | null };
      confirmations.push({
        id: confirm[1],
        correctedDueAt: body.corrected_due_at,
      });
      return jsonResponse(null, 204);
    }
    const fulfill = url.pathname.match(/^\/commitments\/([^/]+)\/fulfill$/);
    if (request.method === "POST" && fulfill) {
      fulfillments.push(fulfill[1]);
      return jsonResponse(null, 204);
    }
    return jsonResponse({ code: "unexpected", message: "unexpected" }, 500);
  });
  return { fetch, confirmations, fulfillments };
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

async function mountCenter(
  fetch: ReturnType<typeof vi.fn<typeof globalThis.fetch>>,
): Promise<{ app: VueApp; root: HTMLElement }> {
  const root = document.createElement("div");
  document.body.replaceChildren(root);
  const app = createApp(App);
  app.provide(
    "tradeos-api-client",
    createApiClient({ baseUrl: "https://tradeos.test", fetch }),
  );
  app.use(router);
  app.mount(root);
  await router.replace("/commitments");
  await new Promise((resolve) => setTimeout(resolve, 0));
  return { app, root };
}

afterEach(() => {
  router.replace("/");
});

describe("CommitmentCenter", () => {
  it("renders action, source verbatim and honest confirmation state", async () => {
    const { fetch } = makeFetch();
    const { root } = await mountCenter(fetch);

    await eventually(() => {
      expect(root.textContent).toContain("发送更新报价");
      expect(root.textContent).toContain("I will send the revised quotation tomorrow.");
      expect(root.textContent).toContain("待确认");
      expect(root.textContent).toContain("时间待核对");
      expect(root.textContent).toContain("已逾期");
    });
    expect(root.textContent).not.toContain("接口尚未装配");
  });

  it("confirms an uncertain due time through the typed endpoint", async () => {
    const { fetch, confirmations } = makeFetch();
    const { root } = await mountCenter(fetch);
    await eventually(() => {
      expect(root.textContent).toContain("发送更新报价");
    });
    const input = root.querySelector<HTMLInputElement>(
      `[data-commitment-id="${pending.commitment_id}"] input`,
    );
    const button = [...root.querySelectorAll("button")].find((candidate) =>
      candidate.textContent?.includes("确认承诺"),
    );
    expect(input).not.toBeNull();
    expect(button).toBeTruthy();
    if (input) {
      input.value = "2026-08-25T09:30";
      input.dispatchEvent(new Event("input"));
    }
    (button as HTMLButtonElement).click();

    await eventually(() => {
      expect(confirmations).toHaveLength(1);
      expect(confirmations[0]?.id).toBe(pending.commitment_id);
      expect(confirmations[0]?.correctedDueAt).toMatch(
        /^2026-08-25T09:30:00[+-]\d{2}:\d{2}$/,
      );
      expect(new Date(confirmations[0]?.correctedDueAt ?? "").getTime()).toBe(
        new Date("2026-08-25T09:30").getTime(),
      );
    });
  });

  it("allows fulfillment only for a confirmed open commitment", async () => {
    const { fetch, fulfillments } = makeFetch();
    const { root } = await mountCenter(fetch);
    await eventually(() => {
      expect(root.textContent).toContain("跟进客户确认数量");
    });
    const cards = [...root.querySelectorAll<HTMLElement>("[data-commitment-id]")];
    const pendingCard = cards.find(
      (card) => card.dataset.commitmentId === pending.commitment_id,
    );
    const overdueCard = cards.find(
      (card) => card.dataset.commitmentId === overdue.commitment_id,
    );
    expect(pendingCard?.textContent).not.toContain("标记已完成");
    const fulfill = [...(overdueCard?.querySelectorAll("button") ?? [])].find(
      (candidate) => candidate.textContent?.includes("标记已完成"),
    );
    expect(fulfill).toBeTruthy();
    (fulfill as HTMLButtonElement).click();

    await eventually(() => {
      expect(fulfillments).toEqual([overdue.commitment_id]);
    });
  });
});
