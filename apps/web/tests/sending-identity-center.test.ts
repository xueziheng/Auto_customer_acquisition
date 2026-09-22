import { createApp, nextTick, type App as VueApp } from "vue";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "../src/api/client";
import type { components } from "../src/api/api";
import App from "../src/App.vue";
import router from "../src/router";

type IdentityView = components["schemas"]["IdentityView"];

const identity: IdentityView = {
  address: "sales@cold.example.test",
  auth: {
    checked_at: "2026-08-15T08:00:00Z",
    dkim_passed: true,
    dmarc_passed: true,
    failures: [],
    spf_passed: true,
  },
  can_send_today: true,
  created_at: "2026-08-01T00:00:00Z",
  domain: "cold.example.test",
  identity_id: "sid-demo-one",
  remaining_today: 50,
  reputation: {
    blocklist_hits: 0,
    complaint_rate: "0.0005",
    computed_at: "2026-08-15T08:00:00Z",
    delivered: 100,
    delivery_rate: "0.99",
    hard_bounce_rate: "0.01",
    sample_sufficient: true,
    sent_attempts: 120,
    spam_trap_hits: 0,
    window_days: 7,
  },
  role: "cold_outreach",
  state: "active",
  target_daily_volume: 50,
  usable_for_cold_outreach: true,
  warmup_complete: false,
  warmup_day: 12,
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

function makeIdentityFetch(options: {
  identities?: IdentityView[];
  listError?: Response;
  checks?: () => Response | Promise<Response>;
} = {}): {
  fetch: ReturnType<typeof vi.fn<typeof globalThis.fetch>>;
  checkKeys: string[];
  checkTargets: string[];
} {
  const checkKeys: string[] = [];
  const checkTargets: string[] = [];
  const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
    const request = asRequest(input);
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/crm/sending-identities/management") {
      if (options.listError) return options.listError;
      return jsonResponse(options.identities ?? [identity]);
    }
    if (
      request.method === "POST" &&
      /^\/crm\/sending-identities\/[^/]+\/authentication-checks$/.test(url.pathname)
    ) {
      const body = (await request.json()) as { request_key?: string };
      if (typeof body.request_key !== "string" || !body.request_key) {
        return jsonResponse({ code: "validation_error", message: "请求参数无效" }, 400);
      }
      checkKeys.push(body.request_key);
      checkTargets.push(url.pathname.split("/")[3]);
      if (options.checks) return options.checks();
      return jsonResponse(
        {
          completed_at: null,
          request_id: "acr-demo-one",
          request_key: body.request_key,
          requested_at: "2026-08-15T09:00:00Z",
          sending_identity_id: "sid-demo-one",
          status: "requested",
          tenant_id: "tenant-demo-one",
        },
        200,
      );
    }
    return jsonResponse({ code: "unexpected", message: "unexpected" }, 500);
  });
  return { fetch, checkKeys, checkTargets };
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
  const client = createApiClient({ baseUrl: "https://tradeos.test", fetch });
  const app = createApp(App);
  app.provide("tradeos-api-client", client);
  app.use(router);
  app.mount(root);
  await router.replace("/crm/sending-identities");
  await new Promise((resolve) => setTimeout(resolve, 0));
  return { app, root };
}

afterEach(() => {
  router.replace("/");
});

describe("SendingIdentityCenter", () => {
  it("registers the route and renders code-derived cards", async () => {
    expect(router.getRoutes().map((route) => route.path)).toContain(
      "/crm/sending-identities",
    );
    const { fetch } = makeIdentityFetch();
    const { root } = await mountCenter(fetch);
    await eventually(() => {
      expect(root.textContent).toContain("cold.example.test");
      expect(root.textContent).toContain("SPF 通过");
      expect(root.textContent).toContain("DKIM 通过");
      expect(root.textContent).toContain("DMARC 通过");
      expect(root.textContent).toContain("第 12 / 28 天");
      expect(root.textContent).toContain("50 · 目标日量 50");
      expect(root.textContent).toContain("代码确定性计算");
    });
  });

  it("renders the fixed 403 and empty states", async () => {
    const denied = makeIdentityFetch({
      listError: jsonResponse({ code: "forbidden", message: "没有权限" }, 403),
    });
    const deniedRoot = await mountCenter(denied.fetch);
    await eventually(() => {
      expect(deniedRoot.root.textContent).toContain("当前账号无法查看发件身份");
    });
    const empty = makeIdentityFetch({ identities: [] });
    const emptyRoot = await mountCenter(empty.fetch);
    await eventually(() => {
      expect(emptyRoot.root.textContent).toContain("暂无已登记发件身份");
    });
  });

  it("submits a hidden request_key, reuses it on retry and reports submission only", async () => {
    const { fetch, checkKeys } = makeIdentityFetch();
    const { root } = await mountCenter(fetch);
    await eventually(() => {
      expect(root.textContent).toContain("cold.example.test");
    });
    const check = [...root.querySelectorAll("button")].find((b) =>
      b.textContent?.includes("重新检查认证"),
    );
    expect(check).toBeTruthy();
    check!.click();
    await eventually(() => {
      expect(checkKeys).toHaveLength(1);
      expect(root.textContent).toContain("认证检查已提交");
    });
    expect(root.textContent).not.toMatch(/request_key|acr-demo-one/);
    const again = [...root.querySelectorAll("button")].find((b) =>
      b.textContent?.includes("重新检查认证"),
    )!;
    again.click();
    await eventually(() => {
      expect(checkKeys).toHaveLength(2);
    });
    expect(checkKeys[0]).toBe(checkKeys[1]);
    expect(root.textContent).not.toMatch(/request_key|acr-demo-one/);
  });

  it("targets the clicked identity and isolates request keys per identity", async () => {
    const identityB: IdentityView = {
      ...identity,
      address: "sales@second.example.test",
      domain: "second.example.test",
      identity_id: "sid-demo-two",
    };
    const { fetch, checkKeys, checkTargets } = makeIdentityFetch({
      identities: [identity, identityB],
    });
    const { root } = await mountCenter(fetch);
    await eventually(() => {
      expect(root.textContent).toContain("cold.example.test");
      expect(root.textContent).toContain("second.example.test");
    });
    const cardOf = (domain: string) =>
      [...root.querySelectorAll("article")].find((card) =>
        card.textContent?.includes(domain),
      )!;
    const checkOf = (domain: string) =>
      [...cardOf(domain).querySelectorAll("button")].find((b) =>
        b.textContent?.includes("重新检查认证"),
      )!;
    // 点击 A：请求路径必须指向 A
    checkOf("cold.example.test").click();
    await eventually(() => {
      expect(checkTargets).toEqual(["sid-demo-one"]);
      expect(checkKeys).toHaveLength(1);
    });
    // 点击 B：请求路径必须指向 B，且 A/B 的 key 不串
    checkOf("second.example.test").click();
    await eventually(() => {
      expect(checkTargets).toEqual(["sid-demo-one", "sid-demo-two"]);
      expect(checkKeys).toHaveLength(2);
    });
    expect(checkKeys[0]).not.toBe(checkKeys[1]);
    // B 重试：仍指向 B，且复用 B 自己的 key
    checkOf("second.example.test").click();
    await eventually(() => {
      expect(checkTargets).toEqual(["sid-demo-one", "sid-demo-two", "sid-demo-two"]);
      expect(checkKeys).toHaveLength(3);
    });
    expect(checkKeys[2]).toBe(checkKeys[1]);
    expect(checkKeys[2]).not.toBe(checkKeys[0]);
  });

  it("locks double click and ignores stale responses", async () => {
    const slow = deferred<Response>();
    let calls = 0;
    const { fetch, checkKeys } = makeIdentityFetch({
      checks: () => {
        calls += 1;
        return calls === 1 ? slow.promise : jsonResponse(
          {
            completed_at: null,
            request_id: "acr-demo-two",
            request_key: "k",
            requested_at: "2026-08-15T09:00:00Z",
            sending_identity_id: "sid-demo-one",
            status: "requested",
            tenant_id: "tenant-demo-one",
          },
          200,
        );
      },
    });
    const { root } = await mountCenter(fetch);
    await eventually(() => {
      expect(root.textContent).toContain("cold.example.test");
    });
    const check = [...root.querySelectorAll("button")].find((b) =>
      b.textContent?.includes("重新检查认证"),
    )!;
    check.click();
    check.click();
    await eventually(() => {
      expect(checkKeys).toHaveLength(1);
    });
    slow.resolve(
      jsonResponse(
        {
          completed_at: null,
          request_id: "acr-demo-late",
          request_key: "late",
          requested_at: "2026-08-15T09:00:00Z",
          sending_identity_id: "sid-demo-one",
          status: "requested",
          tenant_id: "tenant-demo-one",
        },
        200,
      ),
    );
    await nextTick();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(checkKeys).toHaveLength(1);
  });
});
