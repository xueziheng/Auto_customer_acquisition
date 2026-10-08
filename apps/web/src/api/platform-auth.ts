import type { components as PlatformComponents } from "./platform-api";
import type { components } from "./api";

type PlatformSession = PlatformComponents["schemas"]["PlatformSessionResponse"];
export type PlatformOverview = PlatformComponents["schemas"]["PlatformOverview"];
type LoginRequest = components["schemas"]["LoginRequest"];
type PlatformIdentity = Pick<PlatformSession, "username" | "display_name" | "role" | "expires_at">;
type Mutation = "login" | "logout" | null;

export function createPlatformClient(fetcher: typeof fetch = (input, init) => fetch(input, init)) {
  let identity: PlatformIdentity | null = null;
  let csrf: string | null = null;
  let revision = 0;
  let mutation: Mutation = null;
  let expiry: ReturnType<typeof setTimeout> | undefined;
  let channel: BroadcastChannel | null = null;
  const listeners = new Set<() => void>();

  const snapshot = () => ({ identity, mutation, revision });
  function notify(): void { for (const listener of listeners) listener(); }
  function clear(): void {
    identity = null;
    csrf = null;
    revision += 1;
    clearTimeout(expiry);
    notify();
  }
  function accept(session: PlatformSession): void {
    const remaining = Date.parse(session.expires_at) - Date.now();
    if (!session.csrf_token || !session.username || session.role !== "platform_admin"
      || !Number.isFinite(remaining) || remaining <= 0) {
      throw new Error("平台会话无效，请重新登录");
    }
    csrf = session.csrf_token;
    identity = {
      username: session.username, display_name: session.display_name,
      role: session.role, expires_at: session.expires_at,
    };
    clearTimeout(expiry);
    expiry = setTimeout(clear, Math.min(remaining, 2_147_483_647));
    notify();
  }
  function invalidateOtherTabs(): void {
    if (channel) channel.postMessage("invalidate");
    else if (typeof BroadcastChannel !== "undefined") {
      const temporary = new BroadcastChannel("tradeos-platform-session-invalidation");
      temporary.postMessage("invalidate");
      temporary.close();
    }
  }
  function listen(): () => void {
    if (typeof BroadcastChannel === "undefined") return () => {};
    channel?.close();
    const owned = new BroadcastChannel("tradeos-platform-session-invalidation");
    channel = owned;
    owned.onmessage = (event: MessageEvent) => {
      // 正等待独立会话锁的变更随后会重读 cookie；不得被前一个变更的广播取消。
      if (event.data === "invalidate" && (identity || mutation === null)) clear();
    };
    return () => {
      owned.close();
      if (channel === owned) channel = null;
    };
  }
  async function request(path: string, method = "GET", body?: LoginRequest, token = csrf): Promise<Response> {
    const headers = new Headers({ "X-TradeOS-Request": "1" });
    if (method !== "GET") headers.set("Content-Type", "application/json");
    if (method !== "GET" && token) headers.set("X-CSRF-Token", token);
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15_000);
    try {
      const response = await fetcher(`/api/platform${path}`, {
        method, headers, credentials: "same-origin", cache: "no-store",
        signal: controller.signal, ...(body ? { body: JSON.stringify(body) } : {}),
      });
      if (response.status === 401) clear();
      return response;
    } finally { clearTimeout(timeout); }
  }
  function supportsAuthentication(): boolean {
    return typeof globalThis.navigator?.locks?.request === "function";
  }
  async function change(kind: Exclude<Mutation, null>, operation: () => Promise<void>): Promise<void> {
    if (mutation) throw new Error("平台会话变更正在处理中");
    if (!supportsAuthentication()) throw new Error("请使用支持安全会话锁的新版 Chrome 或 Edge");
    mutation = kind;
    clear();
    try {
      await navigator.locks.request("tradeos-platform-authentication", { mode: "exclusive" }, operation);
    } finally {
      mutation = null;
      notify();
    }
  }
  async function restore(): Promise<void> {
    if (mutation) throw new Error("平台会话变更正在处理中");
    const expected = revision;
    try {
      const response = await request("/auth/session");
      if (response.status === 401 || expected !== revision) return;
      if (!response.ok) throw new Error();
      const session = await response.json() as PlatformSession;
      if (expected === revision) accept(session);
    } catch {
      if (expected !== revision) return;
      clear();
      throw new Error("无法恢复平台会话，请重试");
    }
  }
  async function login(username: string, password: string): Promise<void> {
    await change("login", async () => {
      const expected = revision;
      try {
        const response = await request("/auth/login", "POST", { username, password }, null);
        if (!response.ok) throw new Error();
        const session = await response.json() as PlatformSession;
        if (expected !== revision) throw new Error();
        invalidateOtherTabs();
        accept(session);
      } catch {
        if (expected === revision) clear();
        throw new Error("平台登录失败，请检查账号、密码或稍后重试");
      }
    });
  }
  async function logout(): Promise<void> {
    await change("logout", async () => {
      let completed = false;
      try {
        const response = await request("/auth/session");
        if (response.status === 401) completed = true;
        else if (response.ok) {
          const session = await response.json() as PlatformSession;
          if (session.csrf_token) {
            completed = (await request("/auth/logout", "POST", undefined, session.csrf_token)).status === 204;
          }
        }
      } catch { completed = false; }
      finally { clear(); invalidateOtherTabs(); }
      if (!completed) throw new Error("退出未完成，尚不能确认服务器已撤销会话，请重试退出");
    });
  }
  async function overview(): Promise<PlatformOverview> {
    if (!identity || mutation) throw new Error("请先登录平台管理员");
    const expected = revision;
    const response = await request("/overview");
    if (!response.ok || expected !== revision) throw new Error("企业概览暂不可用，请重试");
    const result = await response.json() as PlatformOverview;
    if (expected !== revision || !Array.isArray(result.enterprises)
      || !Number.isFinite(Date.parse(result.generated_at))) throw new Error("企业概览暂不可用，请重试");
    return result;
  }
  return {
    snapshot, supportsAuthentication, listen, restore, login, logout, overview,
    subscribe(listener: () => void): () => void {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
  };
}

export const platformClient = createPlatformClient();
