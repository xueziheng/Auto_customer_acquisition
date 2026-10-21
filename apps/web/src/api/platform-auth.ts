import type { components as PlatformComponents, paths as PlatformPaths } from "./platform-api";
import type { components } from "./api";

type PlatformSession = PlatformComponents["schemas"]["PlatformSessionResponse"];
export type PlatformOverview = PlatformComponents["schemas"]["PlatformOverview"];
export type PlatformDataSources = PlatformComponents["schemas"]["DataSourcesView"];
export type PlatformDataSource = PlatformComponents["schemas"]["DataSourceView"];
export type PlatformCredentialUpdate = PlatformPaths["/data-sources/{source_id}/credentials"]["put"]["requestBody"]["content"]["application/json"];
export type PlatformCredentialDelete = PlatformPaths["/data-sources/{source_id}/credentials"]["delete"]["requestBody"]["content"]["application/json"];
export type PlatformUsageCheck = PlatformPaths["/data-sources/{source_id}/usage-check"]["post"]["requestBody"]["content"]["application/json"];
export type PlatformDataSourceNote = PlatformComponents["schemas"]["DataSourceNoteView"];
export type PlatformNoteUpdate = PlatformPaths["/data-sources/{source_id}/note"]["put"]["requestBody"]["content"]["application/json"];
type BrowserCheck = PlatformPaths["/data-sources/{source_id}/browser-check"]["post"]["requestBody"]["content"]["application/json"];
export type PlatformModelApi = PlatformComponents["schemas"]["ModelApiView"];
export type PlatformModelUpdate = PlatformPaths["/model-api"]["put"]["requestBody"]["content"]["application/json"];
export type PlatformModelCommand = PlatformPaths["/model-api/test"]["post"]["requestBody"]["content"]["application/json"];
export type ModelCatalog = PlatformComponents["schemas"]["ModelCatalogView"];
export type ModelEntry = PlatformComponents["schemas"]["ModelCatalogEntry"];
export type ModelSave = PlatformPaths["/model-catalog"]["post"]["requestBody"]["content"]["application/json"];
export type CompanyModelUpdate = PlatformPaths["/company-models/{company_id}"]["put"]["requestBody"]["content"]["application/json"];
export type InventoryQuota = PlatformComponents["schemas"]["InventoryQuotaView"];
type InventoryQuotaCommand = PlatformPaths["/inventory/companies/{tenant_id}/quota"]["put"]["requestBody"]["content"]["application/json"];
type LoginRequest = components["schemas"]["LoginRequest"];
type PlatformIdentity = Pick<PlatformSession, "username" | "display_name" | "role" | "expires_at">;
type Mutation = "login" | "logout" | null;

export class PlatformDataSourceError extends Error {
  constructor(readonly status: number) { super("数据来源请求未完成"); }
}

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
  async function request(path: string, method = "GET", body?: LoginRequest | PlatformCredentialUpdate | PlatformCredentialDelete | PlatformNoteUpdate | BrowserCheck | PlatformModelUpdate | PlatformModelCommand | ModelSave | CompanyModelUpdate | InventoryQuotaCommand, token = csrf): Promise<Response> {
    const expected = revision;
    const headers = new Headers({ "X-TradeOS-Request": "1" });
    if (method !== "GET") headers.set("Content-Type", "application/json");
    if (method !== "GET" && token) headers.set("X-CSRF-Token", token);
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), path.endsWith("/browser-check") || path.endsWith("/test") || path.endsWith("/test-agent") ? 120_000 : 15_000);
    try {
      const response = await fetcher(`/api/platform${path}`, {
        method, headers, credentials: "same-origin", cache: "no-store",
        signal: controller.signal, ...(body ? { body: JSON.stringify(body) } : {}),
      });
      if (response.status === 401 && expected === revision) clear();
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
  async function dataSources(): Promise<PlatformDataSources> {
    if (!identity || mutation) throw new PlatformDataSourceError(401);
    const expected = revision;
    const response = await request("/data-sources");
    if (expected !== revision) throw new PlatformDataSourceError(401);
    if (!response.ok) throw new PlatformDataSourceError(response.status);
    const result = await response.json() as PlatformDataSources;
    if (expected !== revision || !Array.isArray(result.sources)
      || !Number.isFinite(Date.parse(result.generated_at))) throw new PlatformDataSourceError(503);
    return result;
  }
  async function saveDataSourceCredentials(sourceId: string, update: PlatformCredentialUpdate): Promise<PlatformDataSource> {
    if (!identity || mutation) throw new PlatformDataSourceError(401);
    const expected = revision;
    const response = await request(`/data-sources/${encodeURIComponent(sourceId)}/credentials`, "PUT", update);
    if (expected !== revision) throw new PlatformDataSourceError(401);
    if (!response.ok) throw new PlatformDataSourceError(response.status);
    const result = await response.json() as PlatformDataSource;
    if (expected !== revision || result.definition?.source_id !== sourceId
      || !Number.isSafeInteger(result.revision) || result.revision <= update.expected_revision) throw new PlatformDataSourceError(503);
    return result;
  }
  async function deleteDataSourceCredentials(sourceId: string, update: PlatformCredentialDelete): Promise<PlatformDataSource> {
    if (!identity || mutation) throw new PlatformDataSourceError(401);
    const expected = revision;
    const response = await request(`/data-sources/${encodeURIComponent(sourceId)}/credentials`, "DELETE", update);
    if (expected !== revision) throw new PlatformDataSourceError(401);
    if (!response.ok) throw new PlatformDataSourceError(response.status);
    const result = await response.json() as PlatformDataSource;
    if (expected !== revision || result.definition?.source_id !== sourceId || result.configured
      || !Number.isSafeInteger(result.revision) || result.revision <= update.expected_revision) throw new PlatformDataSourceError(503);
    return result;
  }
  async function checkDataSourceUsage(sourceId: string, command: PlatformUsageCheck): Promise<PlatformDataSource> {
    if (!identity || mutation) throw new PlatformDataSourceError(401);
    const expected = revision;
    const response = await request(`/data-sources/${encodeURIComponent(sourceId)}/usage-check`, "POST", command);
    if (expected !== revision) throw new PlatformDataSourceError(401);
    if (!response.ok) throw new PlatformDataSourceError(response.status);
    const result = await response.json() as PlatformDataSource;
    if (expected !== revision || result.definition?.source_id !== sourceId
      || result.revision !== command.expected_revision || !result.configured) throw new PlatformDataSourceError(503);
    return result;
  }
  async function checkDataSourceBrowser(sourceId: string): Promise<PlatformDataSource> {
    if (!identity || mutation) throw new PlatformDataSourceError(401);
    const expected = revision;
    const response = await request(`/data-sources/${encodeURIComponent(sourceId)}/browser-check`, "POST", {});
    if (expected !== revision) throw new PlatformDataSourceError(401);
    if (!response.ok) throw new PlatformDataSourceError(response.status);
    const result = await response.json() as PlatformDataSource;
    if (expected !== revision || result.definition?.source_id !== sourceId || !result.browser)
      throw new PlatformDataSourceError(503);
    return result;
  }
  async function saveDataSourceNote(sourceId: string, command: PlatformNoteUpdate): Promise<PlatformDataSourceNote> {
    if (!identity || mutation) throw new PlatformDataSourceError(401);
    const expected = revision;
    const response = await request(`/data-sources/${encodeURIComponent(sourceId)}/note`, "PUT", command);
    if (expected !== revision) throw new PlatformDataSourceError(401);
    if (!response.ok) throw new PlatformDataSourceError(response.status);
    const result = await response.json() as PlatformDataSourceNote;
    if (expected !== revision || typeof result.text !== "string" || result.text.length > 2000
      || result.revision !== command.expected_revision + 1
      || !Number.isFinite(Date.parse(result.updated_at ?? ""))) throw new PlatformDataSourceError(503);
    return result;
  }
  async function modelApi(action: "get" | "save" | "test" | "apply" = "get", body?: PlatformModelUpdate | PlatformModelCommand): Promise<PlatformModelApi> {
    if (!identity || mutation) throw new PlatformDataSourceError(401);
    const expected = revision;
    const path = "/model-api" + (action === "test" || action === "apply" ? `/${action}` : "");
    const response = await request(path, action === "get" ? "GET" : action === "save" ? "PUT" : "POST", body);
    if (expected !== revision) throw new PlatformDataSourceError(401);
    if (!response.ok) throw new PlatformDataSourceError(response.status);
    const value = await response.json() as PlatformModelApi;
    if (expected !== revision || !Number.isSafeInteger(value.revision) || typeof value.configured !== "boolean") throw new PlatformDataSourceError(503);
    return value;
  }
  async function catalogRequest<T>(path: string, method = "GET", body?: ModelSave | PlatformModelCommand | CompanyModelUpdate | InventoryQuotaCommand): Promise<T> {
    if (!identity || mutation) throw new PlatformDataSourceError(401);
    const expected = revision;
    const response = await request(path, method, body);
    if (expected !== revision) throw new PlatformDataSourceError(401);
    if (!response.ok) throw new PlatformDataSourceError(response.status);
    const value = await response.json() as T;
    if (expected !== revision) throw new PlatformDataSourceError(401);
    return value;
  }
  const inventoryQuota = (company: string) => catalogRequest<InventoryQuota>(`/inventory/companies/${encodeURIComponent(company)}/quota`);
  const setInventoryQuota = (company: string, daily_limit: number) => catalogRequest<InventoryQuota>(`/inventory/companies/${encodeURIComponent(company)}/quota`, "PUT", { daily_limit });
  const modelCatalog = () => catalogRequest<ModelCatalog>("/model-catalog");
  const saveModel = (id: string | null, body: ModelSave) => catalogRequest<ModelEntry>("/model-catalog" + (id ? `/${encodeURIComponent(id)}` : ""), id ? "PUT" : "POST", body);
  const testModel = (id: string, revision: number, agent = false) => catalogRequest<ModelEntry>(`/model-catalog/${encodeURIComponent(id)}/${agent ? 'test-agent' : 'test'}`, "POST", { expected_revision: revision });
  const deleteModel = (id: string, revision: number) => catalogRequest<ModelCatalog>(`/model-catalog/${encodeURIComponent(id)}/delete`, "POST", { expected_revision: revision });
  const bindModel = (company: string, body: CompanyModelUpdate) => catalogRequest<ModelCatalog>(`/company-models/${encodeURIComponent(company)}`, "PUT", body);
  return {
    inventoryQuota, setInventoryQuota, modelCatalog, saveModel, testModel, deleteModel, bindModel, modelApi, snapshot, supportsAuthentication, listen, restore, login, logout, overview,
    dataSources, saveDataSourceCredentials, deleteDataSourceCredentials, checkDataSourceUsage, checkDataSourceBrowser, saveDataSourceNote,
    subscribe(listener: () => void): () => void {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
  };
}

export const platformClient = createPlatformClient();
