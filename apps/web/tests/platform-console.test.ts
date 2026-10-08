import { createApp, nextTick, type App } from "vue";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { components } from "../src/api/platform-api";

type Overview = components["schemas"]["PlatformOverview"];
const auth = vi.hoisted(() => {
  let identity: { username: string; display_name: string; role: "platform_admin"; expires_at: string } | null = null;
  let revision = 0;
  const listeners = new Set<() => void>();
  return {
    snapshot: () => ({ identity, revision, mutation: null }),
    subscribe: (fn: () => void) => { listeners.add(fn); return () => listeners.delete(fn); },
    supportsAuthentication: () => true,
    listen: () => () => {},
    restore: vi.fn(async () => {
      identity = { username: "platform-test", display_name: "测试平台管理员", role: "platform_admin", expires_at: "2099-01-01T00:00:00Z" };
      for (const listener of listeners) listener();
    }),
    login: vi.fn(),
    logout: vi.fn(),
    overview: vi.fn(),
    invalidate: () => {
      identity = null;
      revision += 1;
      for (const listener of listeners) listener();
    },
    reset: () => { identity = null; revision = 0; listeners.clear(); },
  };
});
vi.mock("../src/api/platform-auth", () => ({ platformClient: auth }));
import PlatformConsole from "../src/components/PlatformConsole.vue";

const availableEnterprise = {
  tenant_id: "tenant-synthetic", name: "测试企业", enabled: true, available: true,
  active_members: 3, admins: 1, employees: 2, customers: 0, validated_needs: 0,
  opportunities: 0, active_tasks: 0,
  members: [{ name: "测试管理员", role: "admin" as const }, { name: "测试员工", role: "employee" as const }],
};
const sample = (): Overview => ({
  generated_at: "2026-10-08T00:00:00Z",
  enterprises: [availableEnterprise, {
    tenant_id: "tenant-unavailable", name: "暂不可读企业", enabled: true, available: false,
    active_members: null, admins: null, employees: null, customers: null, validated_needs: null,
    opportunities: null, active_tasks: null, members: [],
  }],
});
let app: App | null = null;
beforeEach(() => { auth.reset(); auth.overview.mockReset(); auth.logout.mockReset(); });
afterEach(() => { app?.unmount(); app = null; document.body.replaceChildren(); });
async function mount(): Promise<HTMLElement> {
  const root = document.createElement("div");
  document.body.replaceChildren(root);
  app = createApp(PlatformConsole);
  app.mount(root);
  await settle();
  return root;
}
async function settle(): Promise<void> {
  for (let i = 0; i < 6; i += 1) { await Promise.resolve(); await nextTick(); }
}
describe("platform console", () => {
  it("renders platform identity and enterprise hierarchy, with unavailable data kept distinct from zero", async () => {
    auth.overview.mockResolvedValue(sample());
    const root = await mount();
    expect(root.textContent).toContain("平台管理员 · 测试平台管理员");
    expect(root.textContent).toContain("测试管理员");
    expect(root.textContent).toContain("企业管理员");
    expect(root.textContent).toContain("测试员工");
    const cards = root.querySelectorAll(".enterprise-card");
    expect(cards).toHaveLength(2);
    expect(cards[0]!.querySelector(".enterprise-counts")?.textContent).toContain("客户0");
    expect(cards[1]!.textContent).toContain("暂无法读取此企业");
    expect(cards[1]!.querySelector(".enterprise-counts")).toBeNull();
    expect(root.querySelector<HTMLAnchorElement>('a[href="/"]')?.textContent).toBe("企业工作台");
  });

  it("shows an omitted counter as unknown even when the enterprise snapshot is available", async () => {
    const data = sample();
    delete data.enterprises[0]!.customers;
    auth.overview.mockResolvedValue(data);
    const root = await mount();
    const customers = Array.from(root.querySelectorAll(".enterprise-counts div"))
      .find((item) => item.querySelector("dt")?.textContent === "客户");
    expect(customers?.querySelector("dd")?.textContent).toBe("暂不可用");
  });

  it("clears enterprise details after session invalidation", async () => {
    auth.overview.mockResolvedValue(sample());
    const root = await mount();
    expect(root.textContent).toContain("测试企业");
    auth.invalidate();
    await settle();
    expect(root.textContent).not.toContain("测试企业");
    expect(root.textContent).not.toContain("测试管理员");
    expect(root.textContent).toContain("平台管理员登录");
  });

  it("shows a service error without claiming there are zero enterprises", async () => {
    auth.overview.mockRejectedValue(new Error("unavailable"));
    const root = await mount();
    expect(root.textContent).toContain("企业概览暂不可用");
    expect(root.textContent).not.toContain("0 家已注册企业");
    expect(root.textContent).not.toContain("当前没有已注册企业");
    expect(root.querySelectorAll(".enterprise-card")).toHaveLength(0);
  });

  it("drops a pending refresh when session invalidation arrives", async () => {
    let resolve!: (value: Overview) => void;
    auth.overview.mockImplementation(() => new Promise<Overview>((done) => { resolve = done; }));
    const root = await mount();
    auth.invalidate();
    resolve(sample());
    await settle();
    expect(root.textContent).not.toContain("测试企业");
    expect(root.textContent).toContain("平台管理员登录");
  });

  it("offers retry when logout fails and does not claim the server session was revoked", async () => {
    auth.overview.mockResolvedValue(sample());
    auth.logout.mockImplementation(async () => { auth.invalidate(); throw new Error("offline"); });
    const root = await mount();
    const button = Array.from(root.querySelectorAll("button")).find((item) => item.textContent?.includes("退出平台"))!;
    button.click();
    await settle();
    expect(root.textContent).toContain("退出未完成");
    expect(root.textContent).toContain("重试退出");
    expect(root.textContent).not.toContain("测试企业");
  });
});
