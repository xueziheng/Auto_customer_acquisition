import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { createApp, nextTick, type App } from "vue";

let app: App | undefined;
beforeEach(() => {
  vi.resetModules();
  vi.stubGlobal("navigator", { locks: { request: async (_name: string, _options: unknown, callback: () => Promise<void>) => callback() } });
});
afterEach(() => { app?.unmount(); app = undefined; document.body.replaceChildren(); vi.unstubAllGlobals(); });
async function flush() { for (let index = 0; index < 5; index++) { await nextTick(); await new Promise(resolve => setTimeout(resolve, 0)); } }
const source = {
  definition: {
    source_id: "hunter", name: "Hunter", category: "邮箱与联系人", selection: "selected",
    credential_mode: "api_key",
    website_url: "https://hunter.io/", registration_url: "https://hunter.io/users/sign_up", docs_url: "https://hunter.io/api-documentation/v2",
    access_methods: ["API"], coverage: "全球", description: "企业邮箱查找与验证",
    credential_fields: [{ name: "api_key", label: "API Key" }], integration_status: "partial", integration_note: "邮箱接口已有实现，Discover 待接入",
  },
  configured: true, revision: 3, configuration_version: "configuration-3", updated_at: "2026-10-11T00:00:00Z", updated_by: "xue",
  can_edit_note: true, note: { text: "", revision: 0, updated_at: null, updated_by: null },
  test_status: "passed", last_test_scope: "account", tested_at: "2026-10-11T00:00:00Z", can_edit: true,
};
const publicSource = {
  ...source, configured: false, revision: 0, test_status: "not_run", last_test_scope: null, tested_at: null, updated_at: null, updated_by: null, configuration_version: null, can_edit: false,
  definition: { ...source.definition, source_id: "ungm", name: "UNGM", category: "国际项目采购", credential_mode: "public", credential_fields: [], access_methods: ["公开网页", "浏览器"], website_url: "https://www.ungm.org/Public/Notice", integration_status: "planned", integration_note: "专用采集待接入" },
};

async function setup(save?: (body: unknown, init: RequestInit) => Promise<Response>, extras: (typeof publicSource | typeof source)[] = [], usageCheck?: (init: RequestInit) => Promise<Response>, noteSave?: (path: string, init: RequestInit) => Promise<Response>) {
  const session = { username: "xue", display_name: "管理员", role: "platform_admin", csrf_token: "synthetic-csrf", expires_at: "2099-01-01T00:00:00Z" };
  vi.stubGlobal("fetch", async (input: unknown, init: RequestInit = {}) => {
    const path = String(input);
    if (path.endsWith("/auth/session") || path.endsWith("/auth/login")) return Response.json(session);
    if (path.endsWith("/overview")) return Response.json({ enterprises: [], generated_at: "2026-10-11T00:00:00Z" });
    if (path.endsWith("/data-sources")) return Response.json({ sources: [{ ...source, can_check_usage: Boolean(usageCheck) }, publicSource, ...extras], generated_at: "2026-10-11T00:00:00Z" });
    if (path.endsWith("/note") && noteSave) return noteSave(path, init);
    if (path.endsWith("/hunter/credentials") && save) return save(JSON.parse(String(init.body)), init);
    if (path.endsWith("/hunter/usage-check") && usageCheck) return usageCheck(init);
    return new Response(null, { status: 404 });
  });
  const { default: PlatformConsole } = await import("./PlatformConsole.vue");
  const { platformClient } = await import("../api/platform-auth");
  const host = document.createElement("div"); document.body.append(host);
  app = createApp(PlatformConsole); app.mount(host); await flush();
  return { host, platformClient };
}
function button(host: HTMLElement, text: string) {
  const target = [...host.querySelectorAll("button")].find(item => item.textContent?.includes(text));
  expect(target, `应存在按钮：${text}`).toBeDefined();
  return target!;
}
async function openSources(host: HTMLElement) { button(host, "获客数据来源").click(); await flush(); }

it("平台目录展示不同获取方式，公开页无需密钥，已配置不伪装全部已接通", async () => {
  const { host } = await setup(); await openSources(host);
  expect(host.querySelectorAll("[data-source-id]").length).toBe(2);
  const hunter = host.querySelector('[data-source-id="hunter"]')!;
  const ungm = host.querySelector('[data-source-id="ungm"]')!;
  expect(hunter.textContent).toContain("部分已接入");
  expect(hunter.textContent).toContain("已配置");
  expect(hunter.textContent).toContain("账户验证通过");
  expect(hunter.textContent).not.toContain("邮箱样本通过");
  expect(ungm.textContent).toContain("接口待接入");
  expect(ungm.textContent).toContain("无需登录或密钥");
  expect(ungm.textContent).not.toContain("添加密钥");
  expect(ungm.querySelector("button")?.textContent).toContain("添加备注");
  expect(host.querySelectorAll('[data-source-group]').length).toBe(2);
  expect(hunter.textContent).toContain("••••••••");
  const search = host.querySelector<HTMLInputElement>('input[type="search"]')!;
  search.value = "UNGM"; search.dispatchEvent(new Event("input")); await nextTick();
  expect(host.querySelectorAll("[data-source-id]").length).toBe(1);
  expect(host.querySelector('[data-source-id="hunter"]')).toBeNull();
});

it("没有编辑字段的商业来源和CLI会话不能误报为公开访问", async () => {
  const { host } = await setup(undefined, [
    { ...publicSource, definition: { ...publicSource.definition, source_id: "volza", name: "Volza", credential_mode: "commercial_pending" } },
    { ...publicSource, definition: { ...publicSource.definition, source_id: "codex_cli", name: "Codex CLI", credential_mode: "managed_session" } },
  ]);
  await openSources(host);
  expect(host.querySelector('[data-source-id="volza"]')?.textContent).toContain("待商务开通");
  expect(host.querySelector('[data-source-id="codex_cli"]')?.textContent).toContain("部署会话");
  expect(host.querySelector('[data-source-id="volza"]')?.textContent).not.toContain("公开访问");
  expect(host.querySelector('[data-source-id="codex_cli"]')?.textContent).not.toContain("公开访问");
});

it("删除密钥须先确认整组删除，取消不发送请求，成功后可重新添加", async () => {
  let writes = 0, sent: unknown;
  const { host } = await setup(async (body, init) => {
    writes += 1; sent = body;
    expect(init.method).toBe("DELETE");
    return Response.json({ ...source, configured: false, revision: 4, test_status: "not_run", tested_at: null });
  });
  await openSources(host); button(host, "删除密钥").click(); await nextTick();
  expect(writes).toBe(0);
  expect(host.querySelector('[role="dialog"]')?.textContent).toContain("服务商网站");
  button(host, "取消").click(); await nextTick();
  expect(writes).toBe(0);
  button(host, "删除密钥").click(); await nextTick();
  button(host, "确认删除").click(); await flush();
  expect(sent).toEqual({ expected_revision: 3 });
  expect(writes).toBe(1);
  expect(host.querySelector('[data-source-id="hunter"]')?.textContent).toContain("未配置");
  expect(host.querySelector('[data-source-id="hunter"]')?.textContent).not.toContain("••••••••");
  expect(button(host, "添加密钥")).toBeDefined();
});

it("密钥只随同源CSRF写入请求发送，提交时清空输入且不回显旧值", async () => {
  let finish!: (response: Response) => void;
  let sent: unknown, headers: Headers | undefined;
  const { host } = await setup(async (body, init) => { sent = body; headers = new Headers(init.headers); return new Promise(resolve => { finish = resolve; }); });
  await openSources(host); button(host, "修改密钥").click(); await nextTick();
  const field = host.querySelector<HTMLInputElement>('input[type="password"]')!;
  expect(field.value).toBe("");
  field.value = "synthetic-new-provider-key"; field.dispatchEvent(new Event("input"));
  host.querySelector('[role="dialog"] form')!.dispatchEvent(new Event("submit")); await flush();
  expect(field.value).toBe("");
  expect(sent).toEqual({ expected_revision: 3, credentials: { api_key: "synthetic-new-provider-key" } });
  expect(headers?.get("X-CSRF-Token")).toBe("synthetic-csrf");
  expect(host.innerHTML).not.toContain("synthetic-new-provider-key");
  expect(localStorage.length + sessionStorage.length).toBe(0);
  finish(Response.json({ ...source, revision: 4, test_status: "not_run", tested_at: null })); await flush();
  expect(host.querySelector('[role="dialog"]')).toBeNull();
  expect(host.querySelector('[data-source-id="hunter"]')?.textContent).toContain("尚未测试");
  button(host, "修改密钥").click(); await nextTick();
  expect(host.querySelector<HTMLInputElement>('input[type="password"]')!.value).toBe("");
});

it("取消和切换平台页签清空密钥表单", async () => {
  const { host } = await setup(); await openSources(host);
  button(host, "修改密钥").click(); await nextTick();
  let field = host.querySelector<HTMLInputElement>('input[type="password"]')!;
  field.value = "synthetic-cancelled-key"; field.dispatchEvent(new Event("input"));
  button(host, "取消").click(); await nextTick();
  button(host, "修改密钥").click(); await nextTick();
  field = host.querySelector<HTMLInputElement>('input[type="password"]')!;
  expect(field.value).toBe("");
  field.value = "synthetic-tab-key"; field.dispatchEvent(new Event("input"));
  button(host, "企业与账号").click(); await flush(); await openSources(host);
  expect(host.querySelector('[role="dialog"]')).toBeNull();
});

it("保存冲突不展示原始错误与密钥，并要求重新读取后再编辑", async () => {
  const { host } = await setup(async () => Response.json({ detail: "synthetic-secret-response" }, { status: 409 }));
  await openSources(host); button(host, "修改密钥").click(); await nextTick();
  const field = host.querySelector<HTMLInputElement>('input[type="password"]')!;
  field.value = "synthetic-conflicting-key"; field.dispatchEvent(new Event("input"));
  host.querySelector('[role="dialog"] form')!.dispatchEvent(new Event("submit")); await flush();
  expect(host.textContent).toContain("配置已更新");
  expect(host.innerHTML).not.toContain("synthetic-secret-response");
  expect(host.querySelector<HTMLInputElement>('input[type="password"]')?.value ?? "").toBe("");
});

it("旧身份的迟到保存结果不能污染新平台会话或保留密钥表单", async () => {
  let finish!: (response: Response) => void;
  const { host, platformClient } = await setup(async () => new Promise(resolve => { finish = resolve; }));
  await openSources(host); button(host, "修改密钥").click(); await nextTick();
  const field = host.querySelector<HTMLInputElement>('input[type="password"]')!;
  field.value = "synthetic-stale-key"; field.dispatchEvent(new Event("input"));
  host.querySelector('[role="dialog"] form')!.dispatchEvent(new Event("submit")); await flush();
  await platformClient.login("next", "synthetic-password"); await flush();
  finish(Response.json({ ...source, revision: 99 })); await flush();
  expect(host.querySelector('[role="dialog"]')).toBeNull();
  expect(host.innerHTML).not.toContain("synthetic-stale-key");
  expect(host.textContent).not.toContain("密钥已保存");
});

it("刷新尚未返回时不能打开旧版本的密钥编辑或删除", async () => {
  const { host } = await setup(); await openSources(host);
  let finish!: (response: Response) => void;
  vi.stubGlobal("fetch", () => new Promise<Response>(resolve => { finish = resolve; }));
  button(host, "刷新来源状态").click(); await nextTick();
  expect(button(host, "修改密钥").disabled).toBe(true);
  expect(button(host, "删除密钥").disabled).toBe(true);
  finish(Response.json({ sources: [source, publicSource], generated_at: "2026-10-11T01:00:00Z" })); await flush();
  expect(button(host, "修改密钥").disabled).toBe(false);
});

it("打开来源自动查询当前配置额度，区分总量、余额和部分用完", async () => {
  let calls = 0;
  const { host } = await setup(undefined, [], async init => {
    calls += 1;
    expect(init.method).toBe("POST");
    expect(new Headers(init.headers).get("X-CSRF-Token")).toBe("synthetic-csrf");
    expect(JSON.parse(String(init.body))).toEqual({ expected_revision: 3 });
    return Response.json({ ...source, can_check_usage: true, usage: {
      status: "partial_exhausted", checked_at: "2026-10-11T05:00:00Z", reset_date: "2026-11-01", stale: false,
      counters: [{ kind: "searches", used: "100", limit: "100", remaining: "0" }, { kind: "verifications", used: "20", limit: "100", remaining: "80" }],
    } });
  });
  await openSources(host);
  expect(calls).toBe(1);
  const row = host.querySelector('[data-source-id="hunter"]')!;
  expect(row.textContent).toContain("部分额度已用完");
  expect(row.textContent).toContain("总额度 100");
  expect(row.textContent).toContain("剩余 80");
  expect(row.textContent).toContain("额度重置：2026-11-01");
  expect(host.textContent).toContain("额度或密钥需要关注：Hunter");
  button(host, "检查额度").click(); await flush();
  expect(calls).toBe(2);
});

it("暂时限流不显示余额用完，查询未接入的来源不虚报零额度", async () => {
  const { host } = await setup(undefined, [{ ...source, definition: { ...source.definition, source_id: "apollo", name: "Apollo" } }], async () => Response.json({
    ...source, can_check_usage: true, usage: { status: "rate_limited", checked_at: "2026-10-11T05:00:00Z", counters: [] },
  }));
  await openSources(host);
  const hunter = host.querySelector('[data-source-id="hunter"]')!;
  expect(hunter.textContent).toContain("暂时限流");
  expect(hunter.textContent).not.toContain("额度已用完");
  const apollo = host.querySelector('[data-source-id="apollo"]')!;
  expect(apollo.textContent).toContain("额度查询待接入");
  expect(apollo.textContent).not.toContain("剩余 0");
});

it("额度检查失败只显示安全提示，不能把底层错误或余额零当结果", async () => {
  const { host } = await setup(undefined, [], async () => Response.json({ detail: "provider-secret-canary" }, { status: 503 }));
  await openSources(host);
  expect(host.textContent).toContain("本次检查未完成");
  expect(host.textContent).not.toContain("provider-secret-canary");
  expect(host.textContent).not.toContain("剩余 0");
  expect(button(host, "检查额度").disabled).toBe(false);
});

it("切换账号后到达的旧额度结果不能重新显示", async () => {
  let finish!: (response: Response) => void;
  const { host, platformClient } = await setup(undefined, [], async () => new Promise(resolve => { finish = resolve; }));
  await openSources(host);
  await platformClient.login("next", "synthetic-password"); await flush();
  finish(Response.json({ ...source, can_check_usage: true, usage: { status: "exhausted", counters: [] } })); await flush();
  expect(host.textContent).not.toContain("额度已用完");
});

async function writeNote(host: HTMLElement, text: string) {
  const field = host.querySelector<HTMLTextAreaElement>("textarea")!;
  field.value = text; field.dispatchEvent(new Event("input")); await nextTick();
}
it("公开来源可保存人工备注，独立版本和CSRF，文本不能执行HTML", async () => {
  let sent: unknown, calls = 0;
  const { host } = await setup(undefined, [], undefined, async (path, init) => {
    calls += 1; sent = JSON.parse(String(init.body));
    expect(path).toContain("/ungm/note"); expect(init.method).toBe("PUT");
    expect(new Headers(init.headers).get("X-CSRF-Token")).toBe("synthetic-csrf");
    return Response.json({ text: "<img src=x onerror=alert(1)>\n账号已注册", revision: 1, updated_at: "2026-10-11T06:00:00Z", updated_by: "xue" });
  });
  await openSources(host);
  button(host.querySelector<HTMLElement>('[data-source-id="ungm"]')!, "添加备注").click(); await nextTick();
  await writeNote(host, "<img src=x onerror=alert(1)>\n账号已注册");
  host.querySelector('[role="dialog"] form')!.dispatchEvent(new Event("submit")); await flush();
  expect(sent).toEqual({ expected_revision: 0, text: "<img src=x onerror=alert(1)>\n账号已注册" });
  expect(calls).toBe(1); expect(host.querySelector("img")).toBeNull();
  const row = host.querySelector<HTMLElement>('[data-source-id="ungm"]')!;
  expect(row.textContent).toContain("账号已注册"); expect(row.textContent).toContain("尚未测试");
  button(row, "编辑备注").click(); await nextTick();
  expect(host.querySelector<HTMLTextAreaElement>("textarea")?.value).toContain("账号已注册");
});
it("清空备注需保存，取消保留原备注，备注编辑不会改动密钥验证状态", async () => {
  let calls = 0;
  const { host } = await setup(undefined, [], undefined, async (_path, init) => {
    calls += 1; const body = JSON.parse(String(init.body));
    expect(body.expected_revision).toBe(calls - 1);
    return Response.json({ text: body.text, revision: calls, updated_at: "2026-10-11T06:00:00Z", updated_by: "xue" });
  });
  await openSources(host); button(host, "添加备注").click(); await nextTick();
  await writeNote(host, "已注册，后续统一测试");
  host.querySelector('[role="dialog"] form')!.dispatchEvent(new Event("submit")); await flush();
  button(host, "编辑备注").click(); await nextTick(); button(host, "清空内容").click(); await nextTick();
  button(host, "取消").click(); await nextTick();
  expect(calls).toBe(1); expect(host.textContent).toContain("已注册，后续统一测试");
  button(host, "编辑备注").click(); await nextTick(); button(host, "清空内容").click(); await nextTick();
  host.querySelector('[role="dialog"] form')!.dispatchEvent(new Event("submit")); await flush();
  expect(calls).toBe(2); expect(host.textContent).not.toContain("已注册，后续统一测试");
  expect(host.querySelector('[data-source-id="hunter"]')?.textContent).toContain("账户验证通过");
});
it("备注冲突保留草稿提示刷新，不呈现服务器原始错误", async () => {
  const { host } = await setup(undefined, [], undefined, async () => Response.json({ detail: "unsafe-error" }, { status: 409 }));
  await openSources(host); button(host, "添加备注").click(); await nextTick(); await writeNote(host, "我的草稿");
  host.querySelector('[role="dialog"] form')!.dispatchEvent(new Event("submit")); await flush();
  expect(host.querySelector<HTMLTextAreaElement>("textarea")?.value).toBe("我的草稿");
  expect(host.querySelector('[role="dialog"]')?.textContent).toContain("备注已被其他管理员更新");
  expect(host.textContent).not.toContain("unsafe-error");
});
it("切换身份清空备注草稿并忽略迟到保存结果", async () => {
  let finish!: (response: Response) => void;
  const { host, platformClient } = await setup(undefined, [], undefined, async () => new Promise(resolve => { finish = resolve; }));
  await openSources(host); button(host, "添加备注").click(); await nextTick(); await writeNote(host, "旧身份备注");
  host.querySelector('[role="dialog"] form')!.dispatchEvent(new Event("submit")); await flush();
  await platformClient.login("next", "synthetic-password"); await flush();
  finish(Response.json({ text: "旧身份备注", revision: 1, updated_at: "2026-10-11T06:00:00Z", updated_by: "xue" })); await flush();
  expect(host.querySelector("textarea")).toBeNull(); expect(host.textContent).not.toContain("旧身份备注");
  expect(host.textContent).not.toContain("备注已保存");
});
