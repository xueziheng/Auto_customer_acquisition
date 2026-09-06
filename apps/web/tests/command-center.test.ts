import { createApp, nextTick, type App as VueApp } from "vue";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "../src/App.vue";
import type { components } from "../src/api/api";
import { createApiClient } from "../src/api/client";
import router from "../src/router";

type PolicyView = components["schemas"]["SourcingAdmissionPolicyView"];
type ProposalView = components["schemas"]["ProposalView"];

const proposalId = "dpr_01K39P9M5D6K4A91YEQ80EJZ0X";
const directiveId = "dir_01K39P9M5D6K4A91YEQ80EJZ0X";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

async function eventually(assertion: () => void): Promise<void> {
  let latest: unknown;
  for (let attempt = 0; attempt < 80; attempt += 1) {
    await nextTick();
    await new Promise((resolve) => setTimeout(resolve, 0));
    try {
      assertion();
      return;
    } catch (error) {
      latest = error;
    }
  }
  throw latest;
}

const mountedApps: VueApp[] = [];
afterEach(() => {
  mountedApps.splice(0).forEach((app) => app.unmount());
  void router.replace("/");
});

async function mount(fetch: typeof globalThis.fetch): Promise<HTMLElement> {
  const root = document.createElement("div");
  document.body.replaceChildren(root);
  const app = createApp(App);
  app.provide("tradeos-api-client", createApiClient({ baseUrl: "https://tradeos.test", fetch }));
  app.use(router);
  await router.replace("/commands");
  app.mount(root);
  mountedApps.push(app);
  return root;
}

function proposalFixture(): ProposalView {
  return {
    automatic_sourcing_admission_enabled: true,
    created_at: "2026-09-02T09:00:00Z",
    decided_at: null,
    decided_by_id: null,
    decided_by_name: null,
    expected_behavior_changes: ["自动寻源准入：启用", "每轮最多启动 3 个寻源案例"],
    interpretation_summary: "按需求簇规模排序；自动寻源准入将启用；每轮上限为 3 个案例",
    parsed_fields: {},
    proposal_id: proposalId,
    raw_text: "按需求簇排序，每轮最多启动 3 个寻源案例",
    sourcing_admission_batch_limit: 3,
    sourcing_admission_mode: "cluster_ranked",
    state: "pending_confirmation",
  };
}

function policyFetch(policy: PolicyView): {
  fetch: typeof globalThis.fetch;
  requests: Request[];
} {
  const requests: Request[] = [];
  const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
    const request = input as Request;
    requests.push(request);
    const path = new URL(request.url).pathname;
    if (path === "/notifications") return jsonResponse([]);
    if (request.method === "GET" && path === "/sourcing-admissions") {
      return jsonResponse({ items: [], policy });
    }
    if (request.method === "POST" && path === "/commands/sourcing-admission-proposals") {
      expect(await request.json()).toEqual({
        automatic_admission_enabled: true,
        batch_limit: 3,
        message: "按需求簇排序，每轮最多启动 3 个寻源案例",
        mode: "cluster_ranked",
      });
      return jsonResponse(proposalFixture());
    }
    if (request.method === "POST" && path === `/commands/sourcing-admission-proposals/${proposalId}/confirm`) {
      expect(request.headers.get("Idempotency-Key")).toBeTruthy();
      return jsonResponse({
        automatic_admission_enabled: true,
        batch_limit: 3,
        directive_id: directiveId,
        directive_version: 7,
        mode: "cluster_ranked",
        proposal_id: proposalId,
      });
    }
    return jsonResponse({ code: "unexpected" }, 500);
  });
  return { fetch, requests };
}

describe("Command Center sourcing admission policy", () => {
  it.each([
    ["policy_not_configured", "未配置"],
    ["automatic_admission_disabled", "已关闭"],
    ["policy_status_unknown", "状态未知"],
  ] as const)("keeps %s visibly distinct", async (status, expected) => {
    const { fetch } = policyFetch({
      automatic_admission_enabled: status === "automatic_admission_disabled" ? false : null,
      batch_limit: status === "automatic_admission_disabled" ? 3 : null,
      directive_id: status === "automatic_admission_disabled" ? directiveId : null,
      directive_version: status === "automatic_admission_disabled" ? 6 : null,
      status,
    });
    const root = await mount(fetch);

    await eventually(() => expect(root.textContent).toContain(`当前策略：${expected}`));
  });

  it("submits strict fields, previews effects, and confirms a directive without claiming a run", async () => {
    const { fetch, requests } = policyFetch({
      automatic_admission_enabled: null,
      batch_limit: null,
      directive_id: null,
      directive_version: null,
      status: "policy_not_configured",
    });
    const root = await mount(fetch);
    await eventually(() => expect(root.querySelector<HTMLFormElement>('form[aria-label="寻源准入策略提案"]')).not.toBeNull());

    const form = root.querySelector<HTMLFormElement>('form[aria-label="寻源准入策略提案"]')!;
    const message = form.querySelector<HTMLTextAreaElement>('[name="admission_message"]')!;
    const enabled = form.querySelector<HTMLInputElement>('[name="automatic_admission_enabled"]')!;
    const limit = form.querySelector<HTMLInputElement>('[name="batch_limit"]')!;
    message.value = "按需求簇排序，每轮最多启动 3 个寻源案例";
    message.dispatchEvent(new Event("input", { bubbles: true }));
    enabled.checked = true;
    enabled.dispatchEvent(new Event("change", { bubbles: true }));
    limit.value = "3";
    limit.dispatchEvent(new Event("input", { bubbles: true }));
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));

    await eventually(() => {
      expect(root.textContent).toContain("自动寻源准入：启用");
      expect(root.textContent).toContain("每轮最多启动 3 个寻源案例");
      expect(root.textContent).toContain("cluster_ranked");
      expect(root.textContent).toContain("确认提案");
    });
    expect(root.textContent).toContain("提案不会启动寻源流程");
    root.querySelector<HTMLButtonElement>(".admission-policy button[type='button']")!.click();

    await eventually(() => expect(root.textContent).toContain("生效 Directive v7"));
    expect(root.textContent).toContain("确认只更新准入策略，不代表寻源已启动");
    expect(requests.map((request) => new URL(request.url).pathname)).not.toContain("/workflow-runs");
  });
});

it("身份切换后旧提案成功不得恢复旧身份的确认按钮",async()=>{
 const {configureAuthenticatedIdentity,clearAuthenticatedIdentity}=await import("../src/api/client");
 configureAuthenticatedIdentity("tenant", "boss-old");
 let resolve!:(value:Response)=>void;const pending=new Promise<Response>(r=>{resolve=r;});
 const base=policyFetch({status:"policy_not_configured"});
 const root=await mount(async input=>(input as Request).method==="POST"?pending:base.fetch(input));
 await eventually(()=>expect(root.querySelector('button[data-testid="create-admission-proposal"]')??[...root.querySelectorAll("button")].find(b=>b.textContent?.includes("生成准入提案"))).toBeTruthy());
 const button=[...root.querySelectorAll("button")].find(b=>b.textContent?.includes("生成准入提案"))!;button.click();await nextTick();
 configureAuthenticatedIdentity("tenant","boss-new");resolve(jsonResponse(proposalFixture()));
 for(let i=0;i<15;i++){await nextTick();await new Promise(r=>setTimeout(r,0));}
 expect(root.querySelector(".admission-proposal")).toBeNull();clearAuthenticatedIdentity();
});
