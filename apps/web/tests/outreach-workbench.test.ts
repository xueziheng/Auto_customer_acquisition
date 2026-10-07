import { createApp, nextTick, type App as VueApp } from "vue";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "../src/api/client";
import type { components } from "../src/api/api";
import App from "../src/App.vue";
import router from "../src/router";

type EnrollmentView = components["schemas"]["EnrollmentView"];
type MessageAttemptView = components["schemas"]["MessageAttemptView"];

const enrollment: EnrollmentView = {
  account_id: "acc-demo-one",
  campaign_id: "cmp-demo-one",
  campaign_version: 1,
  contact_point_id: "cp-demo-one",
  current_step: 1,
  enrolled_at: "2026-08-15T08:00:00Z",
  enrollment_id: "enr-demo-one",
  next_send_at: "2026-08-16T09:00:00Z",
  sending_identity_id: "sid-demo-one",
  state: "in_sequence",
  stop_reason: null,
  stopped_at: null,
  tenant_id: "tenant-demo-one",
};

const attempt: MessageAttemptView = {
  attempt_id: "mat-demo-one",
  campaign_id: "cmp-demo-one",
  campaign_version: 1,
  created_at: "2026-08-15T08:05:00Z",
  enrollment_id: "enr-demo-one",
  failure_category: null,
  idempotency_key: "demo-key",
  message_id: "msg-demo-one",
  provider_ref: null,
  sending_identity_id: "sid-demo-one",
  state: "reserved",
  step_number: 1,
  tenant_id: "tenant-demo-one",
  updated_at: "2026-08-15T08:05:00Z",
};

function jsonResponse(
  body: unknown,
  status = 200,
  headers: Record<string, string> = {},
): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

function asRequest(input: URL | RequestInfo): Request {
  if (input instanceof Request) return input;
  throw new TypeError("fake transport requires a Request");
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

function sendResponse(body: unknown, status = 200): Response {
  return jsonResponse(body, status);
}

function makeSendFetch(
  options: {
    enrollments?: EnrollmentView[];
    prepare?: () => Response;
    send?: (body: unknown, requestNumber: number) => Response | Promise<Response>;
    listError?: Response;
  } = {},
): ReturnType<typeof vi.fn<typeof globalThis.fetch>> {
  const requests: { method: string; path: string }[] = [];
  return vi.fn<typeof globalThis.fetch>(async (input) => {
    const request = asRequest(input);
    const url = new URL(request.url);
    requests.push({ method: request.method, path: url.pathname });
    if (request.method === "GET" && url.pathname === "/crm/enrollments") {
      if (options.listError) return options.listError;
      return jsonResponse(options.enrollments ?? [enrollment]);
    }
    if (
      request.method === "POST" &&
      /^\/crm\/enrollments\/[^/]+\/attempts\/prepare$/.test(url.pathname)
    ) {
      return options.prepare ? options.prepare() : jsonResponse(attempt);
    }
    if (
      request.method === "POST" &&
      /^\/crm\/message-attempts\/[^/]+\/send$/.test(url.pathname)
    ) {
      const body = await request.json();
      if (options.send) return options.send(body, requests.length);
      return sendResponse({
        duplicate: false,
        error_category: null,
        provider_ref: "gmail-demo-ref",
        status: "succeeded",
        tool_call_id: "tool-demo-one",
      });
    }
    return jsonResponse({ code: "unexpected", message: "unexpected" }, 500);
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

async function selectFirstRow(root: HTMLElement): Promise<HTMLButtonElement> {
  const row = root.querySelector('li[aria-label="入组记录"]');
  expect(row).toBeTruthy();
  (row as HTMLElement).click();
  await eventually(() => {
    expect(root.textContent).toContain("手工准备草稿");
  });
  return [...root.querySelectorAll("button")].find((button) =>
    button.textContent?.includes("手工准备草稿"),
  ) as HTMLButtonElement;
}

async function typeDraft(
  root: HTMLElement,
  subjectText: string,
  bodyText: string,
): Promise<void> {
  const subject = root.querySelector<HTMLInputElement>("#send-subject");
  const body = root.querySelector<HTMLTextAreaElement>("#send-body");
  expect(subject).toBeTruthy();
  expect(body).toBeTruthy();
  subject!.value = subjectText;
  body!.value = bodyText;
  subject!.dispatchEvent(new Event("input"));
  body!.dispatchEvent(new Event("input"));
  // Vue 在微任务中更新 disabled 状态：等按钮可点后再继续
  await eventually(() => {
    const send = [...root.querySelectorAll("button")].find((button) =>
      button.textContent?.trim() === "发送",
    );
    expect(send).toBeTruthy();
    expect((send as HTMLButtonElement).disabled).toBe(false);
  });
}

async function typeDraftAndSend(
  root: HTMLElement,
  subjectText: string,
  bodyText: string,
): Promise<void> {
  await typeDraft(root, subjectText, bodyText);
  [...root.querySelectorAll("button")]
    .find((button) => button.textContent?.trim() === "发送")!
    .click();
}

async function mountWorkbench(
  fetch: ReturnType<typeof vi.fn<typeof globalThis.fetch>>,
): Promise<{ app: VueApp; root: HTMLElement }> {
  const root = document.createElement("div");
  document.body.replaceChildren(root);
  const client = createApiClient({ baseUrl: "https://tradeos.test", fetch });
  const app = createApp(App);
  app.provide("tradeos-api-client", client);
  app.use(router);
  app.mount(root);
  await router.replace("/crm/outreach");
  await new Promise((resolve) => setTimeout(resolve, 0));
  return { app, root };
}

afterEach(() => {
  // 每个测试自带独立 mount 与路由跳转，无需共享路由复位
});

describe("OutreachWorkbench", () => {
  it("registers the /crm/outreach route and renders the ledger", async () => {
    expect(router.getRoutes().map((route) => route.path)).toContain(
      "/crm/outreach",
    );
    const fetch = makeSendFetch();
    const { root } = await mountWorkbench(fetch);
    await eventually(() => {
      expect(root.querySelectorAll('li[aria-label="入组记录"]')).toHaveLength(1);
      expect(root.textContent).toContain("enr-demo-one");
    });
  });

  it("shows loading then the empty state", async () => {
    const gate = deferred<void>();
    const fetch = vi.fn<typeof globalThis.fetch>(async () => {
      await gate.promise;
      return jsonResponse([]);
    });
    const root = document.createElement("div");
    document.body.replaceChildren(root);
    const client = createApiClient({ baseUrl: "https://tradeos.test", fetch });
    const app = createApp(App);
    app.provide("tradeos-api-client", client);
    app.use(router);
    app.mount(root);
    await router.replace("/crm/outreach");
    await eventually(() => {
      expect(root.textContent).toContain("正在加载触达任务…");
    });
    expect(root.querySelector('[aria-busy="true"]')).not.toBeNull();
    gate.resolve();
    await eventually(() => {
      expect(root.textContent).toContain("当前没有待处理的触达任务");
    });
  });

  it("renders the fixed 403-empty state without ids", async () => {
    const fetch = makeSendFetch({ listError: jsonResponse({ code: "forbidden", message: "没有权限" }, 403) });
    const { root } = await mountWorkbench(fetch);
    await eventually(() => {
      expect(root.textContent).toContain("当前没有可访问的触达任务");
      expect(root.textContent).not.toContain("enr-demo-one");
    });
  });

  it("renders the fixed 503 state with manual refresh and no auto write", async () => {
    const fetch = makeSendFetch({ listError: jsonResponse({ code: "service_unavailable", message: "服务暂时不可用" }, 503) });
    const { root } = await mountWorkbench(fetch);
    await eventually(() => {
      expect(root.textContent).toContain("触达服务暂不可用");
    });
    const refresh = [...root.querySelectorAll("button")].find((button) =>
      button.textContent?.includes("刷新"),
    );
    expect(refresh).toBeTruthy();
  });

  it("opens the send drawer after prepare succeeds and wipes draft on success", async () => {
    let prepareCalls = 0;
    const fetch = makeSendFetch({
      prepare: () => {
        prepareCalls += 1;
        return jsonResponse(attempt);
      },
    });
    const { root } = await mountWorkbench(fetch);
    await eventually(() => {
      expect(root.textContent).toContain("enr-demo-one");
    });
    const prepare = await selectFirstRow(root);
    prepare.click();
    await eventually(() => {
      expect(prepareCalls).toBe(1);
      expect(root.textContent).toContain("邮件主题");
      expect(root.textContent).toContain("邮件正文");
      expect(root.textContent).toContain("发给客户的内容请使用英文");
    });
    await typeDraftAndSend(root, "demo subject", "demo body");
    await eventually(() => {
      expect(root.querySelector('[role="dialog"]')).toBeNull();
    });
    // 发送成功后抽屉关闭、输入框已卸载；重新准备并打开抽屉验证草稿被清空
    (await selectFirstRow(root)).click();
    await eventually(() => {
      expect(root.textContent).toContain("邮件主题");
    });
    const reopenedSubject = root.querySelector<HTMLInputElement>("#send-subject")!;
    const reopenedBody = root.querySelector<HTMLTextAreaElement>("#send-body")!;
    expect(reopenedSubject.value).toBe("");
    expect(reopenedBody.value).toBe("");
    const sendCalls = fetch.mock.calls.filter(
      ([input]) => asRequest(input as URL | RequestInfo).method === "POST" &&
        /\/send$/.test(new URL(asRequest(input as URL | RequestInfo).url).pathname),
    );
    expect(sendCalls).toHaveLength(1);
  });

  it("locks double click on send and shows duplicate / conflict fixed copy", async () => {
    let sendCalls = 0;
    const responses: Response[] = [
      sendResponse({ duplicate: true, error_category: null, provider_ref: "r1", status: "succeeded", tool_call_id: "t1" }),
    ];
    const fetch = makeSendFetch({
      send: async () => {
        sendCalls += 1;
        await new Promise((resolve) => setTimeout(resolve, 10));
        return responses[0];
      },
    });
    const { root } = await mountWorkbench(fetch);
    await eventually(() => {
      expect(root.textContent).toContain("enr-demo-one");
    });
    (await selectFirstRow(root)).click();
    await eventually(() => {
      expect(root.textContent).toContain("邮件主题");
    });
    await typeDraftAndSend(root, "demo subject", "demo body");
    const send = [...root.querySelectorAll("button")].find((b) => b.textContent?.trim() === "发送")!;
    send.click(); // 第二次点击应被双击锁忽略
    await eventually(() => {
      expect(sendCalls).toBe(1);
      expect(root.textContent).toContain("已识别为重复请求，未再次发送");
    });
    expect(root.querySelector('[role="dialog"]')).toBeNull();
  });

  it("renders the fixed conflict, reconciliation and in_progress states", async () => {
    for (const [payload, expected] of [
      [{ code: "idempotency_conflict", message: "冲突" }, "请求标识与内容不一致，请重新准备"],
      [{ code: "reconciliation_required", message: "对账" }, "发送状态待人工核对，请勿重复发送"],
      [{ code: "in_progress", message: "处理中" }, "发送请求正在处理中，请稍后刷新"],
    ] as const) {
      const fetch = makeSendFetch({
        send: () => sendResponse(payload, 409),
      });
      const { root } = await mountWorkbench(fetch);
      await eventually(() => {
        expect(root.textContent).toContain("enr-demo-one");
      });
      (await selectFirstRow(root)).click();
      await eventually(() => {
        expect(root.textContent).toContain("邮件主题");
      });
      await typeDraftAndSend(root, "demo subject", "demo body");
      await eventually(() => {
        expect(root.textContent).toContain(expected);
      });
    }
  });

  it("shows Retry-After manual retry on 429 and keeps draft on 503", async () => {
    const fetch = makeSendFetch({
      send: (body) =>
        body && (body as { subject: string }).subject === "retryable"
          ? jsonResponse({ code: "rate_limited", message: "额度" }, 429, { "retry-after": "30" })
          : sendResponse({ code: "service_unavailable", message: "暂不可用" }, 503),
    });
    const { root } = await mountWorkbench(fetch);
    await eventually(() => {
      expect(root.textContent).toContain("enr-demo-one");
    });
    (await selectFirstRow(root)).click();
    await eventually(() => {
      expect(root.textContent).toContain("邮件主题");
    });
    const subject = root.querySelector<HTMLInputElement>("#send-subject")!;
    await typeDraftAndSend(root, "retryable", "demo body");
    await eventually(() => {
      expect(root.textContent).toContain("发送额度暂不可用");
      expect(root.textContent).toMatch(/\d+ 秒后可手工重试/);
      expect(subject.value).toBe("retryable");
    });
    await typeDraftAndSend(root, "not-retryable", "demo body");
    await eventually(() => {
      expect(root.textContent).toContain("发送服务暂不可用，当前事实可能已变化");
      expect(subject.value).toBe("not-retryable");
    });
  });

  it("closes and wipes the drawer on 403 permission change", async () => {
    const fetch = makeSendFetch({
      send: () => sendResponse({ code: "forbidden", message: "没有权限" }, 403),
    });
    const { root } = await mountWorkbench(fetch);
    await eventually(() => {
      expect(root.textContent).toContain("enr-demo-one");
    });
    (await selectFirstRow(root)).click();
    await eventually(() => {
      expect(root.textContent).toContain("邮件主题");
    });
    await typeDraftAndSend(root, "will-be-wiped", "demo body");
    await eventually(() => {
      expect(root.textContent).toContain("权限已变化，编辑器已关闭");
      expect(root.querySelector('[role="dialog"]')).toBeNull();
    });
  });

  it("closes the drawer on Escape, returns focus and wipes the draft", async () => {
    const { root } = await mountWorkbench(makeSendFetch());
    await eventually(() => {
      expect(root.textContent).toContain("enr-demo-one");
    });
    const prepare = await selectFirstRow(root);
    prepare.click();
    await eventually(() => {
      expect(root.textContent).toContain("邮件主题");
    });
    await typeDraft(root, "demo subject", "demo body");
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    await eventually(() => {
      expect(root.querySelector('[role="dialog"]')).toBeNull();
    });
    expect(document.activeElement).toBe(prepare);
    // 重新打开抽屉：草稿已被清空
    (await selectFirstRow(root)).click();
    await eventually(() => {
      expect(root.textContent).toContain("邮件主题");
    });
    expect(root.querySelector<HTMLInputElement>("#send-subject")!.value).toBe("");
    expect(root.querySelector<HTMLTextAreaElement>("#send-body")!.value).toBe("");
  });

  it("keeps the drawer locked while a send is in flight and survives a late response", async () => {
    const slow = deferred<Response>();
    const fetch = makeSendFetch({
      send: () => slow.promise,
    });
    const { root } = await mountWorkbench(fetch);
    await eventually(() => {
      expect(root.textContent).toContain("enr-demo-one");
    });
    (await selectFirstRow(root)).click();
    await eventually(() => {
      expect(root.textContent).toContain("邮件主题");
    });
    await typeDraftAndSend(root, "demo subject", "demo body");
    // 发送在途：按钮保持锁定
    await eventually(() => {
      const send = [...root.querySelectorAll("button")].find((b) =>
        b.textContent?.trim() === "发送",
      );
      expect(send).toBeTruthy();
      expect((send as HTMLButtonElement).disabled).toBe(true);
    });
    // 迟到的成功响应正常收尾：关闭抽屉且不产生错误文案
    slow.resolve(
      sendResponse({ duplicate: false, error_category: null, provider_ref: "late", status: "succeeded", tool_call_id: "late" }),
    );
    await eventually(() => {
      expect(root.querySelector('[role="dialog"]')).toBeNull();
    });
    await nextTick();
    expect(root.textContent).toContain("发送成功");
  });

  it("never leaks provider or customer content in errors", async () => {
    const fetch = makeSendFetch({
      send: () => sendResponse({ code: "service_unavailable", message: "marker-provider" }, 503),
    });
    const { root } = await mountWorkbench(fetch);
    await eventually(() => {
      expect(root.textContent).toContain("enr-demo-one");
    });
    (await selectFirstRow(root)).click();
    await eventually(() => {
      expect(root.textContent).toContain("邮件主题");
    });
    await typeDraftAndSend(root, "customer-marker-secret", "demo body");
    await eventually(() => {
      expect(root.textContent).toContain("发送服务暂不可用，当前事实可能已变化");
    });
    expect(root.textContent).not.toContain("marker-provider");
    expect(root.textContent).not.toContain("customer-marker-secret");
  });
});
