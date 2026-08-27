import { createApp, nextTick } from "vue";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { components } from "../src/api/api";
import { createApiClient } from "../src/api/client";
import App from "../src/App.vue";
import router from "../src/router";

type Account = components["schemas"]["ResearchProspectAccountView"];
type AccountDetail = components["schemas"]["ResearchProspectAccountDetailView"];

const account: Account = {
  account_id: "acc_01K39P9M5D6K4A91YEQ80EJZ0X",
  country: "US",
  created_at: "2026-08-21T10:00:00Z",
  entity_type: "distributor",
  industry: "hardware",
  name: "Northwind Hardware",
  size_hint: "regional",
  source_signal_refs: ["sig_source_public_expansion"],
  research_signals: [],
  tenant_id: "tn_hidden_server_identity",
  website_domain: "northwind.example",
};

function contactPoint(
  suffix: string,
  verification: components["schemas"]["VerificationStatus"],
): components["schemas"]["ContactPointDetailView"] {
  return {
    assessment_ref: "lia-controlled-1",
    collected_at: "2026-08-21T10:01:00Z",
    contact_point: {
      account_id: account.account_id,
      contact_id: "con_01K39P9M5D6K4A91YEQ80EJZ0Y",
      contact_point_id: `cp_${suffix}`,
      created_at: "2026-08-21T10:01:00Z",
      kind: "email",
      tenant_id: account.tenant_id,
      value: `${suffix}@synthetic.invalid`,
      verification,
      verification_checked_at: verification === "unverified" ? null : "2026-08-21T10:02:00Z",
      verification_provider: verification === "unverified" ? null : "controlled-verifier",
      verified_at: verification === "verified" ? "2026-08-21T10:02:00Z" : null,
    },
    contact_type: "role_based",
    legal_basis: "legitimate_interest",
    legal_basis_source: "controlled-lia",
    source_url: "https://example.test/contact-policy",
    subject_type: "legal_entity",
  };
}

const detail: AccountDetail = {
  account,
  contacts: [{
    contact: {
      account_id: account.account_id,
      contact_id: "con_01K39P9M5D6K4A91YEQ80EJZ0Y",
      created_at: "2026-08-21T10:01:00Z",
      full_name: "Synthetic Procurement",
      role_title: "Procurement",
      tenant_id: account.tenant_id,
    },
    contact_points: [
      contactPoint("verified", "verified"),
      contactPoint("invalid", "invalid"),
      contactPoint("risky", "risky"),
      contactPoint("unverified", "unverified"),
    ],
  }],
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

describe("CustomerDiscovery", () => {
  it("shows signal, legal-basis and every verification state and submits no browser-owned identity", async () => {
    let discoveryBody: Record<string, unknown> | null = null;
    let researchReads = 0;
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      if (!(input instanceof Request)) throw new TypeError("Request required");
      const path = new URL(input.url).pathname;
      if (path === "/runs") { researchReads += 1; return response([]); }
      if (input.method === "GET" && path === "/prospects/accounts") return response([account]);
      if (input.method === "GET" && path === `/prospects/accounts/${account.account_id}`) return response(detail);
      if (input.method === "POST" && path === "/prospects/discoveries") {
        discoveryBody = await input.json() as Record<string, unknown>;
        return response({ run_id: "run_controlled", subject_ref: "hyp_controlled", workflow_type: "account_discovery" });
      }
      if (path === "/notifications") return response([]);
      return response([]);
    });
    const root = document.createElement("div");
    document.body.replaceChildren(root);
    const app = createApp(App);
    app.provide("tradeos-api-client", createApiClient({ baseUrl: "https://tradeos.test", fetch }));
    app.use(router);
    app.mount(root);
    await router.replace("/prospects/accounts");
    await eventually(() => expect(root.textContent).toContain(account.name));
    expect(researchReads).toBe(1);
    [...root.querySelectorAll("button")].find((button) => button.textContent?.trim() === "刷新")!.click();
    await eventually(() => expect(researchReads).toBe(2));

    const accountButton = root.querySelector<HTMLElement>(`.account-list li[role="button"]`);
    accountButton?.click();
    await eventually(() => {
      expect(root.textContent).toContain(account.account_id);
      expect(root.textContent).toContain(account.source_signal_refs[0]);
      expect(root.textContent).toContain("legitimate_interest");
      expect(root.textContent).toContain("已验证，可入组");
      expect(root.textContent).toContain("无效，禁止入组");
      expect(root.textContent).toContain("风险地址，禁止入组");
      expect(root.textContent).toContain("未验证，禁止入组");
    });

    const inputs = root.querySelectorAll<HTMLInputElement>(".discovery-command input");
    inputs[0]!.value = "hyp_controlled";
    inputs[0]!.dispatchEvent(new Event("input"));
    inputs[1]!.value = "cmp_controlled";
    inputs[1]!.dispatchEvent(new Event("input"));
    inputs[2]!.value = "procurement, sourcing";
    inputs[2]!.dispatchEvent(new Event("input"));
    inputs[3]!.value = "lia-controlled-1";
    inputs[3]!.dispatchEvent(new Event("input"));
    root.querySelector<HTMLFormElement>(".discovery-command form")?.dispatchEvent(new Event("submit"));

    await eventually(() => expect(discoveryBody).not.toBeNull());
    expect(discoveryBody).toEqual({
      assessment_ref: "lia-controlled-1",
      campaign_id: "cmp_controlled",
      hypothesis_id: "hyp_controlled",
      role_hints: ["procurement", "sourcing"],
    });
    expect(discoveryBody).not.toHaveProperty("tenant_id");
    expect(discoveryBody).not.toHaveProperty("employee_id");
    expect(root.textContent).toContain("账户发现任务已创建：run_controlled");
    app.unmount();
  });
});
