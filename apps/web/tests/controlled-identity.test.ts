import { afterEach, describe, expect, it, vi } from "vitest";

const config = JSON.stringify({ owner: "a".repeat(32), tenantId: "tn_controlled", identities: [{ employeeId: "emp_a", label: "老板甲" }, { employeeId: "emp_b", label: "老板乙" }], schedulerUrl: "http://127.0.0.1:19010" });
afterEach(() => { vi.unstubAllEnvs(); vi.resetModules(); });
describe("受控角色", () => {
  it("只允许显式本次员工，切换仍开发身份并使旧generation失效", async () => {
    vi.stubEnv("VITE_API_BASE_URL", "http://127.0.0.1:19000"); vi.stubEnv("DEV", true); vi.stubEnv("PROD", false); vi.stubEnv("VITE_CONTROLLED_CONFIG", config);
    const client = await import("../src/api/client");
    expect(client.configureControlledIdentity).toBeTypeOf("function");
    const old = client.apiClient.identitySnapshot();
    let changed = 0;
    const unsubscribe = client.apiClient.subscribeIdentity(() => { changed++; });
    client.configureControlledIdentity("emp_b");
    const next = client.apiClient.identitySnapshot();
    expect(next.identity).toEqual({ employeeId: "emp_b", mode: "fixed-dev", tenantId: "tn_controlled" });
    expect(next.generation).toBeGreaterThan(old.generation);
    expect(changed).toBe(1);
    expect(() => client.configureControlledIdentity("outsider")).toThrow();
    unsubscribe();
  });
  it("生产模式和缺少owner配置均拒绝", async () => {
    vi.stubEnv("VITE_API_BASE_URL", "http://127.0.0.1:19000"); vi.stubEnv("DEV", false); vi.stubEnv("PROD", true); vi.stubEnv("VITE_CONTROLLED_CONFIG", config);
    const client = await import("../src/api/client");
    expect(client.configureControlledIdentity).toBeTypeOf("function");
    expect(() => client.configureControlledIdentity("emp_b")).toThrow();
    vi.stubEnv("VITE_API_BASE_URL", "http://127.0.0.1:19000"); vi.stubEnv("DEV", true); vi.stubEnv("PROD", false); vi.stubEnv("VITE_CONTROLLED_CONFIG", "");
    expect(() => client.configureControlledIdentity("emp_b")).toThrow();
  });
});
