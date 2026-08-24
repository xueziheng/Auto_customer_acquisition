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

const countryPolicyFields = [
  "public_research_allowed",
  "contact_enrichment_allowed",
  "cold_b2b_email_allowed",
  "personal_data_basis_required",
  "subject_type_affects_judgment",
  "contact_type_affects_judgment",
  "opt_out_deadline_days",
  "local_representative_required",
  "requirements",
] as const;

const countryPolicyVersionId = "cpp_01J00000000000000000000000";
const countryPolicyHash = "b".repeat(64);
const countryPolicyBaseHash = "a".repeat(64);

function fieldProvenance(sourceId: string) {
  return {
    confirmed_at: "2026-08-24T09:30:00Z",
    confirmed_by: "emp_policy_approver",
    extracted_at: "2026-08-24T09:00:00Z",
    extracted_by: "human:emp_policy_owner",
    page_hash: null,
    source_id: sourceId,
    source_type: "employee_input",
    source_url: null,
  };
}

const activeCountryPolicyVersion = {
  base_content_hash: null,
  base_version_id: null,
  change_set_ref: `country_policy:${countryPolicyVersionId}:${countryPolicyHash}`,
  cold_b2b_email_allowed: false,
  contact_enrichment_allowed: true,
  contact_type_affects_judgment: true,
  content_hash: countryPolicyHash,
  country: "Synthetic Market",
  country_key: "synthetic market",
  country_policy_version_id: countryPolicyVersionId,
  field_provenance: Object.fromEntries(
    countryPolicyFields.map((field) => [field, fieldProvenance(`src.${field}`)]),
  ),
  local_representative_required: false,
  notes: "Only facts confirmed by the policy owner.",
  opt_out_deadline_days: 30,
  personal_data_basis_required: true,
  proposed_at: "2026-08-24T09:00:00Z",
  proposed_by: "emp_policy_owner",
  public_research_allowed: true,
  requirements: ["consent_record"],
  subject_type_affects_judgment: true,
  version_number: 1,
};

const activeCountryPolicy = {
  activation: {
    activated_at: "2026-08-24T10:00:00Z",
    activated_by: "system:country-policy-change",
    activation_id: "cpa_01J00000000000000000000000",
    activation_sequence: 1,
    approval_id: "apr_country_active",
    approved_at: "2026-08-24T09:30:00Z",
    approved_by: "emp_policy_approver",
    change_set_ref: activeCountryPolicyVersion.change_set_ref,
    content_hash: countryPolicyHash,
    country_key: "synthetic market",
    country_policy_version_id: countryPolicyVersionId,
  },
  version: activeCountryPolicyVersion,
};

const candidateCountryPolicyVersion = {
  ...activeCountryPolicyVersion,
  base_content_hash: countryPolicyHash,
  base_version_id: countryPolicyVersionId,
  change_set_ref: `country_policy:cpp_01J00000000000000000000001:${countryPolicyBaseHash}`,
  cold_b2b_email_allowed: true,
  contact_enrichment_allowed: false,
  content_hash: countryPolicyBaseHash,
  country_policy_version_id: "cpp_01J00000000000000000000001",
  notes: "Candidate facts awaiting independent approval.",
  proposed_at: "2026-08-25T11:00:00Z",
  proposed_by: "emp_policy_reviser",
  version_number: 2,
};

type ReadinessReason =
  | "COUNTRY_POLICY_NOT_CONFIGURED"
  | "CONTACT_ENRICHMENT_NOT_ALLOWED"
  | "CONTACT_ENRICHMENT_NOT_COMPOSED";

interface CountryFetchOptions {
  activePolicies?: unknown[];
  allowedCount?: number;
  reason?: ReadinessReason;
  versions?: unknown[];
  onPost?: (request: Request) => Promise<Response>;
}

function countryPolicyFetch({
  activePolicies = [activeCountryPolicy],
  allowedCount = 1,
  reason = "CONTACT_ENRICHMENT_NOT_COMPOSED",
  versions = [],
  onPost,
}: CountryFetchOptions = {}): typeof globalThis.fetch {
  return vi.fn<typeof globalThis.fetch>(async (input) => {
    if (!(input instanceof Request)) throw new TypeError("Request required");
    const url = new URL(input.url);
    if (input.method === "GET" && url.pathname === "/settings/playbook") {
      return jsonResponse({
        active_version: null,
        configured: false,
        contact_enrichment: { reason_code: reason, state: "blocked" },
      });
    }
    if (input.method === "GET" && url.pathname === "/settings/playbook/versions") {
      return jsonResponse([]);
    }
    if (input.method === "GET" && url.pathname === "/settings/country-policies") {
      return jsonResponse({
        active_policies: activePolicies,
        contact_enrichment: { reason_code: reason, state: "blocked" },
        coverage: {
          active_policy_count: activePolicies.length,
          contact_enrichment_allowed_count: allowedCount,
        },
      });
    }
    if (input.method === "GET" && url.pathname === "/settings/country-policies/versions") {
      return jsonResponse(versions);
    }
    if (input.method === "POST" && url.pathname === "/settings/country-policies/proposals" && onPost) {
      return onPost(input);
    }
    return jsonResponse({ code: "unexpected", message: url.pathname }, 500);
  });
}

function countryPolicyForm(root: HTMLElement): HTMLFormElement {
  const form = root.querySelector<HTMLFormElement>('form[aria-label="国家政策候选表单"]');
  if (!form) throw new Error("missing country policy form");
  return form;
}

function setCountryField(root: HTMLElement, name: string, value: string): void {
  const form = countryPolicyForm(root);
  const field = form.querySelector<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>(
    `[name="${name}"]`,
  );
  if (!field) throw new Error(`missing country policy field ${name}`);
  field.value = value;
  field.dispatchEvent(new Event("change", { bubbles: true }));
  field.dispatchEvent(new Event("input", { bubbles: true }));
}

function clickButton(root: HTMLElement, name: string): void {
  const button = [...root.querySelectorAll<HTMLButtonElement>("button")]
    .find((candidate) => candidate.textContent?.trim() === name);
  if (!button) throw new Error(`missing button ${name}`);
  button.click();
}

function fillCountryPolicy(root: HTMLElement): void {
  setCountryField(root, "country", "Synthetic Market");
  setCountryField(root, "public_research_allowed", "true");
  setCountryField(root, "contact_enrichment_allowed", "false");
  setCountryField(root, "cold_b2b_email_allowed", "true");
  setCountryField(root, "personal_data_basis_required", "true");
  setCountryField(root, "subject_type_affects_judgment", "false");
  setCountryField(root, "contact_type_affects_judgment", "true");
  setCountryField(root, "opt_out_deadline_days", "21");
  setCountryField(root, "local_representative_required", "false");
  setCountryField(root, "requirements", "consent_record, suppression_check");
  setCountryField(root, "notes", "Verified by an authorized policy owner.");
  countryPolicyFields.forEach((field, index) => {
    setCountryField(root, `source_${field}`, `safe.policy.${index + 1}`);
  });
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

describe("SettingsCenter country policy workspace", () => {
  it("states that no legal defaults exist and requires authorized verified entry", async () => {
    const { root } = await mountSettings(countryPolicyFetch({
      activePolicies: [],
      allowedCount: 0,
      reason: "COUNTRY_POLICY_NOT_CONFIGURED",
    }));

    await eventually(() => expect(root.textContent).toContain("系统不提供国家法律默认值"));
    expect(root.textContent).toContain("由授权人员录入并确认已核验的政策事实");
  });

  it("renders configured and enrichment-allowed counts separately with active action facts", async () => {
    const secondPolicy = {
      ...activeCountryPolicy,
      activation: {
        ...activeCountryPolicy.activation,
        activation_id: "cpa_01J00000000000000000000001",
        country_key: "synthetic second market",
      },
      version: {
        ...activeCountryPolicyVersion,
        contact_enrichment_allowed: false,
        country: "Synthetic Second Market",
        country_key: "synthetic second market",
      },
    };
    const { root } = await mountSettings(countryPolicyFetch({
      activePolicies: [activeCountryPolicy, secondPolicy],
      allowedCount: 1,
    }));

    await eventually(() => expect(root.textContent).toContain("生效国家政策 2"));
    expect(root.textContent).toContain("允许联系人补全 1");
    expect(root.textContent).toContain("public_research：允许");
    expect(root.textContent).toContain("cold_b2b_email：禁止");
    expect(root.querySelector('time[datetime="2026-08-24T10:00:00Z"]')).not.toBeNull();
  });

  it("blocks submission and sends zero POST requests when one safe source is missing", async () => {
    let postCount = 0;
    const { root } = await mountSettings(countryPolicyFetch({
      onPost: async () => {
        postCount += 1;
        return jsonResponse({}, 500);
      },
    }));
    await eventually(() => expect(root.textContent).toContain("国家政策包"));

    fillCountryPolicy(root);
    setCountryField(root, "source_requirements", "");
    const form = countryPolicyForm(root);
    const button = form.querySelector<HTMLButtonElement>('button[type="submit"]');
    expect(button?.disabled).toBe(true);
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await nextTick();

    expect(root.textContent).toContain("每个决策字段都必须填写安全来源");
    expect(postCount).toBe(0);
  });

  it("posts the exact strict proposal once and keeps one key throughout the in-flight attempt", async () => {
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    const requests: Request[] = [];
    const bodies: unknown[] = [];
    const { root } = await mountSettings(countryPolicyFetch({
      onPost: async (request) => {
        requests.push(request);
        bodies.push(await request.clone().json());
        await gate;
        return jsonResponse({
          change_set_ref: "country_policy:cpp_candidate:hash",
          country_policy_version_id: "cpp_candidate",
          run_id: "run_country_candidate",
        }, 202);
      },
    }));
    await eventually(() => expect(root.textContent).toContain("国家政策包"));
    fillCountryPolicy(root);

    const form = countryPolicyForm(root);
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await eventually(() => expect(requests).toHaveLength(1));

    expect(requests[0]?.headers.get("Idempotency-Key")).toMatch(/^settings-country-policy-/);
    expect(bodies).toEqual([{
      cold_b2b_email_allowed: true,
      contact_enrichment_allowed: false,
      contact_type_affects_judgment: true,
      country: "Synthetic Market",
      field_sources: {
        cold_b2b_email_allowed: { source_id: "safe.policy.3", source_type: "employee_input" },
        contact_enrichment_allowed: { source_id: "safe.policy.2", source_type: "employee_input" },
        contact_type_affects_judgment: { source_id: "safe.policy.6", source_type: "employee_input" },
        local_representative_required: { source_id: "safe.policy.8", source_type: "employee_input" },
        opt_out_deadline_days: { source_id: "safe.policy.7", source_type: "employee_input" },
        personal_data_basis_required: { source_id: "safe.policy.4", source_type: "employee_input" },
        public_research_allowed: { source_id: "safe.policy.1", source_type: "employee_input" },
        requirements: { source_id: "safe.policy.9", source_type: "employee_input" },
        subject_type_affects_judgment: { source_id: "safe.policy.5", source_type: "employee_input" },
      },
      local_representative_required: false,
      notes: "Verified by an authorized policy owner.",
      opt_out_deadline_days: 21,
      personal_data_basis_required: true,
      public_research_allowed: true,
      requirements: ["consent_record", "suppression_check"],
      subject_type_affects_judgment: false,
    }]);

    release();
    await eventually(() => expect(root.textContent).toContain("run_country_candidate"));
    await eventually(() => {
      const button = countryPolicyForm(root).querySelector<HTMLButtonElement>('button[type="submit"]');
      expect(button?.disabled).toBe(false);
    });
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await eventually(() => expect(requests).toHaveLength(2));
    expect(requests[1]?.headers.get("Idempotency-Key")).not.toBe(
      requests[0]?.headers.get("Idempotency-Key"),
    );
  });

  it("copies the selected active policy into a revision and displays its exact base", async () => {
    const { root } = await mountSettings(countryPolicyFetch());
    await eventually(() => expect(root.textContent).toContain("Synthetic Market"));
    expect(countryPolicyForm(root).querySelector<HTMLInputElement>('[name="country"]')?.value).toBe("");

    clickButton(root, "修订此政策");
    await nextTick();

    expect(countryPolicyForm(root).querySelector<HTMLInputElement>('[name="country"]')?.value).toBe("Synthetic Market");
    expect(countryPolicyForm(root).querySelector<HTMLSelectElement>('[name="cold_b2b_email_allowed"]')?.value).toBe("false");
    expect(root.textContent).toContain(`基准版本 ${countryPolicyVersionId}`);
    expect(root.textContent).toContain(`基准哈希 ${countryPolicyHash}`);
  });

  it("shows literal before and after values with pending and applied decision facts", async () => {
    const versions = [
      {
        application_error_code: null,
        approval_decided_at: null,
        approval_decided_by: null,
        approval_id: "apr_country_pending",
        approval_state: "pending",
        version: candidateCountryPolicyVersion,
      },
      {
        application_error_code: null,
        approval_decided_at: "2026-08-24T09:30:00Z",
        approval_decided_by: "emp_policy_approver",
        approval_id: "apr_country_active",
        approval_state: "applied",
        version: activeCountryPolicyVersion,
      },
    ];
    const { root } = await mountSettings(countryPolicyFetch({ versions }));

    await eventually(() => expect(root.textContent).toContain("待审批"));
    expect(root.textContent).toContain("已应用");
    expect(root.textContent).toContain("cold_b2b_email_allowed：禁止 → 允许");
    expect(root.textContent).toContain("contact_enrichment_allowed：允许 → 禁止");
    expect(root.textContent).toContain("提案人 emp_policy_reviser");
    expect(root.textContent).toContain("决定人 emp_policy_approver");
    expect(root.querySelector('time[datetime="2026-08-24T09:30:00Z"]')).not.toBeNull();
  });

  it("maps missing policy readiness to its dedicated explanation", async () => {
    const { root } = await mountSettings(countryPolicyFetch({
      activePolicies: [],
      allowedCount: 0,
      reason: "COUNTRY_POLICY_NOT_CONFIGURED",
    }));

    await eventually(() => expect(root.textContent).toContain("尚无任何已激活国家政策，联系人补全保持阻断"));
    expect(root.textContent).not.toContain("Hunter / Provider 生产组合尚未完成");
  });

  it("maps all-denied readiness to a distinct explanation", async () => {
    const { root } = await mountSettings(countryPolicyFetch({
      allowedCount: 0,
      reason: "CONTACT_ENRICHMENT_NOT_ALLOWED",
    }));

    await eventually(() => expect(root.textContent).toContain("已激活政策均禁止联系人补全"));
    expect(root.textContent).not.toContain("尚无任何已激活国家政策");
  });

  it("maps composition readiness to the Hunter and Provider explanation without claiming ready", async () => {
    const { root } = await mountSettings(countryPolicyFetch());

    await eventually(() => expect(root.textContent).toContain("Hunter / Provider 生产组合尚未完成"));
    expect(root.textContent).toContain("即使已有允许政策，联系人补全仍保持阻断");
    expect(root.textContent).not.toContain("联系人补全已就绪");
  });

  it("does not expose delete, direct activation, force-apply, or legal-template controls", async () => {
    const { root } = await mountSettings(countryPolicyFetch());
    await eventually(() => expect(root.textContent).toContain("国家政策包"));
    const accessibleButtonNames = [...root.querySelectorAll<HTMLButtonElement>("button")]
      .map((button) => button.textContent?.trim() ?? "");

    expect(accessibleButtonNames).not.toContain("删除政策");
    expect(accessibleButtonNames).not.toContain("直接激活");
    expect(accessibleButtonNames).not.toContain("立即应用");
    expect(accessibleButtonNames).not.toContain("强制应用");
    expect(accessibleButtonNames).not.toContain("使用法律模板");
    expect(root.querySelector('[aria-label*="法律模板"]')).toBeNull();
  });
});
