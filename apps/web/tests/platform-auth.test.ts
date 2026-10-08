import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPlatformClient } from "../src/api/platform-auth";
import type { components } from "../src/api/platform-api";

type Session = components["schemas"]["PlatformSessionResponse"];
type Overview = components["schemas"]["PlatformOverview"];
const session = (): Session => ({
  username: "platform-test", display_name: "测试平台管理员", role: "platform_admin",
  csrf_token: "synthetic-platform-csrf", expires_at: new Date(Date.now() + 60_000).toISOString(),
});
const overview = (): Overview => ({ generated_at: new Date().toISOString(), enterprises: [] });
function json(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
}
class FakeChannel {
  static all: FakeChannel[] = [];
  onmessage: ((event: MessageEvent) => void) | null = null;
  postMessage = vi.fn();
  close = vi.fn();
  constructor(public name: string) { FakeChannel.all.push(this); }
  invalidate(): void { this.onmessage?.({ data: "invalidate" } as MessageEvent); }
}
beforeEach(() => {
  vi.useFakeTimers();
  FakeChannel.all = [];
  vi.stubGlobal("BroadcastChannel", FakeChannel);
  vi.stubGlobal("navigator", {
    locks: { request: vi.fn(async (_name: string, _options: LockOptions, action: () => Promise<void>) => action()) },
  });
});
afterEach(() => { vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllGlobals(); });

describe("isolated platform authentication", () => {
  it("uses only platform endpoints and its own Web Lock without exposing csrf in identity", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(json(session()));
    const client = createPlatformClient(fetcher);
    await client.login("platform-test", "synthetic-password");
    expect(navigator.locks.request).toHaveBeenCalledWith(
      "tradeos-platform-authentication", { mode: "exclusive" }, expect.any(Function),
    );
    const [url, options] = fetcher.mock.calls[0]!;
    expect(url).toBe("/api/platform/auth/login");
    expect(options?.credentials).toBe("same-origin");
    expect(options?.cache).toBe("no-store");
    expect(new Headers(options?.headers).get("X-TradeOS-Request")).toBe("1");
    expect(new Headers(options?.headers).get("Content-Type")).toBe("application/json");
    expect(client.snapshot().identity?.role).toBe("platform_admin");
    expect(client.snapshot().identity).not.toHaveProperty("csrf_token");
    expect(FakeChannel.all[0]?.name).toBe("tradeos-platform-session-invalidation");
    expect(FakeChannel.all[0]?.postMessage).toHaveBeenCalledWith("invalidate");
  });

  it("rejects an enterprise identity instead of treating it as platform authority", async () => {
    const client = createPlatformClient(vi.fn<typeof fetch>().mockResolvedValue(
      json({ ...session(), role: "boss" }),
    ));
    await expect(client.login("enterprise-test", "synthetic-password")).rejects.toThrow("平台登录失败");
    expect(client.snapshot().identity).toBeNull();
  });

  it("clears private identity immediately when an overview request receives 401", async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(json(session()))
      .mockResolvedValueOnce(json({ code: "authentication_required" }, 401));
    const client = createPlatformClient(fetcher);
    await client.login("platform-test", "synthetic-password");
    const listener = vi.fn();
    client.subscribe(listener);
    await expect(client.overview()).rejects.toThrow();
    expect(client.snapshot().identity).toBeNull();
    expect(listener).toHaveBeenCalled();
  });

  it("does not publish a delayed overview after another tab invalidates the session", async () => {
    let finish!: (response: Response) => void;
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(json(session()))
      .mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    const client = createPlatformClient(fetcher);
    const stop = client.listen();
    await client.login("platform-test", "synthetic-password");
    const pending = client.overview();
    FakeChannel.all[0]!.invalidate();
    finish(json(overview()));
    await expect(pending).rejects.toThrow();
    expect(client.snapshot().identity).toBeNull();
    stop();
    expect(FakeChannel.all[0]?.close).toHaveBeenCalledOnce();
  });

  it("keeps logout failure explicit and retries using freshly read csrf", async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(json(session()))
      .mockResolvedValueOnce(json({ ...session(), csrf_token: "fresh-csrf-one" }))
      .mockResolvedValueOnce(json({}, 503))
      .mockResolvedValueOnce(json({ ...session(), csrf_token: "fresh-csrf-two" }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    const client = createPlatformClient(fetcher);
    await client.login("platform-test", "synthetic-password");
    await expect(client.logout()).rejects.toThrow("退出未完成");
    expect(client.snapshot().identity).toBeNull();
    expect(client.snapshot().mutation).toBeNull();
    await expect(client.logout()).resolves.toBeUndefined();
    const logoutRequests = fetcher.mock.calls.filter(([url]) => url === "/api/platform/auth/logout");
    expect(logoutRequests).toHaveLength(2);
    expect(new Headers(logoutRequests[0]![1]?.headers).get("X-CSRF-Token")).toBe("fresh-csrf-one");
    expect(new Headers(logoutRequests[1]![1]?.headers).get("X-CSRF-Token")).toBe("fresh-csrf-two");
  });

  it("rejects overlapping mutations while the shared lock is waiting", async () => {
    let enter!: () => void;
    vi.stubGlobal("navigator", {
      locks: { request: vi.fn((_name: string, _options: LockOptions, action: () => Promise<void>) =>
        new Promise<void>((resolve, reject) => { enter = () => { void action().then(resolve, reject); }; })) },
    });
    const client = createPlatformClient(vi.fn<typeof fetch>().mockResolvedValue(json(session())));
    const pending = client.login("platform-test", "synthetic-password");
    await expect(client.logout()).rejects.toThrow("正在处理中");
    enter();
    await pending;
    expect(client.snapshot().identity?.username).toBe("platform-test");
  });

  it("clears the identity when the verified session expires", async () => {
    const client = createPlatformClient(vi.fn<typeof fetch>().mockResolvedValue(json(session())));
    await client.restore();
    expect(client.snapshot().identity).not.toBeNull();
    await vi.advanceTimersByTimeAsync(60_001);
    expect(client.snapshot().identity).toBeNull();
  });

  it("never turns a service failure into an empty successful overview", async () => {
    const client = createPlatformClient(vi.fn<typeof fetch>()
      .mockResolvedValueOnce(json(session()))
      .mockResolvedValueOnce(json({}, 503)));
    await client.restore();
    await expect(client.overview()).rejects.toThrow("企业概览暂不可用");
  });
});
