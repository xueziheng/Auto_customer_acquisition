import { createApp, nextTick, type App as VueApp } from "vue";
import { createMemoryHistory, createRouter } from "vue-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { components } from "../src/api/api";
import { createApiClient } from "../src/api/client";
import App from "../src/App.vue";
import router from "../src/router";
import CampaignCenter from "../src/views/campaigns/CampaignCenter.vue";

type Campaign = components["schemas"]["CampaignView"];
type Enrollment = components["schemas"]["EnrollmentView"];

const campaign: Campaign = {
  approval_id: "apr_exact_campaign_v3",
  approved_at: "2026-08-21T10:00:00Z",
  approved_by: "emp_boss",
  boundary: {
    allowed_categories: ["hinges"],
    daily_new_contact_limit: 20,
    daily_total_message_limit: 50,
    handoff_triggers: ["quote_requested"],
    markets: ["US"],
    sender_identity_ids: ["sid_controlled"],
    steps: [{ intent: "discovery", step_number: 1, wait_days: 0 }],
    stop_on_reply: true,
    target_entity_types: ["distributor"],
  },
  campaign_id: "cmp_01K39P9M5D6K4A91YEQ80EJZ0X",
  created_at: "2026-08-21T09:00:00Z",
  created_by: "emp_boss",
  name: "Controlled Hinges",
  paused_reason: "manual quality review",
  state: "paused",
  tenant_id: "tn_server_bound",
  today_messages_reserved: 1,
  today_new_contacts_reserved: 1,
  version: 3,
};

const enrollment: Enrollment = {
  account_id: "acc_controlled",
  campaign_id: campaign.campaign_id,
  campaign_version: 3,
  contact_point_id: "cp_controlled",
  current_step: 1,
  enrolled_at: "2026-08-21T10:05:00Z",
  enrollment_id: "enr_controlled",
  execution_mode: "automatic",
  next_send_at: null,
  sending_identity_id: "sid_controlled",
  state: "replied",
  stop_reason: "reply",
  stopped_at: "2026-08-21T10:06:00Z",
  tenant_id: "tn_server_bound",
  source_hypothesis_id: "hyp_01K39P9M5D6K4A91YEQ80EJZ0Z",
};

function response(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

async function eventually(assertion: () => void): Promise<void> {
  let error: unknown;
  for (let attempt = 0; attempt < 80; attempt += 1) {
    await nextTick();
    await new Promise((resolve) => setTimeout(resolve, 0));
    try {
      assertion();
      return;
    } catch (caught) {
      error = caught;
    }
  }
  throw error;
}

const mounted: VueApp[] = [];
afterEach(() => {
  mounted.splice(0).forEach(app => app.unmount());
  router.replace("/");
});

async function mountPurpose(path = "/campaigns", options: {
  agentRole?: "boss" | "sales"; state?: Campaign["state"]; senderUsable?: boolean; otherUsableSender?: boolean; allCapabilities?: boolean;
  post?: (request: Request) => Promise<Response>;
} = {}) {
  let generation = 0;
  const listeners = new Set<() => void>();
  const writes: Request[] = [];
  let current = { ...campaign, state: options.state ?? "draft", paused_reason: null };
  const sender: components["schemas"]["IdentityView"] = {
    identity_id: "sid_controlled", address: "sender@example.test", domain: "example.test", role: "cold_outreach",
    state: options.senderUsable === false ? "auth_pending" : "active", created_at: campaign.created_at,
    usable_for_cold_outreach: options.senderUsable !== false, can_send_today: options.senderUsable !== false,
    remaining_today: options.senderUsable === false ? 0 : 1, warmup_complete: options.senderUsable !== false,
  };
  const client = createApiClient({ baseUrl: "https://tradeos.test", fetch: async input => {
    const request = input as Request, url = new URL(request.url);
    if (request.method === "POST") {
      writes.push(request.clone());
      if (options.post) return options.post(request);
      if (url.pathname === "/crm/campaigns") {
        const { name, ...boundary } = await request.json();
        current = { ...current, name, boundary };
      } else if (/\/(start|activate)$/.test(url.pathname)) current = { ...current, state: "active" };
      return response(current);
    }
    if (url.pathname === "/prospects/customers/access") return response({role: options.agentRole ?? "sales", can_assign: options.agentRole === "boss", can_search: options.agentRole === "boss"});
    if (url.pathname === "/team/employees") return response([{tenant_id: "tn_server_bound", employee_id: "emp_owner", name: "业务负责人", role: "sales", is_active: true}]);
    if (url.pathname === "/knowledge/documents") return response({items: [{tenant_id: "tn_server_bound", document_id: "doc_scope", current_revision_id: "rev_scope", status: "confirmed", source: {filename: "确认产品.txt"}}]});
    if (url.pathname === "/crm/campaigns") return response([current]);
    if (url.pathname === "/crm/sending-identities") return response([
      sender, ...(options.otherUsableSender ? [{ ...sender, identity_id: "sid_other", usable_for_cold_outreach: true }] : []),
    ]);
    if (url.pathname === "/health/capabilities") return response(["research", "contacts", "campaign", "reply"].map(name => ({
      name, status: options.allCapabilities || name === "campaign" ? "enabled" : "disabled", reason: "composed",
    })));
    return response([]);
  } }, {
    current: () => ({ mode: "fixed-dev", tenantId: "tn_server_bound", employeeId: generation ? "emp_other" : "emp_boss" }),
    generation: () => generation, subscribe: listener => { listeners.add(listener); return () => listeners.delete(listener); },
  });
  const localRouter = createRouter({ history: createMemoryHistory(), routes: [{ path: "/campaigns", component: CampaignCenter }] });
  await localRouter.replace(path);
  const root = document.createElement("div"); document.body.append(root);
  const app = createApp(CampaignCenter); mounted.push(app); app.provide("tradeos-api-client", client); app.use(localRouter).mount(root);
  await eventually(() => expect(root.querySelector(".campaign-detail h2")?.textContent).toBe(campaign.name));
  await eventually(() => expect(root.textContent).not.toContain("正在读取发件身份"));
  await nextTick();
  return { root, writes, router: localRouter, switchIdentity: () => { generation++; listeners.forEach(listener => listener()); } };
}
const button = (root: HTMLElement, text: string) => [...root.querySelectorAll("button")].find(item => item.textContent?.trim() === text)!;
function fill(root: HTMLElement, label: string, value: string) {
  const input = root.querySelector<HTMLInputElement>(`[aria-label="${label}"]`)!;
  input.value = value; input.dispatchEvent(new Event("input", { bubbles: true }));
}

it("manual配置视图只要求真实手动发送能力，保存精确单封边界并明确尚未发信", async () => {
  const { root, writes } = await mountPurpose("/campaigns?execution_mode=manual");
  expect(root.querySelector("h1")?.textContent).toBe("手动联系范围");
  button(root, "设置手动联系范围").click(); await nextTick();
  fill(root, "目标市场", "NG"); fill(root, "产品类别", "electric_tricycle"); fill(root, "客户类型", "distributor");
  root.querySelector<HTMLInputElement>('.sender-picker input[value="sid_controlled"]')!.click(); await nextTick();
  const submit = root.querySelector<HTMLButtonElement>('.boundary-editor button[type="submit"]')!;
  expect(submit.disabled).toBe(false); submit.click();
  await eventually(() => expect(writes).toHaveLength(2));
  expect(writes.map(request => new URL(request.url).pathname)).toEqual(["/crm/campaigns", `/crm/campaigns/${campaign.campaign_id}/start`]);
  const body = await writes[0].json();
  expect(body).toMatchObject({
    markets: ["NG"], target_entity_types: ["distributor"], allowed_categories: ["electric_tricycle"], sender_identity_ids: ["sid_controlled"],
    daily_new_contact_limit: 1, daily_total_message_limit: 1, steps: [{ step_number: 1, intent: "discovery", wait_days: 0 }], stop_on_reply: true,
  });
  expect(body).not.toHaveProperty("execution_mode");
  await eventually(() => expect(root.querySelector(".automation-running")?.textContent).toContain("手动入组"));
  expect(root.querySelector(".automation-running")?.textContent).toContain("尚未发信");
  expect(root.textContent).toContain("现有入组模式不会改变");
});

it.each(["draft", "paused"] as const)("自动模式在四能力未齐备时继续阻止%s，手动视图显式批准范围", async state => {
  const automatic = await mountPurpose("/campaigns", { state });
  expect(button(automatic.root, state === "draft" ? "启动自动发邮件" : "启动自动安全发送").disabled).toBe(true);
  button(automatic.root, "设置自动任务").click(); await nextTick();
  expect(automatic.root.querySelector<HTMLButtonElement>('.boundary-editor button[type="submit"]')?.disabled).toBe(true);
  expect(automatic.writes).toHaveLength(0);
  const manual = await mountPurpose("/campaigns?execution_mode=manual", { state });
  const submit = button(manual.root, state === "draft" ? "批准手动联系范围" : "恢复已批准范围");
  expect(submit.disabled).toBe(false); submit.click();
  await eventually(() => expect(manual.writes).toHaveLength(1));
  expect(new URL(manual.writes[0].url).pathname).toBe(`/crm/campaigns/${campaign.campaign_id}/${state === "draft" ? "start" : "activate"}`);
  expect(manual.writes[0].body).toBeNull();
});

it("manual query不能把未认证的当前边界发件身份当可用，其他邮箱不代替它", async () => {
  const { root, writes } = await mountPurpose("/campaigns?execution_mode=manual", { senderUsable: false, otherUsableSender: true });
  expect(button(root, "批准手动联系范围").disabled).toBe(true);
  button(root, "设置手动联系范围").click(); await nextTick();
  expect(root.querySelector<HTMLInputElement>('.sender-picker input[value="sid_controlled"]')?.disabled).toBe(true);
  root.querySelector("form")!.dispatchEvent(new Event("submit", { cancelable: true })); await nextTick();
  expect(writes).toHaveLength(0);
});

it("切换用途不改旧边界，编辑时锁定用途；身份变化清空手动表单与模式", async () => {
  const { root, writes, router: localRouter, switchIdentity } = await mountPurpose("/campaigns?execution_mode=manual");
  button(root, "修订边界").click(); await nextTick();
  const purpose = root.querySelector<HTMLSelectElement>('[aria-label="配置用途"]')!;
  expect(purpose.disabled).toBe(true);
  expect(root.querySelector<HTMLInputElement>('[aria-label="目标市场"]')?.value).toBe("US");
  expect(root.querySelector<HTMLInputElement>('[aria-label="客户类型"]')?.value).toBe("distributor");
  expect(writes).toHaveLength(0);
  switchIdentity(); await nextTick();
  expect(root.querySelector(".boundary-editor")).toBeNull();
  expect(root.querySelector<HTMLSelectElement>('[aria-label="配置用途"]')?.value).toBe("automatic");
  expect(root.textContent).not.toContain("sender@example.test");
  expect(root.textContent).not.toContain(campaign.name);
  await eventually(() => expect(localRouter.currentRoute.value.query.execution_mode).toBeUndefined());
});

it("手动视图修订旧边界保留原额度，不隐式转换已有任务", async () => {
  const { root, writes } = await mountPurpose("/campaigns?execution_mode=manual");
  button(root, "修订边界").click(); await nextTick();
  button(root, "保存并批准联系范围").click();
  await eventually(() => expect(writes).toHaveLength(2));
  const body = await writes[0].json();
  expect(new URL(writes[0].url).pathname).toBe(`/crm/campaigns/${campaign.campaign_id}/revise`);
  expect(body).toEqual({ name: campaign.name, ...campaign.boundary });
  expect(body).not.toHaveProperty("execution_mode");
});

it("用途可显式选择，未知批准结果禁止切换模式或再次提交", async () => {
  const { root, writes } = await mountPurpose("/campaigns", { post: async () => new Response("{}", { status: 503 }) });
  const purpose = root.querySelector<HTMLSelectElement>('[aria-label="配置用途"]')!;
  purpose.value = "manual"; purpose.dispatchEvent(new Event("change")); await nextTick();
  expect(button(root, "批准手动联系范围").disabled).toBe(false);
  button(root, "批准手动联系范围").click();
  await eventually(() => expect(root.textContent).toContain("结果待核对"));
  expect(purpose.disabled).toBe(true);
  purpose.value = "automatic"; purpose.dispatchEvent(new Event("change")); await nextTick();
  expect(root.querySelector("h1")?.textContent).toBe("手动联系范围");
  expect(button(root, "批准手动联系范围").disabled).toBe(true);
  expect(writes).toHaveLength(1);
});

describe("CampaignCenter", () => {
  it("keeps enrollment bound to the running version and explains the simple automation flow", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      if (!(input instanceof Request)) throw new TypeError("Request required");
      const path = new URL(input.url).pathname;
      if (path === "/crm/campaigns") return response([campaign]);
      if (path === `/crm/campaigns/${campaign.campaign_id}/enrollments`) return response([enrollment]);
      if (path === "/crm/sending-identities" || path === "/notifications") return response([]);
      return response([]);
    });
    const root = document.createElement("div");
    document.body.replaceChildren(root);
    const app = createApp(App);
    app.provide("tradeos-api-client", createApiClient({ baseUrl: "https://tradeos.test", fetch }));
    app.use(router);
    app.mount(root);
    await router.replace("/campaigns");

    await eventually(() => expect(root.textContent).toContain(campaign.name));
    const workflowLinks = [...root.querySelectorAll<HTMLElement>('nav[aria-label="主导航"] a')];
    expect(workflowLinks.map((item) => item.textContent?.replace(/\s+/g, "").replace(/^\d/, ""))).toEqual([
      "我的客户", "消息", "产品资料",
    ]);
    expect(root.textContent).not.toContain("指挥中心");
    expect(root.textContent).not.toContain("更多");
    expect(root.querySelector('a[href="/notifications"]')).not.toBeNull();
    expect(root.textContent).toContain("已暂停");
    expect(root.textContent).not.toContain("审批 apr_exact_campaign_v3");
    expect(root.textContent).toContain("首轮仅发第一封");
    expect(root.textContent).toContain("真实搜索与邮箱验证就绪后才可发送");
    expect(root.textContent).toContain("当前不能启动外发活动");
    expect(root.textContent).toContain("启动自动安全发送");
    expect(root.textContent).toContain("manual quality review");
    expect(root.textContent).toContain("acc_controlled");
    expect(root.textContent).not.toContain("enr_controlled");
    expect(root.textContent).not.toContain("hyp_01K39P9M5D6K4A91YEQ80EJZ0Z");
    expect(root.textContent).toContain("已回复");
    app.unmount();
  });

  it("keeps safe defaults out of the simplified setup form", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      if (!(input instanceof Request)) throw new TypeError("Request required");
      const path = new URL(input.url).pathname;
      if (path === "/crm/campaigns" || path === "/crm/sending-identities" || path === "/notifications") {
        return response([]);
      }
      return response([]);
    });
    const root = document.createElement("div");
    document.body.replaceChildren(root);
    const app = createApp(App);
    app.provide("tradeos-api-client", createApiClient({ baseUrl: "https://tradeos.test", fetch }));
    app.use(router);
    app.mount(root);
    await router.replace("/campaigns");
    await eventually(() => expect(root.textContent).toContain("设置自动任务"));

    const create = [...root.querySelectorAll("button")].find((item) => item.textContent?.includes("设置自动任务"));
    create?.click();
    await nextTick();

    expect(root.querySelector(".advanced-settings")).toBeNull();
    expect(root.querySelectorAll('input[type="number"]')).toHaveLength(0);
    expect(root.textContent).toContain("目标市场");
    expect(root.textContent).toContain("产品");
    expect(root.textContent).toContain("发件邮箱");
    expect((root.querySelector('input[placeholder="填写本次开发的目标市场"]') as HTMLInputElement).value).toBe("");
    expect((root.querySelector('input[placeholder="填写本企业要开发的产品类别"]') as HTMLInputElement).value).toBe("");
    expect(root.textContent).not.toContain("每日新联系人上限");
    expect(root.textContent).not.toContain("增加步骤");
    expect(root.textContent).not.toContain("手工发送（故障恢复）");
    const submit = root.querySelector<HTMLButtonElement>('.boundary-editor button[type="submit"]');
    expect(submit?.disabled).toBe(true);
    app.unmount();
  });
});

it("发送关闭时老板可显式批准准备范围，员工隐藏入口", async () => {
  const {root, writes, switchIdentity} = await mountPurpose("/campaigns", {agentRole: "boss"});
  button(root, "设置自动任务").click(); await nextTick();
  await eventually(() => expect(root.querySelector('[aria-label="批准自动准备并入组"]')).not.toBeNull());
  root.querySelector<HTMLInputElement>('[aria-label="批准自动准备并入组"]')!.click(); await nextTick();
  fill(root, "目标市场", "US"); fill(root, "产品类别", "hinges"); fill(root, "处理依据引用", "lia:actual-test");
  for (const [label, value] of [["自动准备负责人", "emp_owner"], ["自动准备确认资料", "doc_scope"]]) {
    const select = root.querySelector<HTMLSelectElement>(`[aria-label="${label}"]`)!;
    select.value = value; select.dispatchEvent(new Event("change"));
  }
  root.querySelector<HTMLInputElement>('.sender-picker input[value="sid_controlled"]')!.click(); await nextTick();
  root.querySelector<HTMLButtonElement>('.boundary-editor button[type="submit"]')!.click();
  await eventually(() => expect(writes).toHaveLength(2));
  expect((await writes[0]!.json()).customer_agent_policy).toEqual({prepare_and_enroll: true, owner_employee_id: "emp_owner", assessment_ref: "lia:actual-test", basis: "legitimate_interest", subject_type: "legal_entity", contact_type: "role_based", confirmed_refs: [{document_id: "doc_scope", revision_id: "rev_scope"}]});
  switchIdentity(); await nextTick(); expect(root.textContent).not.toContain("lia:actual-test");
  const employee = await mountPurpose("/campaigns", {agentRole: "sales"});
  button(employee.root, "设置自动任务").click(); await nextTick();
  expect(employee.root.querySelector('[aria-label="批准自动准备并入组"]')).toBeNull();
});
