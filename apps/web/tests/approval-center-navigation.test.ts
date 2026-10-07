import { afterEach, expect, it } from "vitest";
import { createApp, nextTick, type App as VueApp } from "vue";
import { createMemoryHistory, createRouter } from "vue-router";
import type { components } from "../src/api/api";
import { clearAuthenticatedIdentity, configureAuthenticatedIdentity, createApiClient } from "../src/api/client";
import ApprovalCenter from "../src/views/approvals/ApprovalCenter.vue";

type Approval = components["schemas"]["ApprovalView"];
const first: Approval = {
  approval_id: "apr_first", approval_type: "quote", affected_entities: [],
  can_current_user_decide: true, created_at: "2026-10-07T00:00:00Z",
  expires_at: "2026-10-08T00:00:00Z", if_approved: "批准不发送", if_rejected: "退回修正",
  proposed_by: "employee_proposer", owner_name: "业务负责人", proposed_change_display: {},
  reason: "合成审批资料", reversible: false, state: "pending", title: "旧审批资料", type_label: "正式报价",
};
const second: Approval = { ...first, approval_id: "apr_second", title: "当前精确审批", can_current_user_decide: false };
const apps: VueApp[] = [];

afterEach(() => {
  apps.splice(0).forEach((app) => app.unmount());
  clearAuthenticatedIdentity();
  document.body.replaceChildren();
});

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), { headers: { "content-type": "application/json" } });
}

function deferred() {
  let resolve!: (response: Response) => void;
  const promise = new Promise<Response>((done) => { resolve = done; });
  return { promise, resolve };
}

async function eventually(assertion: () => void): Promise<void> {
  let lastError: unknown;
  for (let attempt = 0; attempt < 50; attempt += 1) {
    await nextTick();
    await new Promise((resolve) => setTimeout(resolve, 0));
    try { assertion(); return; } catch (error) { lastError = error; }
  }
  throw lastError;
}

async function mount(fetch: typeof globalThis.fetch, path = "/approvals") {
  configureAuthenticatedIdentity("tenant_a", "employee_a");
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/approvals", component: ApprovalCenter }] });
  await router.replace(path);
  const host = document.createElement("div");
  document.body.append(host);
  const app = createApp(ApprovalCenter);
  app.provide("tradeos-api-client", createApiClient({ baseUrl: "https://tradeos.test", fetch }));
  app.use(router);
  app.mount(host);
  apps.push(app);
  await nextTick();
  return { host, router };
}

it("列表未完成时切换审批深链会完成新列表，迟到旧列表不能覆盖或解除新加载状态", async () => {
  const oldList = deferred();
  const newList = deferred();
  const requests: Request[] = [];
  let lists = 0;
  const { host, router } = await mount(async (input) => {
    const request = input as Request;
    requests.push(request);
    if (new URL(request.url).pathname === "/approvals/pending") {
      lists += 1;
      return lists === 1 ? oldList.promise : newList.promise;
    }
    return json(second);
  });
  await eventually(() => expect(lists).toBe(1));
  await router.replace("/approvals?approval_id=apr_second");
  expect(requests[0]!.signal.aborted).toBe(true);
  oldList.resolve(json([first]));
  await nextTick();
  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(host.textContent).toContain("正在按过期时间排序");
  expect(host.querySelector<HTMLButtonElement>(".approval-head button")!.disabled).toBe(true);
  expect(host.textContent).not.toContain(first.title);
  newList.resolve(json([second]));
  await eventually(() => {
    expect(host.querySelector("main.approval-packet h2")?.textContent).toBe(second.title);
    expect(host.querySelector<HTMLButtonElement>(".approval-head button")!.disabled).toBe(false);
  });
  expect(host.textContent).not.toContain("正在按过期时间排序");
  expect(host.querySelector(".approval-list button")?.textContent).toContain(second.title);
  expect(host.textContent).toContain("当前身份不能决定此审批");
  expect(host.querySelector(".decision-panel .btn-primary")).toBeNull();
  expect(requests.map((request) => [request.method, new URL(request.url).pathname])).toEqual([
    ["GET", "/approvals/pending"], ["GET", "/approvals/pending"], ["GET", "/approvals/apr_second"],
  ]);
});

it("切换审批深链后不显示旧详情，也不接受旧身份迟到的新列表", async () => {
  const oldDetail = deferred();
  const newList = deferred();
  const requests: Request[] = [];
  let lists = 0;
  const { host, router } = await mount(async (input) => {
    const request = input as Request;
    requests.push(request);
    if (new URL(request.url).pathname === "/approvals/pending") {
      lists += 1;
      return lists === 1 ? json([first]) : newList.promise;
    }
    return oldDetail.promise;
  }, "/approvals?approval_id=apr_first");
  await eventually(() => expect(requests).toHaveLength(2));
  await router.replace("/approvals?approval_id=apr_second");
  oldDetail.resolve(json(first));
  await nextTick();
  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(host.querySelector("main.approval-packet")).toBeNull();
  expect(host.textContent).not.toContain(first.title);
  configureAuthenticatedIdentity("tenant_b", "employee_b");
  configureAuthenticatedIdentity("tenant_a", "employee_a");
  newList.resolve(json([second]));
  await nextTick();
  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(host.textContent).not.toContain(second.title);
  expect(host.textContent).not.toContain("正在按过期时间排序");
  expect(host.querySelector("main.approval-packet")).toBeNull();
  expect(requests.map((request) => [request.method, new URL(request.url).pathname])).toEqual([
    ["GET", "/approvals/pending"], ["GET", "/approvals/apr_first"], ["GET", "/approvals/pending"],
  ]);
});
