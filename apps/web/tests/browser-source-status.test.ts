import { createApp, nextTick, type App } from "vue";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
const auth = vi.hoisted(() => ({
  revision: 1, listeners: new Set<() => void>(),
  snapshot() { return { revision: this.revision, identity: this.revision === 1 ? {} : null, mutation: null }; },
  subscribe(fn: () => void) { this.listeners.add(fn); return () => this.listeners.delete(fn); },
  dataSources: vi.fn(), checkDataSourceUsage: vi.fn(), checkDataSourceBrowser: vi.fn(),
}));
vi.mock("../src/api/platform-auth", () => ({ platformClient: auth, PlatformDataSourceError: class extends Error {} }));
import PlatformDataSources from "../src/components/PlatformDataSources.vue";
const source = (kind = "session", status = "not_checked") => ({
  definition: { source_id: "ungm", name: "UNGM", category: "国际项目采购", access_methods: ["登录浏览器"], selection: "selected", credential_fields: [], credential_mode: "public", integration_status: "planned" },
  status_kind: kind, can_check_browser: kind === "session", browser: { status, checked_at: status === "not_checked" ? null : new Date().toISOString() },
  can_edit: false, can_edit_note: false, configured: false, revision: 0, test_status: "not_run",
});
let app: App | undefined;
const settle = async () => { for (let i = 0; i < 10; i++) { await Promise.resolve(); await nextTick(); } };
beforeEach(() => { vi.clearAllMocks(); auth.revision = 1; });
afterEach(() => { app?.unmount(); document.body.replaceChildren(); });
async function mount(item = source()) {
  auth.dataSources.mockResolvedValue({ generated_at: new Date().toISOString(), sources: [item] });
  const root = document.createElement("div"); document.body.append(root);
  app = createApp(PlatformDataSources); app.mount(root); await settle(); return root;
}
it("tests login explicitly and shows no quota for a session", async () => {
  const root = await mount(); const row = root.querySelector('[data-source-id="ungm"]')!;
  expect(row.textContent).toContain("尚未测试登录");
  expect(row.textContent).not.toContain("额度");
  expect(auth.checkDataSourceBrowser).not.toHaveBeenCalled();
  auth.checkDataSourceBrowser.mockResolvedValue(source("session", "available"));
  Array.from(row.querySelectorAll("button")).find(b => b.textContent?.includes("测试登录"))!.click(); await settle();
  expect(row.textContent).toContain("登录有效");
  expect(row.textContent).toContain("上次检查");
});
it("shows website availability for a public source", async () => {
  const root = await mount(source("website"));
  const row = root.querySelector('[data-source-id="ungm"]')!;
  expect(row.textContent).toContain("网站尚未测试");
  expect(row.textContent).not.toContain("额度");
});
it("clears green status when a new test fails", async () => {
  const root = await mount(source("session", "available"));
  auth.checkDataSourceBrowser.mockRejectedValue(new Error("private-error-canary"));
  Array.from(root.querySelectorAll("button")).find(b => b.textContent?.includes("测试登录"))!.click(); await settle();
  expect(root.textContent).toContain("暂时无法验证");
  expect(root.textContent).not.toContain("登录有效");
  expect(root.textContent).not.toContain("private-error-canary");
});
it("drops results after identity changes", async () => {
  let resolve!: (item: ReturnType<typeof source>) => void;
  auth.checkDataSourceBrowser.mockImplementation(() => new Promise(done => { resolve = done; }));
  const root = await mount();
  Array.from(root.querySelectorAll("button")).find(b => b.textContent?.includes("测试登录"))!.click(); await settle();
  auth.revision = 2; for (const fn of auth.listeners) fn();
  resolve(source("session", "available")); await settle();
  expect(root.textContent).not.toContain("登录有效");
});
