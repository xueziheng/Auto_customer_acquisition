import type { components } from "./api";
import {
  apiBaseUrl, apiClient, clearAuthenticatedIdentity, configureAuthenticatedIdentity,
  configureSessionCsrf, currentIdentity, sessionCsrf,
} from "./client";

export { currentIdentity };
type SessionResponse = components["schemas"]["SessionResponse"];
type LoginRequest = components["schemas"]["LoginRequest"];
let channel: BroadcastChannel | null = null;
export type AuthenticationMutation = "login" | "logout" | null;
let mutation: AuthenticationMutation = null;
const mutationListeners = new Set<() => void>();
export function currentAuthenticationMutation(): AuthenticationMutation { return mutation; }
export function subscribeAuthenticationMutation(listener: () => void): () => void {
  mutationListeners.add(listener);
  return () => { mutationListeners.delete(listener); };
}
export function supportsAuthenticationMutations(): boolean {
  return typeof globalThis.navigator?.locks?.request === "function";
}
function requireIdle(): void {
  if (mutation !== null) throw new Error("会话变更正在处理中，请等待完成后重试");
}
async function changeSession(kind: Exclude<AuthenticationMutation, null>, operation: () => Promise<void>): Promise<void> {
  requireIdle();
  if (!supportsAuthenticationMutations()) throw new Error("当前浏览器不支持 Web Locks，无法安全登录或退出");
  mutation = kind;
  for (const listener of mutationListeners) listener();
  try {
    // 同源标签共享cookie；锁须覆盖响应头处理，防止旧Set-Cookie晚于新登录。
    await navigator.locks.request("tradeos-authentication", { mode: "exclusive" }, operation);
  } finally {
    mutation = null;
    for (const listener of mutationListeners) listener();
  }
}

export function listenForSessionInvalidation(): () => void {
  if (typeof BroadcastChannel === "undefined") return () => {};
  channel?.close();
  const owned = new BroadcastChannel("tradeos-session-invalidation");
  channel = owned;
  owned.onmessage = (event: MessageEvent) => {
    if (event.data === "invalidate" && (currentIdentity() || mutation === null)) clearAuthenticatedIdentity();
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
async function request(path: string, method: string, body?: LoginRequest, csrf: string | null = sessionCsrf()): Promise<Response> {
  const headers = new Headers({ "X-TradeOS-Request": "1" });
  if (body) headers.set("Content-Type", "application/json");
  if (method !== "GET" && csrf) headers.set("X-CSRF-Token", csrf);
  return fetch(`${apiBaseUrl()}${path}`, { method, headers, credentials: "same-origin", cache: "no-store", ...(body ? { body: JSON.stringify(body) } : {}) });
}
export async function restoreSession(): Promise<void> {
  requireIdle();
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
  await changeSession("login", async () => {
    clearAuthenticatedIdentity();
    const generation = apiClient.identitySnapshot().generation;
    try {
      const response = await request("/auth/login", "POST", { username, password });
      const session = response.ok ? await response.json() as SessionResponse : null;
      if (generation !== apiClient.identitySnapshot().generation) throw new Error();
      if (!session) throw new Error();
      broadcastInvalidation();
      acceptSession(session);
    } catch {
      if (generation === apiClient.identitySnapshot().generation) clearAuthenticatedIdentity();
      throw new Error("登录失败，请检查账号、密码及本机服务；尝试过多时请稍后重试");
    }
  });
}
export async function logout(): Promise<void> {
  await changeSession("logout", async () => {
    clearAuthenticatedIdentity();
    const generation = apiClient.identitySnapshot().generation;
    let completed = false;
    try {
      // 只读取材料，不发布身份；既支持失败直接重试，也避免等待锁时cookie已轮换。
      const current = await request("/auth/session", "GET");
      if (current.status === 401) completed = true;
      else if (current.ok) {
        const session = await current.json() as SessionResponse;
        if (session.csrf_token && generation === apiClient.identitySnapshot().generation) {
          completed = (await request("/auth/logout", "POST", undefined, session.csrf_token)).status === 204;
        }
      }
    } catch { completed = false; }
    finally {
      if (generation === apiClient.identitySnapshot().generation) {
        clearAuthenticatedIdentity();
        broadcastInvalidation();
      }
    }
    if (!completed) throw new Error("退出未完成：无法确认服务器会话已撤销，请恢复服务后直接重试退出");
  });
}
