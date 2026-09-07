import type { components } from "./api";
import {
  apiBaseUrl, apiClient, clearAuthenticatedIdentity, configureAuthenticatedIdentity,
  configureSessionCsrf, currentIdentity, sessionCsrf,
} from "./client";

export { currentIdentity };
type SessionResponse = components["schemas"]["SessionResponse"];
type LoginRequest = components["schemas"]["LoginRequest"];
let channel: BroadcastChannel | null = null;

export function listenForSessionInvalidation(): () => void {
  if (typeof BroadcastChannel === "undefined") return () => {};
  channel?.close();
  const owned = new BroadcastChannel("tradeos-session-invalidation");
  channel = owned;
  owned.onmessage = (event: MessageEvent) => {
    if (event.data === "invalidate") clearAuthenticatedIdentity();
  };
  return () => { owned.close(); if (channel === owned) channel = null; };
}
function broadcastInvalidation(): void {
  if (channel) channel.postMessage("invalidate");
  else if (typeof BroadcastChannel !== "undefined") {
    const temporary = new BroadcastChannel("tradeos-session-invalidation");
    temporary.postMessage("invalidate"); temporary.close();
  }
}
function acceptSession(session: SessionResponse): void {
  if (!session.csrf_token || !session.employee.is_active || !Number.isFinite(Date.parse(session.expires_at)) || Date.parse(session.expires_at) <= Date.now()) throw new Error("会话资料无效，请重新登录");
  configureSessionCsrf(session.csrf_token);
  configureAuthenticatedIdentity(session.employee.tenant_id, session.employee.employee_id);
}
async function request(path: string, method: string, body?: LoginRequest): Promise<Response> {
  const headers = new Headers({ "X-TradeOS-Request": "1" });
  if (body) headers.set("Content-Type", "application/json");
  if (method !== "GET" && sessionCsrf()) headers.set("X-CSRF-Token", sessionCsrf()!);
  return fetch(`${apiBaseUrl()}${path}`, { method, headers, credentials: "same-origin", cache: "no-store", ...(body ? { body: JSON.stringify(body) } : {}) });
}
export async function restoreSession(): Promise<void> {
  const generation = apiClient.identitySnapshot().generation;
  try {
    const response = await request("/auth/session", "GET");
    const session = response.ok ? await response.json() as SessionResponse : null;
    if (generation !== apiClient.identitySnapshot().generation) return;
    if (response.status === 401) { clearAuthenticatedIdentity(); return; }
    if (!session) throw new Error();
    acceptSession(session);
  } catch {
    if (generation !== apiClient.identitySnapshot().generation) return;
    clearAuthenticatedIdentity();
    throw new Error("无法恢复会话，请检查本机服务后重试");
  }
}
export async function login(username: string, password: string): Promise<void> {
  const generation = apiClient.identitySnapshot().generation;
  try {
    const response = await request("/auth/login", "POST", { username, password });
    const session = response.ok ? await response.json() as SessionResponse : null;
    if (generation !== apiClient.identitySnapshot().generation) throw new Error("会话已变化，请重新登录");
    if (!session) throw new Error(response.status === 429 ? "尝试次数过多，请稍后重试" : "登录失败，请检查账号、密码及本机服务");
    // cookie轮换撤销其他标签的旧会话，只广播失效事件。
    broadcastInvalidation();
    acceptSession(session);
  } catch {
    if (generation === apiClient.identitySnapshot().generation) clearAuthenticatedIdentity();
    throw new Error("登录失败，请检查账号、密码及本机服务；尝试过多时请稍后重试");
  }
}
export async function logout(): Promise<void> {
  let completed: boolean;
  try { completed = (await request("/auth/logout", "POST")).status === 204; }
  catch { completed = false; }
  finally { clearAuthenticatedIdentity(); broadcastInvalidation(); }
  if (!completed) throw new Error("退出未完成：无法确认服务器会话已撤销，请恢复服务后重试退出");
}
