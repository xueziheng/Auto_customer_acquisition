import { createApp, nextTick, type App as VueApp } from "vue";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { components } from "../src/api/api";
import { createApiClient } from "../src/api/client";
import App from "../src/App.vue";
import router from "../src/router";

type InboxItem = components["schemas"]["ConversationInboxItem"];
type InboxDetail = components["schemas"]["ConversationInboxDetail"];

const conversationId = "con_01K39P9M5D6K4A91YEQ80EJZ0X";
const mountedApps: VueApp[] = [];
const workId = "rwa_01K39P9M5D6K4A91YEQ80EJZ0Y";
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
      outbound_message_id: "<tradeos.outbound@example.test>",
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

function makeFetch(options: { staleOnce?: boolean; workDelay?: Promise<void>; review?: boolean; outbound?: boolean; history?: boolean; work?: boolean; canHandle?: boolean; updateStatus?: number; questionsStatus?: number; recheckStatus?: number } = {}): {
  fetch: ReturnType<typeof vi.fn<typeof globalThis.fetch>>;
  corrections: Array<{ messageId: string; category: string }>;
  workUpdates: Array<{ expected_status: string; status: string }>;
} {
  const corrections: Array<{ messageId: string; category: string }> = [];
  const detailState = structuredClone(detail);
  if (options.review) {
    detailState.messages[0] = { ...detailState.messages[0]!, original_category: "needs_review", effective_category: "needs_review", corrections: [], required_actions: ["stop_sequence", "review_reply"] };
  }
  if (options.outbound) {
    detailState.last_inbound_at = null;
    detailState.messages[0] = { ...detailState.messages[0]!, direction: "outbound", effective_category: null, original_category: null, classified_by: null, classified_at: null, corrections: [], required_actions: [] };
  }
  if (options.history) {
    detailState.messages.unshift({ ...detailState.messages[0]!, message_id: "msg_old", direction: "inbound", sent_at: "2026-08-20T10:00:00Z" });
  }
  const workUpdates: Array<{ expected_status: string; status: string }> = [];
  let workStatus = "pending";
  let detailReads = 0;
  const work = {
    action_id: workId, message_id: messageId, outbound_message_id: "out_fixture",
    enrollment_id: "enr_fixture", account_id: item.account_id, contact_point_id: "cp_fixture",
    action: options.review ? "review_reply" : "start_qualification", owner_queue: options.review ? "reply_review" : "need_qualification", status: "pending",
    idempotency_key: "fixture", created_at: "2026-10-15T00:00:00Z", can_handle: options.canHandle !== false, history: [] as Array<Record<string, unknown>>,
  };
  const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
    const request = asRequest(input);
    const url = new URL(request.url);
    if (options.history && url.pathname === "/inbox/messages/msg_old/content") return jsonResponse({ subject: "Q01 old reply", sender: "buyer@example.test", recipients: "sender@example.test", body_text: "I would like to buy a solar fan.", html_only: false, truncated: false });
    if (options.outbound && url.pathname === `/inbox/messages/${messageId}/content`) return jsonResponse({ subject: "TradeOS UI email test - Q02", sender: "sender@example.test", recipients: "buyer@example.test", body_text: "Received Q02 <script>unsafe()</script>", html_only: false, truncated: false });
    if (url.pathname.endsWith("/next-questions") && options.questionsStatus) return jsonResponse({}, options.questionsStatus);
    if (request.method === "GET" && url.pathname === "/inbox/reply-work") {
      expect(url.searchParams.get("account_id")).toBe(item.account_id);
      return jsonResponse(options.work && url.searchParams.get("status") === workStatus ? [{ ...work, status: workStatus }] : []);
    }
    if (request.method === "POST" && url.pathname === `/inbox/reply-work/${workId}`) {
      const command = await request.json() as { expected_status: string; status: string };
      workUpdates.push(command);
      if (options.workDelay) await options.workDelay;
      if (options.updateStatus) return jsonResponse({}, options.updateStatus);
      if (options.staleOnce && workUpdates.length === 1) { workStatus = "in_progress"; return jsonResponse({ code: "validation_error" }, 400); }
      workStatus = command.status;
      work.history.push({ ...command, update_id: "rwu_test", handled_by: "emp-test-owner", handled_at: "2026-10-20T14:00:00Z" });
      return jsonResponse({ ...work, status: workStatus });
    }
    if (request.method === "GET" && url.pathname === "/inbox/conversations") {
      return jsonResponse([options.outbound ? { ...item, latest_direction: "outbound", effective_category: null, original_category: null, classified_by: null } : item]);
    }
    if (
      request.method === "GET"
      && url.pathname === `/inbox/conversations/${conversationId}`
    ) {
      detailReads++;
      if (detailReads > 1 && options.recheckStatus) return jsonResponse({}, options.recheckStatus);
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
  return { fetch, corrections, workUpdates };
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
  path = "/inbox",
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
  mountedApps.push(app);
  await router.replace(path);
  await new Promise((resolve) => setTimeout(resolve, 0));
  return { app, root };
}

afterEach(() => {
  for (const app of mountedApps.splice(0)) app.unmount();
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

  it("keeps metadata usable when protected mail content is unavailable", async () => {
    const { fetch } = makeFetch();
    const { root } = await mountInbox(fetch);

    await eventually(() => {
      expect(root.textContent).toContain("artifact:reply-1");
      expect(root.textContent).toContain("<tradeos.outbound@example.test>");
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

describe("SmartInbox current owner reply work", () => {
  it("retains authorized owner work when only suggestions are forbidden", async () => {
    const { fetch } = makeFetch({ work: true, questionsStatus: 403 });
    const { root } = await mountInbox(fetch);
    await eventually(() => {
      expect(root.textContent).toContain("开始处理");
      expect(root.textContent).toContain("下一问建议暂不可用");
    });
  });
  it("clears owner work when the permission recheck is revoked", async () => {
    const { fetch } = makeFetch({ work: true, questionsStatus: 403, recheckStatus: 403 });
    const { root } = await mountInbox(fetch);
    await eventually(() => expect(root.textContent).toContain("当前身份无权访问"));
    expect(root.textContent).not.toContain("开始处理");
    expect(root.textContent).not.toContain("artifact:reply-1");
  });
  it.each([false, true])("handles existing work including neutral review=%s without creating a handoff", async (review) => {
    const { fetch, workUpdates } = makeFetch({ work: true, review });
    const { root } = await mountInbox(fetch, `/inbox/conversations/${conversationId}`);
    await eventually(() => expect(root.textContent).toContain("开始处理"));
    expect(fetch.mock.calls.some(([input]) => asRequest(input).url.includes(`/inbox/conversations/${conversationId}`))).toBe(true);
    if (review) {
      expect(root.textContent).toContain("需人工核对");
      expect(root.textContent).toContain("核对回复（信息不足或有矛盾）");
    }
    const button = [...root.querySelectorAll("button")].find(b => b.textContent?.trim() === "开始处理");
    button!.click();
    await eventually(() => {
      expect(workUpdates).toEqual([expect.objectContaining({ expected_status: "pending", status: "in_progress", disposition: "started" })]);
      expect(root.querySelector('[aria-label="负责人回复工作项"]')?.textContent).toContain("处理中");
    });
    expect(fetch.mock.calls.filter(([input]) => asRequest(input).method === "POST").map(([input]) => new URL(asRequest(input).url).pathname))
      .toEqual([`/inbox/reply-work/${workId}`]);
  });

  it("does not offer handling when the server denies current ownership", async () => {
    const { fetch } = makeFetch({ work: true, canHandle: false });
    const { root } = await mountInbox(fetch);
    await eventually(() => expect(root.textContent).toContain("由当前负责人处理"));
    expect([...root.querySelectorAll("button")].some(b => b.textContent?.trim() === "开始处理")).toBe(false);
    expect([...root.querySelectorAll("button")].some(b => b.textContent === "标记已处理")).toBe(false);
  });

  it("clears protected work and message details after ownership is revoked", async () => {
    const { fetch, workUpdates } = makeFetch({ work: true, updateStatus: 403 });
    const { root } = await mountInbox(fetch);
    await eventually(() => expect(root.textContent).toContain("开始处理"));
    [...root.querySelectorAll("button")].find(b => b.textContent?.trim() === "开始处理")!.click();
    await eventually(() => {
      expect(root.textContent).toContain("客户负责人或访问权限已变化");
      expect(root.querySelector('[aria-label="负责人回复工作项"]')).toBeNull();
      expect(root.textContent).not.toContain("artifact:reply-1");
    });
    expect(workUpdates).toHaveLength(1);
  });

  it("shows an uncertain outcome without retrying the mutation", async () => {
    const { fetch, workUpdates } = makeFetch({ work: true, updateStatus: 503 });
    const { root } = await mountInbox(fetch);
    await eventually(() => expect(root.textContent).toContain("开始处理"));
    [...root.querySelectorAll("button")].find(b => b.textContent?.trim() === "开始处理")!.click();
    await eventually(() => expect(root.textContent).toContain("处理结果待核对"));
    await new Promise(resolve => setTimeout(resolve, 50));
    expect(workUpdates).toHaveLength(1);
  });
});


it("shows sent-only conversation, recipient and readable content while waiting for a reply", async () => {
  const { fetch } = makeFetch({ outbound: true });
  const { root } = await mountInbox(fetch);
  await eventually(() => {
    expect(root.textContent).toContain("等待回复");
    expect(root.textContent).toContain("buyer@example.test");
    expect(root.textContent).toContain("TradeOS UI email test - Q02");
    expect(root.textContent).toContain("Received Q02 <script>unsafe()</script>");
  });
  expect(root.querySelector("script")).toBeNull();
});

it("shows newest content first and retains the older incoming body with explicit dates", async () => {
  const { fetch } = makeFetch({ outbound: true, history: true });
  const { root } = await mountInbox(fetch);
  await eventually(() => {
    const cards = root.querySelectorAll(".timeline .message-card");
    expect(cards).toHaveLength(2);
    expect(cards[0]?.textContent).toContain("TradeOS UI email test - Q02");
    expect(cards[0]?.textContent).toContain("发送于");
    expect(cards[1]?.textContent).toContain("I would like to buy a solar fan.");
    expect(cards[1]?.textContent).toContain("收到于");
    expect(root.querySelector(".conversation-detail > header")?.textContent).toContain("buyer@example.test");
  });
});


describe("SmartInbox handling records", () => {
  it("requires a note, preserves waiting work and shows completed history", async () => {
    const { fetch, workUpdates } = makeFetch({ work: true });
    const { root } = await mountInbox(fetch);
    const button = (label: string) => [...root.querySelectorAll("button")].find(b => b.textContent?.trim() === label)!;
    await eventually(() => expect(root.textContent).toContain("等待客户补充"));
    expect(button("等待客户补充").disabled).toBe(true);
    const note = root.querySelector<HTMLTextAreaElement>('[aria-label="处理说明"]')!;
    note.value = "测试：等待客户明确数量";
    note.dispatchEvent(new Event("input", { bubbles: true }));
    await nextTick();
    button("等待客户补充").click();
    await eventually(() => expect(workUpdates).toHaveLength(1));
    await eventually(() => expect(root.textContent).toContain("测试：等待客户明确数量"));
    expect(workUpdates[0]).toMatchObject({ status: "in_progress", disposition: "waiting_customer", note: "测试：等待客户明确数量" });
    const finishNote = root.querySelector<HTMLTextAreaElement>('[aria-label="处理说明"]')!;
    finishNote.value = "测试：本次核对已完成，不代表客户采购确认";
    finishNote.dispatchEvent(new Event("input", { bubbles: true }));
    await nextTick();
    button("完成本次处理").click();
    await eventually(() => expect(workUpdates).toHaveLength(2));
    await eventually(() => expect(root.textContent).toContain("处理历史"));
    await eventually(() => expect(root.textContent).toContain("本次核对已完成"));
    expect(root.textContent).toContain("测试：等待客户明确数量");
    expect(root.textContent).toContain("emp-test-owner");
  });
});


it("keeps the original handling request across a refresh while saving", async () => {
  let release!: () => void;
  const workDelay = new Promise<void>(resolve => { release = resolve; });
  const { fetch, workUpdates } = makeFetch({ work: true, workDelay });
  const { root } = await mountInbox(fetch);
  const button = (label: string) => [...root.querySelectorAll("button")].find(b => b.textContent?.trim() === label)!;
  await eventually(() => expect(root.textContent).toContain("等待客户补充"));
  const note = root.querySelector<HTMLTextAreaElement>('[aria-label="处理说明"]')!;
  note.value = "测试：未决处理不能换请求编号";
  note.dispatchEvent(new Event("input", { bubbles: true }));
  await nextTick();
  button("等待客户补充").click();
  await eventually(() => expect(workUpdates).toHaveLength(1));
  button("刷新").click();
  await eventually(() => expect(root.textContent).toContain("重试原处理"));
  button("重试原处理").click();
  await eventually(() => expect(workUpdates).toHaveLength(2));
  expect(workUpdates[1]).toEqual(workUpdates[0]);
  release();
  await eventually(() => expect(root.textContent).toContain("处理中"));
});

it("clears notes and history when expanded work evidence is denied", async () => {
  const transport = makeFetch({ work: true });
  let denied = false;
  const fetch = vi.fn<typeof globalThis.fetch>(async input => {
    const request = asRequest(input);
    if (new URL(request.url).pathname === `/inbox/messages/${messageId}/content`) {
      return denied ? jsonResponse({}, 403) : jsonResponse({ subject: "测试邮件", sender: "test@example.test", recipients: "owner@example.test", body_text: "测试正文", html_only: false, truncated: false });
    }
    return transport.fetch(input);
  });
  const { root } = await mountInbox(fetch);
  await eventually(() => expect(root.textContent).toContain("等待客户补充"));
  const note = root.querySelector<HTMLTextAreaElement>('[aria-label="处理说明"]')!;
  note.value = "未保存的私有说明";
  note.dispatchEvent(new Event("input", { bubbles: true }));
  denied = true;
  const evidence = [...root.querySelectorAll("details")].find(d => d.querySelector("summary")?.textContent === "查看本工作项的原邮件依据")!;
  evidence.open = true;
  evidence.dispatchEvent(new Event("toggle"));
  await eventually(() => expect(root.querySelector('[aria-label="处理说明"]')).toBeNull());
  expect(root.textContent).not.toContain("未保存的私有说明");
  expect(root.textContent).not.toContain("测试正文");
});


it("allows a fresh reviewed command after a stale-state request is explicitly rejected", async () => {
  const { fetch, workUpdates } = makeFetch({ work: true, staleOnce: true });
  const { root } = await mountInbox(fetch);
  const button = () => [...root.querySelectorAll("button")].find(b => b.textContent?.trim() === "等待客户补充")!;
  await eventually(() => expect(root.textContent).toContain("等待客户补充"));
  const note = root.querySelector<HTMLTextAreaElement>('[aria-label="处理说明"]')!;
  note.value = "测试：其他标签页已开始处理";
  note.dispatchEvent(new Event("input", { bubbles: true }));
  await nextTick(); button().click();
  await eventually(() => expect(workUpdates).toHaveLength(1));
  await eventually(() => expect(root.querySelector('[aria-label="处理说明"]')).not.toBeNull());
  await eventually(() => expect(root.textContent).toContain("处理中"));
  expect(workUpdates).toHaveLength(1);
  button().click();
  await eventually(() => expect(workUpdates).toHaveLength(2));
  expect(workUpdates[1]).toMatchObject({ expected_status: "in_progress", status: "in_progress" });
});
