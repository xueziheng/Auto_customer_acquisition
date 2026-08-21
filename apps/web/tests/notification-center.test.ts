import { createApp, nextTick, type App as VueApp } from "vue";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "../src/api/client";
import NotificationBadge from "../src/components/NotificationBadge.vue";
import type { components } from "../src/api/api";
import App from "../src/App.vue";
import router from "../src/router";

type InAppNotificationView = components["schemas"]["InAppNotificationView"];

function notification(
  id: string,
  overrides: Partial<InAppNotificationView> = {},
): InAppNotificationView {
  return {
    context: {
      kind: "handoff_escalation",
      level: null,
      primary_id: "hnd-demo-one",
      reason_code: "t1",
      secondary_id: null,
    },
    created_at: "2026-08-15T09:00:00Z",
    notification_id: id,
    priority: "urgent",
    read_at: null,
    relative_link: "/crm/handoffs",
    tenant_id: "tenant-demo-one",
    title: "人工接管提醒",
    ...overrides,
  };
}

const unread = notification("ntf-demo-one");
const read = notification("ntf-demo-two", { read_at: "2026-08-15T08:00:00Z", priority: "normal" });
const low = notification("ntf-demo-three", { priority: "low", relative_link: "https://evil.example.test/x" });

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

function makeNotificationFetch(options: {
  items?: InAppNotificationView[];
  read?: (id: string) => Response;
  list?: (call: number) => Response;
} = {}): {
  fetch: ReturnType<typeof vi.fn<typeof globalThis.fetch>>;
  reads: string[];
} {
  const reads: string[] = [];
  let listCalls = 0;
  const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
    const request = asRequest(input);
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/notifications") {
      listCalls += 1;
      if (options.list) return options.list(listCalls);
      return jsonResponse(options.items ?? [unread, read, low]);
    }
    if (
      request.method === "POST" &&
      /^\/notifications\/[^/]+\/read$/.test(url.pathname)
    ) {
      const id = url.pathname.split("/")[2];
      reads.push(id);
      if (options.read) return options.read(id);
      const target = (options.items ?? [unread, read, low]).find(
        (item) => item.notification_id === id,
      );
      if (!target) return jsonResponse({ code: "validation_error", message: "请求参数无效" }, 400);
      return jsonResponse({ ...target, read_at: "2026-08-15T09:30:00Z" });
    }
    return jsonResponse({ code: "unexpected", message: "unexpected" }, 500);
  });
  return { fetch, reads };
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
  const client = createApiClient({ baseUrl: "https://tradeos.test", fetch });
  const app = createApp(App);
  app.provide("tradeos-api-client", client);
  app.use(router);
  app.mount(root);
  await router.replace("/notifications");
  await new Promise((resolve) => setTimeout(resolve, 0));
  return { app, root };
}

async function selectInboxRow(root: HTMLElement, id: string): Promise<void> {
  const row = [...root.querySelectorAll("li")].find((item) =>
    item.textContent?.includes(id),
  );
  expect(row).toBeTruthy();
  (row as HTMLElement).click();
  await eventually(() => {
    expect(root.textContent).toContain("标记为已读");
  });
}

async function clickMarkRead(root: HTMLElement): Promise<void> {
  const button = [...root.querySelectorAll("button")].find((item) =>
    item.textContent?.includes("标记为已读"),
  );
  expect(button).toBeTruthy();
  (button as HTMLButtonElement).click();
}

afterEach(() => {
  router.replace("/");
});

describe("NotificationCenter", () => {
  it("keeps the badge in a safe stale state when the request itself throws", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>(async () => {
      throw new TypeError("network unavailable");
    });
    const root = document.createElement("div");
    document.body.replaceChildren(root);
    const app = createApp(NotificationBadge);
    app.provide(
      "tradeos-api-client",
      createApiClient({ baseUrl: "https://tradeos.test", fetch }),
    );
    app.use(router);
    app.mount(root);

    await eventually(() => {
      expect(root.textContent).toContain("状态可能已过期");
      expect(root.querySelector('[aria-label="通知，0 条未读"]')).not.toBeNull();
    });
    app.unmount();
  });

  it("registers the route and renders unread/read with text and priority labels", async () => {
    expect(router.getRoutes().map((route) => route.path)).toContain("/notifications");
    const { fetch } = makeNotificationFetch();
    const { root } = await mountInbox(fetch);
    await eventually(() => {
      expect(root.textContent).toContain("ntf-demo-one");
      expect(root.textContent).toContain("未读");
      expect(root.textContent).toContain("已读");
      expect(root.textContent).toContain("紧急");
      expect(root.textContent).toContain("普通");
      expect(root.textContent).toContain("低");
    });
    const unreadRows = [...root.querySelectorAll("li")].filter((row) =>
      row.textContent?.includes("未读"),
    );
    expect(unreadRows.length).toBeGreaterThan(0);
  });

  it("renders 前往处理 only for validated relative links and never v-html", async () => {
    const { fetch } = makeNotificationFetch();
    const { root } = await mountInbox(fetch);
    await eventually(() => {
      expect(root.textContent).toContain("ntf-demo-one");
    });
    await selectInboxRow(root, "ntf-demo-one");
    const links = [...root.querySelectorAll("a")].filter((a) =>
      a.textContent?.includes("前往处理"),
    );
    expect(links).toHaveLength(1);
    expect(links[0].getAttribute("href")).toBe("/crm/handoffs");
    expect(root.querySelectorAll("[v-html]")).toHaveLength(0);
    const evil = [...root.querySelectorAll("li")].find((row) =>
      row.textContent?.includes("ntf-demo-three"),
    );
    expect(evil?.querySelector("a[href^='https://evil']")).toBeNull();
  });

  it("marks read monotonic: stale response never reverts read_at", async () => {
    let readNumber = 0;
    const { fetch, reads } = makeNotificationFetch({
      read: () => {
        readNumber += 1;
        if (readNumber === 1) {
          return jsonResponse({ ...unread, read_at: "2026-08-15T09:45:00Z" });
        }
        // 模拟过期/回退的服务器状态：read_at 回到 null
        return jsonResponse({ ...unread, read_at: null });
      },
    });
    const { root } = await mountInbox(fetch);
    await eventually(() => {
      expect(root.textContent).toContain("ntf-demo-one");
    });
    const row = [...root.querySelectorAll("li")].find((r) =>
      r.textContent?.includes("ntf-demo-one"),
    )!;
    row.click();
    await eventually(() => {
      expect(root.textContent).toContain("标记为已读");
    });
    clickMarkRead(root);
    await eventually(() => {
      expect(reads).toHaveLength(1);
    });
    const afterFirst = [...root.querySelectorAll("li")].find((r) =>
      r.textContent?.includes("ntf-demo-one"),
    )!;
    await eventually(() => {
      expect(afterFirst.textContent).toContain("已读");
    });
    clickMarkRead(root);
    await eventually(() => {
      expect(reads).toHaveLength(2);
    });
    await nextTick();
    await new Promise((resolve) => setTimeout(resolve, 10));
    const afterStale = [...root.querySelectorAll("li")].find((r) =>
      r.textContent?.includes("ntf-demo-one"),
    )!;
    expect(afterStale.textContent).toContain("已读");
    expect(afterStale.textContent).not.toContain("未读");
  });

  it("keeps the last badge count and flags stale status on a real failed refresh after mark-read", async () => {
    let listCalls = 0;
    const { fetch } = makeNotificationFetch({
      read: () => jsonResponse({ ...unread, read_at: "2026-08-15T09:30:00Z" }),
      list: (call) => {
        listCalls = call;
        if (call === 3) {
          // 真实非 2xx 失败：mark-read 触发的徽标刷新
          return jsonResponse({ code: "unexpected", message: "boom" }, 500);
        }
        return jsonResponse([unread, read, low]);
      },
    });
    const { root } = await mountInbox(fetch);
    // 初始：徽标与列表都成功加载，2 条未读
    await eventually(() => {
      expect(root.textContent).toContain("未读");
      expect(root.querySelector('[aria-label="通知，2 条未读"]')).not.toBeNull();
    });
    expect(root.textContent).not.toContain("状态可能已过期");
    // 选中并标记已读 → 视图更新（1 未读）→ 徽标刷新失败
    [...root.querySelectorAll("li")].find((r) =>
      r.textContent?.includes("ntf-demo-one"),
    )!.click();
    await eventually(() => {
      expect(root.textContent).toContain("标记为已读");
    });
    clickMarkRead(root);
    await eventually(() => {
      // 视图侧已更新：1 未读
      expect(root.textContent).toMatch(/\b1 未读/);
    });
    // 徽标刷新确实失败过（第三次 GET 非 2xx）
    await eventually(() => {
      expect(listCalls).toBeGreaterThanOrEqual(3);
      expect(root.textContent).toContain("状态可能已过期");
    });
    // 失败不清零：徽标保留最近可信计数 2
    expect(root.querySelector('[aria-label="通知，2 条未读"]')).not.toBeNull();
    // 徽标失败不得把视图的已读状态回退
    const rowOne = [...root.querySelectorAll("li")].find((r) =>
      r.textContent?.includes("ntf-demo-one"),
    )!;
    expect(rowOne.textContent).toContain("已读");
    expect(rowOne.textContent).not.toContain("未读");
  });

  it("shows fixed safe state for cross-recipient read failure", async () => {
    const { fetch } = makeNotificationFetch({
      read: () => jsonResponse({ code: "validation_error", message: "请求参数无效" }, 400),
    });
    const { root } = await mountInbox(fetch);
    await eventually(() => {
      expect(root.textContent).toContain("ntf-demo-one");
    });
    [...root.querySelectorAll("li")].find((r) =>
      r.textContent?.includes("ntf-demo-one"),
    )!.click();
    await eventually(() => {
      expect(root.textContent).toContain("标记为已读");
    });
    clickMarkRead(root);
    await eventually(() => {
      expect(root.textContent).toContain("无法读取或更新该通知");
    });
    expect(root.textContent).not.toContain("ntf-demo-one 不存在");
  });
});
