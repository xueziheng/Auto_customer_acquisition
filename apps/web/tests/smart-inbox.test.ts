import { createApp, nextTick, type App as VueApp } from "vue";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { components } from "../src/api/api";
import { createApiClient } from "../src/api/client";
import App from "../src/App.vue";
import router from "../src/router";

type InboxItem = components["schemas"]["ConversationInboxItem"];
type InboxDetail = components["schemas"]["ConversationInboxDetail"];

const conversationId = "con_01K39P9M5D6K4A91YEQ80EJZ0X";
const messageId = "msg_01K39P9M5D6K4A91YEQ80EJZ0Y";

const item: InboxItem = {
  account_id: "acc_01K39P9M5D6K4A91YEQ80EJZ0Z",
  channel: "email",
  classified_at: "2026-08-21T10:01:00Z",
  classified_by: "reply-classifier:v3",
  conversation_id: conversationId,
  correction_count: 1,
  effective_category: "requests_quote",
  last_activity_at: "2026-08-21T10:00:00Z",
  latest_message_at: "2026-08-21T10:00:00Z",
  latest_message_id: messageId,
  original_category: "rejection",
  raw_artifact_ref: "artifact:reply-1",
  required_actions: ["stop_sequence", "handoff"],
};

const detail: InboxDetail = {
  account_id: item.account_id,
  channel: "email",
  conversation_id: conversationId,
  created_at: "2026-08-20T10:00:00Z",
  last_inbound_at: "2026-08-21T10:00:00Z",
  last_outbound_at: "2026-08-20T10:00:00Z",
  messages: [
    {
      classified_at: "2026-08-21T10:01:00Z",
      classified_by: "reply-classifier:v3",
      corrections: [
        {
          corrected_at: "2026-08-21T10:02:00Z",
          corrected_by: "emp-reviewer",
          corrected_category: "requests_quote",
        },
      ],
      direction: "inbound",
      effective_category: "requests_quote",
      message_id: messageId,
      original_category: "rejection",
      raw_artifact_ref: "artifact:reply-1",
      required_actions: ["stop_sequence", "handoff"],
      sent_at: "2026-08-21T10:00:00Z",
    },
  ],
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

function makeFetch(): {
  fetch: ReturnType<typeof vi.fn<typeof globalThis.fetch>>;
  corrections: Array<{ messageId: string; category: string }>;
} {
  const corrections: Array<{ messageId: string; category: string }> = [];
  const detailState = structuredClone(detail);
  const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
    const request = asRequest(input);
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/inbox/conversations") {
      return jsonResponse([item]);
    }
    if (
      request.method === "GET"
      && url.pathname === `/inbox/conversations/${conversationId}`
    ) {
      return jsonResponse(detailState);
    }
    if (
      request.method === "POST"
      && url.pathname === `/inbox/messages/${messageId}/correct-classification`
    ) {
      const body = await request.json() as { category: string };
      corrections.push({ messageId, category: body.category });
      detailState.messages[0]!.corrections.push({
        corrected_at: "2026-08-21T10:03:00Z",
        corrected_by: "emp-boss",
        corrected_category: body.category as components["schemas"]["ReplyCategory"],
      });
      detailState.messages[0]!.effective_category = body.category as components["schemas"]["ReplyCategory"];
      return jsonResponse({
        message_id: messageId,
        category: body.category,
        corrected_by: "emp-boss",
      });
    }
    return jsonResponse({ code: "unexpected", message: "unexpected" }, 500);
  });
  return { fetch, corrections };
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

async function mountInbox(
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
  await router.replace("/inbox");
  await new Promise((resolve) => setTimeout(resolve, 0));
  return { app, root };
}

afterEach(() => {
  router.replace("/");
});

describe("SmartInbox", () => {
  it("registers the route and keeps model judgement separate from correction", async () => {
    expect(router.getRoutes().map((route) => route.path)).toContain("/inbox");
    const { fetch } = makeFetch();
    const { root } = await mountInbox(fetch);

    await eventually(() => {
      expect(root.textContent).toContain("要求报价");
      expect(root.textContent).toContain("模型原判：拒绝");
      expect(root.textContent).toContain("reply-classifier:v3");
      expect(root.textContent).toContain("emp-reviewer");
    });
  });

  it("shows only metadata and artifact reference, never copied mail body", async () => {
    const { fetch } = makeFetch();
    const { root } = await mountInbox(fetch);

    await eventually(() => {
      expect(root.textContent).toContain("artifact:reply-1");
      expect(root.textContent).toContain("规则要求（不代表已执行）");
    });
    expect(root.textContent).not.toContain("邮件正文");
    expect(root.querySelector("[v-html]")).toBeNull();
  });

  it("submits a human correction through the typed endpoint", async () => {
    const { fetch, corrections } = makeFetch();
    const { root } = await mountInbox(fetch);
    await eventually(() => {
      expect(root.textContent).toContain("人工纠正分类");
    });
    const select = root.querySelector("select");
    const button = [...root.querySelectorAll("button")].find((candidate) =>
      candidate.textContent?.includes("提交纠正"),
    );
    expect(select).not.toBeNull();
    expect(button).toBeTruthy();
    (select as HTMLSelectElement).value = "unsubscribe";
    select?.dispatchEvent(new Event("change"));
    (button as HTMLButtonElement).click();

    await eventually(() => {
      expect(corrections).toEqual([
        { messageId, category: "unsubscribe" },
      ]);
      expect(root.textContent).toContain("人工纠正已记录");
      expect(root.textContent).toContain("emp-boss");
      expect(root.textContent).toContain("退订");
    });
  });
});
