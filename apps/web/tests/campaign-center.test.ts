import { createApp, nextTick } from "vue";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { components } from "../src/api/api";
import { createApiClient } from "../src/api/client";
import App from "../src/App.vue";
import router from "../src/router";

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
  next_send_at: null,
  sending_identity_id: "sid_controlled",
  state: "replied",
  stop_reason: "reply",
  stopped_at: "2026-08-21T10:06:00Z",
  tenant_id: "tn_server_bound",
  source_hypothesis_id: "hyp_01K39P9M5D6K4A91YEQ80EJZ0Z",
} as Enrollment;

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

afterEach(() => {
  router.replace("/");
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
    const workflowLinks = [...root.querySelectorAll<HTMLElement>('nav[aria-label="自动获客流程"] a')];
    expect(workflowLinks.map((item) => item.textContent?.replace(/\s+/g, "").replace(/^\d/, ""))).toEqual([
      "自动找客户",
      "自动发邮件",
      "人工接管",
    ]);
    expect(root.textContent).not.toContain("指挥中心");
    expect(root.textContent).not.toContain("更多");
    expect(root.textContent).not.toContain("通知");
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
    expect((root.querySelector('input[placeholder="例如：Kenya"]') as HTMLInputElement).value).toBe("Kenya");
    expect((root.querySelector('input[placeholder="例如：solar electric three-wheeler"]') as HTMLInputElement).value).toBe("solar electric three-wheeler");
    expect(root.textContent).not.toContain("每日新联系人上限");
    expect(root.textContent).not.toContain("增加步骤");
    expect(root.textContent).not.toContain("手工发送（故障恢复）");
    const submit = root.querySelector<HTMLButtonElement>('.boundary-editor button[type="submit"]');
    expect(submit?.disabled).toBe(true);
    app.unmount();
  });
});
