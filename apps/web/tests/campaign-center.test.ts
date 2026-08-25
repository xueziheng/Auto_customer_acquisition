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

afterEach(() => {
  router.replace("/");
});

describe("CampaignCenter", () => {
  it("binds approval and enrollment visibly to the exact immutable version and explains pause semantics", async () => {
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
    expect(root.textContent).toContain("不可变版本 v3");
    expect(root.textContent).toContain("审批 apr_exact_campaign_v3");
    expect(root.textContent).toContain("暂停只阻止新发送；入站回复仍继续处理");
    expect(root.textContent).toContain("manual quality review");
    expect(root.textContent).toContain("enr_controlled");
    expect(root.textContent).toContain("Campaign v3");
    expect(root.textContent).toContain("replied");
    app.unmount();
  });
});
