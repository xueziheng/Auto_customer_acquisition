import { createApp, nextTick, type App as VueApp } from "vue";
import { describe, expect, it, vi } from "vitest";

import { createApiClient } from "../src/api/client";
import App from "../src/App.vue";
import router from "../src/router";

const versionId = "pbv_01K00000000000000000000000";
const activeVersion = {
  approval_requirements: ["catalog_reference_price"],
  base_content_hash: null,
  base_version_id: null,
  change_set_ref: `playbook:${versionId}:${"a".repeat(64)}`,
  company_type: "trading_company",
  content_hash: "a".repeat(64),
  content_provenance: {
    confirmed_at: null,
    confirmed_by: null,
    extracted_at: "2026-08-24T08:00:00Z",
    extracted_by: "human:emp_boss",
    source_id: versionId,
    source_type: "employee_input",
  },
  excluded_categories: ["adult"],
  excluded_countries: ["north korea"],
  minimum_deal_amount: "10000",
  minimum_deal_currency: "USD",
  monthly_budget_credits: 500,
  playbook_version_id: versionId,
  proposed_at: "2026-08-24T08:00:00Z",
  proposed_by: "emp_boss",
  sourcing_regions: ["guangdong"],
  supply_capabilities_note: "Hardware sourcing.",
  version_number: 1,
};

function jsonResponse(
  body: unknown,
  status = 200,
  headers: Record<string, string> = {},
): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

async function eventually(assertion: () => void): Promise<void> {
  let latestError: unknown;
  for (let attempt = 0; attempt < 100; attempt += 1) {
    await nextTick();
    await new Promise((resolve) => setTimeout(resolve, 0));
    try {
      assertion();
      return;
    } catch (error) {
      latestError = error;
    }
  }
  throw latestError;
}

async function mountSettings(fetch: typeof globalThis.fetch): Promise<{
  app: VueApp;
  root: HTMLElement;
}> {
  const root = document.createElement("div");
  document.body.replaceChildren(root);
  const app = createApp(App);
  app.provide(
    "tradeos-api-client",
    createApiClient({ baseUrl: "https://tradeos.test", fetch }),
  );
  app.use(router);
  await router.replace("/settings");
  app.mount(root);
  await new Promise((resolve) => setTimeout(resolve, 0));
  return { app, root };
}

function setField(root: HTMLElement, name: string, value: string): void {
  const field = root.querySelector<HTMLInputElement | HTMLTextAreaElement>(
    `[name="${name}"]`,
  );
  if (!field) throw new Error(`missing field ${name}`);
  field.value = value;
  field.dispatchEvent(new Event("input", { bubbles: true }));
}

function submit(root: HTMLElement): void {
  const form = root.querySelector<HTMLFormElement>("form");
  if (!form) throw new Error("missing form");
  form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
}

describe("SettingsCenter", () => {
  it("loads the overview and version history in parallel without inventing a first configuration", async () => {
    const pending = { ...activeVersion, playbook_version_id: "pbv_pending", version_number: 2 };
    const rejected = { ...activeVersion, playbook_version_id: "pbv_rejected", version_number: 3 };
    const applied = { ...activeVersion, playbook_version_id: "pbv_applied", version_number: 4 };
    const failed = { ...activeVersion, playbook_version_id: "pbv_failed", version_number: 5 };
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    const requested: string[] = [];
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      if (!(input instanceof Request)) throw new TypeError("Request required");
      const path = new URL(input.url).pathname;
      if (path.startsWith("/settings/playbook")) {
        requested.push(path);
        if (requested.length === 2) release();
        await gate;
      }
      if (path === "/settings/playbook") {
        return jsonResponse({
          active_version: null,
          configured: false,
          contact_enrichment: {
            reason_code: "COUNTRY_POLICY_NOT_CONFIGURED",
            state: "blocked",
          },
        });
      }
      if (path === "/settings/playbook/versions") {
        return jsonResponse([
          { application_error_code: null, approval_id: "apr_pending", approval_state: "pending", version: pending },
          { application_error_code: null, approval_id: "apr_rejected", approval_state: "rejected", version: rejected },
          { application_error_code: null, approval_id: "apr_applied", approval_state: "applied", version: applied },
          { application_error_code: "PLAYBOOK_BASE_VERSION_CONFLICT", approval_id: "apr_failed", approval_state: "apply_failed", version: failed },
        ]);
      }
      return jsonResponse({ code: "unexpected", message: "unexpected" }, 500);
    });

    const { root } = await mountSettings(fetch);

    await eventually(() => {
      expect(root.textContent).toContain("尚未配置 Company Playbook");
      expect(root.textContent).toContain("国家政策未配置，联系人补全保持阻断");
      expect(root.textContent).toContain("待审批");
      expect(root.textContent).toContain("已拒绝");
      expect(root.textContent).toContain("已生效");
      expect(root.textContent).toContain("应用失败");
      expect(root.textContent).toContain("基准版本冲突");
    });
    expect(requested.sort()).toEqual([
      "/settings/playbook",
      "/settings/playbook/versions",
    ]);
    expect(root.querySelector<HTMLInputElement>('[name="company_type"]')?.value).toBe("");
    expect(root.querySelector<HTMLInputElement>('[name="minimum_deal_amount"]')?.value).toBe("");
    expect(root.textContent).not.toMatch(/强制|直接应用|删除版本/);
  });

  it("copies the active version into a revision form and shows approval provenance", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      if (!(input instanceof Request)) throw new TypeError("Request required");
      const path = new URL(input.url).pathname;
      if (path === "/settings/playbook") {
        return jsonResponse({
          configured: true,
          contact_enrichment: { reason_code: "COUNTRY_POLICY_NOT_CONFIGURED", state: "blocked" },
          active_version: {
            activation: {
              activated_at: "2026-08-24T08:02:00Z",
              activated_by: "system:playbook-change",
              activation_id: "pba_active",
              approval_id: "apr_active",
              approved_at: "2026-08-24T08:01:00Z",
              approved_by: "emp_approver",
              change_set_ref: activeVersion.change_set_ref,
              content_hash: activeVersion.content_hash,
              playbook_version_id: versionId,
            },
            version: {
              ...activeVersion,
              content_provenance: {
                ...activeVersion.content_provenance,
                confirmed_at: "2026-08-24T08:01:00Z",
                confirmed_by: "emp_approver",
              },
            },
          },
        });
      }
      if (path === "/settings/playbook/versions") return jsonResponse([]);
      return jsonResponse({}, 500);
    });

    const { root } = await mountSettings(fetch);

    await eventually(() => expect(root.textContent).toContain("当前生效版本 v1"));
    expect(root.querySelector<HTMLInputElement>('[name="company_type"]')?.value).toBe("trading_company");
    expect(root.querySelector<HTMLInputElement>('[name="minimum_deal_amount"]')?.value).toBe("10000");
    expect(root.textContent).toContain("emp_boss");
    expect(root.textContent).toContain("emp_approver");
    expect(root.textContent).toContain("employee_input");
    expect(root.textContent).toContain("system:playbook-change");
  });

  it("keeps one idempotency key for a retry, rotates it after editing, and preserves Decimal text", async () => {
    const postKeys: string[] = [];
    const postBodies: unknown[] = [];
    let postAttempt = 0;
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      if (!(input instanceof Request)) throw new TypeError("Request required");
      const path = new URL(input.url).pathname;
      if (input.method === "GET" && path === "/settings/playbook") {
        return jsonResponse({
          active_version: null,
          configured: false,
          contact_enrichment: { reason_code: "COUNTRY_POLICY_NOT_CONFIGURED", state: "blocked" },
        });
      }
      if (input.method === "GET" && path === "/settings/playbook/versions") return jsonResponse([]);
      if (input.method === "POST" && path === "/settings/playbook/proposals") {
        postAttempt += 1;
        postKeys.push(input.headers.get("Idempotency-Key") ?? "");
        postBodies.push(await input.json());
        if (postAttempt < 3) return jsonResponse({ code: "temporarily_unavailable", message: "retry" }, 503, { "Retry-After": "7" });
        return jsonResponse({
          change_set_ref: "playbook:pbv_candidate:hash",
          playbook_version_id: "pbv_candidate",
          run_id: "run_candidate",
        }, 202);
      }
      return jsonResponse({}, 500);
    });
    const { root } = await mountSettings(fetch);
    await eventually(() => expect(root.textContent).toContain("尚未配置 Company Playbook"));

    setField(root, "company_type", "trading_company");
    setField(root, "minimum_deal_amount", "10000.0010");
    setField(root, "minimum_deal_currency", "USD");
    submit(root);
    await eventually(() => expect(root.textContent).toContain("7 秒后可重试"));

    submit(root);
    await eventually(() => expect(postKeys).toHaveLength(2));
    expect(postKeys[1]).toBe(postKeys[0]);

    setField(root, "minimum_deal_amount", "10000.0011");
    submit(root);
    await eventually(() => {
      expect(root.textContent).toContain("pbv_candidate");
      expect(root.textContent).toContain("run_candidate");
    });
    expect(postKeys[2]).not.toBe(postKeys[1]);
    expect(postBodies[2]).toMatchObject({ minimum_deal_amount: "10000.0011" });
    expect(root.querySelector<HTMLAnchorElement>('a[href="/approvals"]')).not.toBeNull();
    expect(root.querySelector<HTMLAnchorElement>('a[href="/runs/run_candidate"]')).not.toBeNull();
  });

  it("shows the boss-only permission boundary without leaking backend detail", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>(async () =>
      jsonResponse({ code: "permission_denied", message: "raw internal detail" }, 403),
    );

    const { root } = await mountSettings(fetch);

    await eventually(() => expect(root.textContent).toContain("只有老板可以查看或提交 Company Playbook"));
    expect(root.textContent).not.toContain("raw internal detail");
  });
});
