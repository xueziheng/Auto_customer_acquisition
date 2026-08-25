import { createApp, nextTick, type App as VueApp } from "vue";
import { describe, expect, it, vi } from "vitest";

import type { components } from "../src/api/api";
import { createApiClient } from "../src/api/client";
import App from "../src/App.vue";
import router from "../src/router";

type CountryPolicyVersionFixture = components["schemas"]["CountryPolicyVersionView"];
type ContactEnrichmentReadiness = components["schemas"]["ContactEnrichmentReadiness"];
type ProvenanceFixture = components["schemas"]["Provenance"];

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

function fieldProvenance(sourceId: string): ProvenanceFixture {
  return {
    confirmed_at: "2026-08-24T09:30:00Z",
    confirmed_by: "emp_policy_approver",
    extracted_at: "2026-08-24T09:00:00Z",
    extracted_by: "human:emp_policy_owner",
  page_hash: null,
  source_quote: null,
    source_id: sourceId,
    source_type: "employee_input",
    source_url: null,
  };
}

const activeCountryPolicyVersion: CountryPolicyVersionFixture = {
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

type ReadinessReason = NonNullable<ContactEnrichmentReadiness["reason_code"]>;
const defaultReadinessPayload = Symbol("default readiness payload");

interface CountryFetchOptions {
  activePolicies?: unknown[];
  allowedCount?: number;
  historyQueries?: string[];
  onHistory?: (country: string) => Promise<Response>;
  onOverview?: () => Promise<Response>;
  readinessPayload?: unknown;
  reason?: ReadinessReason | null;
  versions?: unknown[];
  onPost?: (request: Request) => Promise<Response>;
}

function countryPolicyFetch({
  activePolicies = [activeCountryPolicy],
  allowedCount = 1,
  historyQueries,
  onHistory,
  onOverview,
  readinessPayload = defaultReadinessPayload,
  reason = "CONTACT_ENRICHMENT_PROVIDER_NOT_CONFIGURED",
  versions = [],
  onPost,
}: CountryFetchOptions = {}): typeof globalThis.fetch {
  const contactEnrichment: unknown = readinessPayload === defaultReadinessPayload
    ? (reason === null
        ? { reason_code: null, state: "ready" }
        : { reason_code: reason, state: "blocked" })
    : readinessPayload;
  return vi.fn<typeof globalThis.fetch>(async (input) => {
    if (!(input instanceof Request)) throw new TypeError("Request required");
    const url = new URL(input.url);
    if (input.method === "GET" && url.pathname === "/settings/playbook") {
      return jsonResponse({
        active_version: null,
        configured: false,
        contact_enrichment: contactEnrichment,
      });
    }
    if (input.method === "GET" && url.pathname === "/settings/playbook/versions") {
      return jsonResponse([]);
    }
    if (input.method === "GET" && url.pathname === "/settings/country-policies") {
      if (onOverview) return onOverview();
      return jsonResponse({
        active_policies: activePolicies,
        contact_enrichment: contactEnrichment,
        coverage: {
          active_policy_count: activePolicies.length,
          contact_enrichment_allowed_count: allowedCount,
        },
      });
    }
    if (input.method === "GET" && url.pathname === "/settings/country-policies/versions") {
      const country = url.searchParams.get("country") ?? "";
      historyQueries?.push(country);
      if (onHistory) return onHistory(country);
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

function expectReadinessBanners(root: HTMLElement, expectedMessage: string): void {
  const playbook = root.querySelector<HTMLElement>(
    '[aria-label="Playbook 联系人补全就绪状态"]',
  );
  const countryPolicy = root.querySelector<HTMLElement>(
    '[aria-label="国家政策联系人补全就绪状态"]',
  );
  if (!playbook || !countryPolicy) throw new Error("missing readiness banners");

  expect(playbook.querySelector("strong")?.textContent?.trim()).toBe(expectedMessage);
  expect(countryPolicy.textContent?.trim()).toBe(expectedMessage);
  for (const banner of [playbook, countryPolicy]) {
    expect(banner.getAttribute("role")).toBe("status");
    expect(banner.getAttribute("aria-live")).toBe("polite");
    expect(banner.getAttribute("aria-label")).toBeTruthy();
    expect(banner.querySelectorAll("input, textarea, button")).toHaveLength(0);
  }
}

describe("SettingsCenter", () => {
  it("preserves top-level content height inside the vertically scrolling settings shell", async () => {
    await router.replace("/settings");
    const { root } = await mountSettings(countryPolicyFetch());
    await eventually(() => expect(root.textContent).toContain("国家政策包"));

    const shell = root.querySelector<HTMLElement>(".settings-shell");
    const workspace = root.querySelector<HTMLElement>(".country-policy-workspace");
    if (!shell || !workspace) throw new Error("missing settings layout");

    expect(shell.style.overflowY).toBe("auto");
    expect(shell.style.overflowX).toBe("hidden");
    expect([...shell.children]).not.toHaveLength(0);
    for (const child of shell.children) {
      expect((child as HTMLElement).style.flexShrink).toBe("0");
    }

    expect(workspace.style.overflowX).toBe("clip");
    expect(workspace.style.overflowY).toBe("visible");
  });

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
      expect(root.textContent).toContain("尚无任何已激活国家政策，联系人补全保持阻断");
      expect(root.textContent).not.toContain("先提交含目标/排除国家的 Playbook 候选");
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
  it.each([
    ["COUNTRY_POLICY_NOT_CONFIGURED", "尚无任何已激活国家政策，联系人补全保持阻断。"],
    ["CONTACT_ENRICHMENT_NOT_ALLOWED", "已激活政策均禁止联系人补全，系统不会调用外部 Provider。"],
  ] as const)("maps %s inside the Playbook section without claiming Playbook can fix it", async (reason, expected) => {
    const { app, root } = await mountSettings(countryPolicyFetch({
      activePolicies: reason === "COUNTRY_POLICY_NOT_CONFIGURED" ? [] : [activeCountryPolicy],
      allowedCount: 0,
      reason,
    }));

    await eventually(() => {
      expectReadinessBanners(root, expected);
      expect(root.querySelector<HTMLElement>(
        '[aria-label="Playbook 联系人补全就绪状态"]',
      )?.textContent).toContain("在下方国家政策包工作区单独录入、审批并激活");
    });
    expect(root.textContent).not.toContain("先提交含目标/排除国家的 Playbook 候选");
    app.unmount();
  });

  it.each([
    ["CONTACT_ENRICHMENT_PROVIDER_NOT_CONFIGURED", "部署尚未声明 Hunter 安全配置版本。"],
    ["CONTACT_ENRICHMENT_PROVIDER_VALIDATION_PENDING", "Hunter 配置已声明，等待人工 Provider 验证。"],
    ["CONTACT_ENRICHMENT_PROVIDER_VALIDATION_FAILED", "Hunter Provider 验证失败，请按固定分类排查。"],
    ["CONTACT_ENRICHMENT_PROVIDER_VALIDATION_INCONCLUSIVE", "Hunter 验证结果不确定，禁止自动重试。"],
    ["CONTACT_ENRICHMENT_RUNTIME_NOT_COMPOSED", "验证已通过，等待 scheduler 重启并完成工具注册。"],
  ] satisfies ReadonlyArray<readonly [ReadinessReason, string]>)(
    "renders Provider reason %s in both readiness banners",
    async (reason, expected) => {
      const { app, root } = await mountSettings(countryPolicyFetch({ reason }));

      await eventually(() => {
        expectReadinessBanners(root, expected);
      });
      app.unmount();
    },
  );

  it("renders READY truthfully in both banners while preserving per-country checks", async () => {
    const { app, root } = await mountSettings(countryPolicyFetch({ reason: null }));

    await eventually(() => {
      expectReadinessBanners(
        root,
        "联系人补全生产组合已就绪；每个目标国家仍会逐次检查国家政策。",
      );
    });
    app.unmount();
  });

  it.each([
    { reason_code: "CONTACT_ENRICHMENT_PROVIDER_NOT_CONFIGURED", state: "ready" },
    { reason_code: null, state: "blocked" },
    { reason_code: "CONTACT_ENRICHMENT_PROVIDER_NOT_CONFIGURED", state: "unknown" },
    { reason_code: "UNKNOWN_REASON", state: "blocked" },
    { state: "ready" },
    { state: "blocked" },
    null,
    "invalid-readiness",
    [],
    42,
  ])("renders malformed readiness payload %# neutrally", async (readinessPayload) => {
    const { app, root } = await mountSettings(countryPolicyFetch({ readinessPayload }));

    await eventually(() => {
      expectReadinessBanners(root, "正在核对联系人补全就绪状态。");
    });
    expect(root.textContent).not.toContain("联系人补全生产组合已就绪");
    app.unmount();
  });

  it("does not expose Provider credentials, validation success, or direct activation controls", async () => {
    const { app, root } = await mountSettings(countryPolicyFetch());
    await eventually(() => expect(root.textContent).toContain("国家政策包"));

    const forbiddenInput = [
      'input[type="password"]',
      'input[name*="hunter" i]',
      'input[name*="api_key" i]',
      'input[name*="apikey" i]',
      'input[name*="secret" i]',
      'input[name*="password" i]',
      'input[name*="credential" i]',
      'input[id*="hunter" i]',
      'input[aria-label*="hunter" i]',
      'input[aria-label*="api key" i]',
      'input[aria-label*="apikey" i]',
      'input[aria-label*="secret" i]',
      'input[aria-label*="password" i]',
      'input[aria-label*="credential" i]',
    ].join(",");
    const accessibleButtonNames = [...root.querySelectorAll<HTMLButtonElement>("button")]
      .map((button) => button.textContent?.trim() ?? "");
    const controlIdentity = [...root.querySelectorAll<HTMLElement>(
      "input, textarea, select, button",
    )].map((control) => [
      control.getAttribute("name"),
      control.id,
      control.getAttribute("aria-label"),
      control.getAttribute("placeholder"),
      control.tagName === "BUTTON" ? control.textContent : null,
    ].filter(Boolean).join(" "));

    expect(root.querySelector(forbiddenInput)).toBeNull();
    expect(controlIdentity).not.toEqual(expect.arrayContaining([
      expect.stringMatching(/api[\s_-]?key|secret|password|hunter.*credential/i),
    ]));
    expect(accessibleButtonNames).not.toEqual(expect.arrayContaining([
      expect.stringMatching(/验证成功|标记验证|验证 Hunter|直接激活|立即激活|激活 Hunter|Provider 激活/i),
    ]));
    expect(accessibleButtonNames).not.toContain("验证成功");
    expect(accessibleButtonNames).not.toContain("标记验证成功");
    expect(accessibleButtonNames).not.toContain("验证 Hunter");
    expect(accessibleButtonNames).not.toContain("直接激活");
    expect(accessibleButtonNames).not.toContain("立即激活");
    expect(accessibleButtonNames).not.toContain("激活 Hunter");
    app.unmount();
  });

  it("states that no legal defaults exist and requires authorized verified entry", async () => {
    const { root } = await mountSettings(countryPolicyFetch({
      activePolicies: [],
      allowedCount: 0,
      reason: "COUNTRY_POLICY_NOT_CONFIGURED",
    }));

    await eventually(() => expect(root.textContent).toContain("系统不提供国家法律默认值"));
    expect(root.textContent).toContain("由授权人员录入并确认已核验的政策事实");
  });

  it("shows server-bound extraction and confirmation actors and times for every source", async () => {
    const versions = [{
      application_error_code: null,
      approval_decided_at: "2026-08-24T09:30:00Z",
      approval_decided_by: "emp_policy_approver",
      approval_id: "apr_country_active",
      approval_state: "applied",
      version: activeCountryPolicyVersion,
    }];
    const { root } = await mountSettings(countryPolicyFetch({ versions }));

    await eventually(() => expect(root.textContent).toContain("已应用"));
    const active = root.querySelector<HTMLElement>('[aria-label="生效国家政策"]');
    const history = root.querySelector<HTMLElement>('[aria-label="国家政策版本历史"]');
    for (const scope of [active, history]) {
      expect(scope?.textContent).toContain("提取 human:emp_policy_owner");
      expect(scope?.textContent).toContain("确认 emp_policy_approver");
      expect(scope?.querySelector('time[datetime="2026-08-24T09:00:00Z"]')).not.toBeNull();
      expect(scope?.querySelector('time[datetime="2026-08-24T09:30:00Z"]')).not.toBeNull();
      expect(scope?.textContent).not.toContain("https://");
    }
  });

  it("queries country-scoped pending history even when no policy is active", async () => {
    const historyQueries: string[] = [];
    const pendingVersion = {
      ...candidateCountryPolicyVersion,
      country: "Synthetic Pending Market",
      country_key: "synthetic pending market",
    };
    const versions = [{
      application_error_code: null,
      approval_decided_at: null,
      approval_decided_by: null,
      approval_id: "apr_pending_only",
      approval_state: "pending",
      version: pendingVersion,
    }];
    const { root } = await mountSettings(countryPolicyFetch({
      activePolicies: [],
      allowedCount: 0,
      historyQueries,
      reason: "COUNTRY_POLICY_NOT_CONFIGURED",
      versions,
    }));
    await eventually(() => expect(root.textContent).toContain("尚无已激活国家政策"));

    const query = root.querySelector<HTMLInputElement>('[name="country_history_query"]');
    expect(query).not.toBeNull();
    if (!query) return;
    query.value = "Synthetic Pending Market";
    query.dispatchEvent(new Event("input", { bubbles: true }));
    clickButton(root, "查询国家历史");

    await eventually(() => expect(root.textContent).toContain("apr_pending_only"));
    expect(root.textContent).toContain("待审批");
    expect(historyQueries).toEqual(["Synthetic Pending Market"]);
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

  it("freezes an unknown network attempt and retries its exact body with the same key", async () => {
    const keys: string[] = [];
    const bodies: unknown[] = [];
    let attempts = 0;
    const { root } = await mountSettings(countryPolicyFetch({
      onPost: async (request) => {
        attempts += 1;
        keys.push(request.headers.get("Idempotency-Key") ?? "");
        bodies.push(await request.clone().json());
        if (attempts === 1) throw new TypeError("network result unknown");
        return jsonResponse({
          change_set_ref: candidateCountryPolicyVersion.change_set_ref,
          country_policy_version_id: candidateCountryPolicyVersion.country_policy_version_id,
          run_id: "run_unknown_retry",
        }, 202);
      },
    }));
    await eventually(() => expect(root.textContent).toContain("国家政策包"));
    fillCountryPolicy(root);
    const form = countryPolicyForm(root);
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await eventually(() => expect(root.textContent).toContain("网络结果未知"));

    const country = form.querySelector<HTMLInputElement>('[name="country"]');
    expect(country?.disabled).toBe(true);
    setCountryField(root, "country", "Changed Market Must Not Ship");
    await nextTick();
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));

    await eventually(() => expect(root.textContent).toContain("run_unknown_retry"));
    expect(attempts).toBe(2);
    expect(keys[1]).toBe(keys[0]);
    expect(bodies[1]).toEqual(bodies[0]);
    expect(bodies[1]).toMatchObject({ country: "Synthetic Market" });
    expect(form.querySelector<HTMLInputElement>('[name="country"]')?.disabled).toBe(false);
  });

  it("switches to the submitted country and refreshes its pending history after terminal acceptance", async () => {
    const historyQueries: string[] = [];
    const pending = {
      application_error_code: null,
      approval_decided_at: null,
      approval_decided_by: null,
      approval_id: "apr_after_submit",
      approval_state: "pending",
      version: candidateCountryPolicyVersion,
    };
    const { root } = await mountSettings(countryPolicyFetch({
      activePolicies: [],
      allowedCount: 0,
      historyQueries,
      reason: "COUNTRY_POLICY_NOT_CONFIGURED",
      versions: [pending],
      onPost: async () => jsonResponse({
        change_set_ref: candidateCountryPolicyVersion.change_set_ref,
        country_policy_version_id: candidateCountryPolicyVersion.country_policy_version_id,
        run_id: "run_pending_after_submit",
      }, 202),
    }));
    await eventually(() => expect(root.textContent).toContain("尚无已激活国家政策"));
    fillCountryPolicy(root);
    countryPolicyForm(root).dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));

    await eventually(() => expect(root.textContent).toContain("apr_after_submit"));
    expect(root.textContent).toContain("run_pending_after_submit");
    expect(historyQueries).toEqual(["Synthetic Market"]);
  });

  it("locks the country throughout a revision and only unlocks it after explicit exit", async () => {
    const { root } = await mountSettings(countryPolicyFetch());
    await eventually(() => expect(root.textContent).toContain("Synthetic Market"));
    expect(countryPolicyForm(root).querySelector<HTMLInputElement>('[name="country"]')?.value).toBe("");

    clickButton(root, "修订此政策");
    await nextTick();

    const country = countryPolicyForm(root).querySelector<HTMLInputElement>('[name="country"]');
    expect(country?.value).toBe("Synthetic Market");
    expect(country?.disabled).toBe(true);
    expect(countryPolicyForm(root).querySelector<HTMLSelectElement>('[name="cold_b2b_email_allowed"]')?.value).toBe("false");
    expect(root.textContent).toContain(`基准版本 ${countryPolicyVersionId}`);
    expect(root.textContent).toContain(`基准哈希 ${countryPolicyHash}`);

    clickButton(root, "取消修订并新建");
    await nextTick();
    expect(countryPolicyForm(root).querySelector<HTMLInputElement>('[name="country"]')?.disabled).toBe(false);
    expect(root.textContent).not.toContain(`基准版本 ${countryPolicyVersionId}`);
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

  it("derives each historical diff from its exact base and names a missing base", async () => {
    const version1: CountryPolicyVersionFixture = {
      ...activeCountryPolicyVersion,
      content_hash: "1".repeat(64),
      change_set_ref: `country_policy:cpp_01J00000000000000000000010:${"1".repeat(64)}`,
      country_policy_version_id: "cpp_01J00000000000000000000010",
      version_number: 1,
    };
    const version2 = {
      ...version1,
      base_content_hash: version1.content_hash,
      base_version_id: version1.country_policy_version_id,
      change_set_ref: `country_policy:cpp_01J00000000000000000000011:${"2".repeat(64)}`,
      cold_b2b_email_allowed: true,
      content_hash: "2".repeat(64),
      country_policy_version_id: "cpp_01J00000000000000000000011",
      version_number: 2,
    };
    const version3 = {
      ...version2,
      base_content_hash: version2.content_hash,
      base_version_id: version2.country_policy_version_id,
      change_set_ref: `country_policy:cpp_01J00000000000000000000012:${"3".repeat(64)}`,
      cold_b2b_email_allowed: false,
      content_hash: "3".repeat(64),
      country_policy_version_id: "cpp_01J00000000000000000000012",
      version_number: 3,
    };
    const missingBase = {
      ...version3,
      base_content_hash: "9".repeat(64),
      base_version_id: "cpp_01J00000000000000000000019",
      change_set_ref: `country_policy:cpp_01J00000000000000000000013:${"4".repeat(64)}`,
      content_hash: "4".repeat(64),
      country_policy_version_id: "cpp_01J00000000000000000000013",
      version_number: 4,
    };
    const activeV3 = {
      activation: {
        ...activeCountryPolicy.activation,
        change_set_ref: version3.change_set_ref,
        content_hash: version3.content_hash,
        country_policy_version_id: version3.country_policy_version_id,
      },
      version: version3,
    };
    const status = (version: CountryPolicyVersionFixture, suffix: string) => ({
      application_error_code: null,
      approval_decided_at: "2026-08-25T12:00:00Z",
      approval_decided_by: "emp_policy_approver",
      approval_id: `apr_${suffix}`,
      approval_state: "applied",
      version,
    });
    const { root } = await mountSettings(countryPolicyFetch({
      activePolicies: [activeV3],
      versions: [status(missingBase, "v4"), status(version3, "v3"), status(version2, "v2"), status(version1, "v1")],
    }));

    await eventually(() => expect(root.textContent).toContain("apr_v4"));
    const entries = [...root.querySelectorAll<HTMLElement>(".country-history-list > li")];
    const v2 = entries.find((entry) => entry.textContent?.includes(version2.country_policy_version_id));
    const v4 = entries.find((entry) => entry.textContent?.includes(missingBase.country_policy_version_id));
    expect(v2?.textContent).toContain("cold_b2b_email_allowed：禁止 → 允许");
    expect(v2?.textContent).not.toContain("cold_b2b_email_allowed：允许 → 禁止");
    expect(v4?.textContent).toContain("基准版本未在当前历史中");
    expect(v4?.textContent).not.toContain("cold_b2b_email_allowed：");
  });

  it("previews a first proposal as unconfigured to candidate before submission", async () => {
    const { root } = await mountSettings(countryPolicyFetch({
      activePolicies: [],
      allowedCount: 0,
      reason: "COUNTRY_POLICY_NOT_CONFIGURED",
    }));
    await eventually(() => expect(root.textContent).toContain("国家政策包"));
    fillCountryPolicy(root);
    await nextTick();

    expect(root.textContent).toContain("public_research_allowed：未配置 → 允许");
    expect(root.textContent).toContain("contact_enrichment_allowed：未配置 → 禁止");
  });

  it.each(["http-500", "network"] as const)(
    "refreshes submitted-country history after 202 even when the overview has a %s failure",
    async (failure) => {
      let overviewCalls = 0;
      const historyQueries: string[] = [];
      const pending = {
        application_error_code: null,
        approval_decided_at: null,
        approval_decided_by: null,
        approval_id: `apr_overview_${failure}`,
        approval_state: "pending",
        version: candidateCountryPolicyVersion,
      };
      const { root } = await mountSettings(countryPolicyFetch({
        activePolicies: [],
        allowedCount: 0,
        historyQueries,
        onOverview: async () => {
          overviewCalls += 1;
          if (overviewCalls === 1) {
            return jsonResponse({
              active_policies: [],
              contact_enrichment: {
                reason_code: "COUNTRY_POLICY_NOT_CONFIGURED",
                state: "blocked",
              },
              coverage: {
                active_policy_count: 0,
                contact_enrichment_allowed_count: 0,
              },
            });
          }
          if (failure === "network") throw new TypeError("overview unavailable");
          return jsonResponse({ code: "temporarily_unavailable", message: "safe" }, 500);
        },
        onPost: async () => jsonResponse({
          change_set_ref: candidateCountryPolicyVersion.change_set_ref,
          country_policy_version_id: candidateCountryPolicyVersion.country_policy_version_id,
          run_id: `run_overview_${failure}`,
        }, 202),
        reason: "COUNTRY_POLICY_NOT_CONFIGURED",
        versions: [pending],
      }));
      await eventually(() => expect(root.textContent).toContain("尚无已激活国家政策"));
      fillCountryPolicy(root);
      countryPolicyForm(root).dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));

      await eventually(() => expect(root.textContent).toContain(`apr_overview_${failure}`));
      expect(historyQueries).toEqual(["Synthetic Market"]);
      expect(root.textContent).toContain(failure === "network"
        ? "无法连接国家政策包服务"
        : "国家政策包读取失败，请稍后重试");
    },
  );

  it("finishes a terminal 202 before its submitted-country history refresh settles", async () => {
    let resolveHistory!: (response: Response) => void;
    const deferredHistory = new Promise<Response>((resolve) => {
      resolveHistory = resolve;
    });
    let postCount = 0;
    const pending = {
      application_error_code: null,
      approval_decided_at: null,
      approval_decided_by: null,
      approval_id: "apr_deferred_history",
      approval_state: "pending",
      version: candidateCountryPolicyVersion,
    };
    const { root } = await mountSettings(countryPolicyFetch({
      activePolicies: [],
      allowedCount: 0,
      onHistory: async () => deferredHistory,
      onPost: async () => {
        postCount += 1;
        return jsonResponse({
          change_set_ref: candidateCountryPolicyVersion.change_set_ref,
          country_policy_version_id: candidateCountryPolicyVersion.country_policy_version_id,
          run_id: "run_deferred_history",
        }, 202);
      },
      reason: "COUNTRY_POLICY_NOT_CONFIGURED",
    }));
    await eventually(() => expect(root.textContent).toContain("尚无已激活国家政策"));
    fillCountryPolicy(root);
    countryPolicyForm(root).dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));

    await eventually(() => expect(root.textContent).toContain("run_deferred_history"));
    const submitButton = countryPolicyForm(root).querySelector<HTMLButtonElement>('button[type="submit"]');
    const countryInput = countryPolicyForm(root).querySelector<HTMLInputElement>('[name="country"]');
    const modeButton = root.querySelector<HTMLButtonElement>(
      'section[aria-labelledby="country-policy-title"] > header button',
    );
    expect(submitButton?.textContent?.trim()).toBe("提交国家政策审批候选");
    expect(submitButton?.disabled).toBe(false);
    expect(countryInput?.disabled).toBe(false);
    expect(modeButton?.disabled).toBe(false);
    expect(postCount).toBe(1);

    resolveHistory(jsonResponse([pending]));
    await eventually(() => expect(root.textContent).toContain("apr_deferred_history"));
    expect(postCount).toBe(1);
  });

  it("finishes a terminal 202 before its overview refresh settles", async () => {
    let resolveOverview!: (response: Response) => void;
    const deferredOverview = new Promise<Response>((resolve) => {
      resolveOverview = resolve;
    });
    let overviewCalls = 0;
    let postCount = 0;
    const pending = {
      application_error_code: null,
      approval_decided_at: null,
      approval_decided_by: null,
      approval_id: "apr_while_overview_pending",
      approval_state: "pending",
      version: candidateCountryPolicyVersion,
    };
    const overviewResponse = {
      active_policies: [],
      contact_enrichment: {
        reason_code: "COUNTRY_POLICY_NOT_CONFIGURED",
        state: "blocked",
      },
      coverage: {
        active_policy_count: 0,
        contact_enrichment_allowed_count: 0,
      },
    };
    const { root } = await mountSettings(countryPolicyFetch({
      activePolicies: [],
      allowedCount: 0,
      onOverview: async () => {
        overviewCalls += 1;
        return overviewCalls === 1 ? jsonResponse(overviewResponse) : deferredOverview;
      },
      onPost: async () => {
        postCount += 1;
        return jsonResponse({
          change_set_ref: candidateCountryPolicyVersion.change_set_ref,
          country_policy_version_id: candidateCountryPolicyVersion.country_policy_version_id,
          run_id: "run_deferred_overview",
        }, 202);
      },
      reason: "COUNTRY_POLICY_NOT_CONFIGURED",
      versions: [pending],
    }));
    await eventually(() => expect(root.textContent).toContain("尚无已激活国家政策"));
    fillCountryPolicy(root);
    countryPolicyForm(root).dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));

    await eventually(() => expect(root.textContent).toContain("apr_while_overview_pending"));
    expect(root.textContent).toContain("run_deferred_overview");
    const submitButton = countryPolicyForm(root).querySelector<HTMLButtonElement>('button[type="submit"]');
    const countryInput = countryPolicyForm(root).querySelector<HTMLInputElement>('[name="country"]');
    const modeButton = root.querySelector<HTMLButtonElement>(
      'section[aria-labelledby="country-policy-title"] > header button',
    );
    expect(submitButton?.textContent?.trim()).toBe("提交国家政策审批候选");
    expect(submitButton?.disabled).toBe(false);
    expect(countryInput?.disabled).toBe(false);
    expect(modeButton?.disabled).toBe(false);
    expect(postCount).toBe(1);

    resolveOverview(jsonResponse(overviewResponse));
    await nextTick();
    expect(postCount).toBe(1);
  });

  it.each(["late-success", "late-error"] as const)(
    "keeps country B history when country A finishes with %s",
    async (lateOutcome) => {
      let resolveA!: (response: Response) => void;
      let rejectA!: (error: Error) => void;
      const delayedA = new Promise<Response>((resolve, reject) => {
        resolveA = resolve;
        rejectA = reject;
      });
      const versionB: CountryPolicyVersionFixture = {
        ...activeCountryPolicyVersion,
        change_set_ref: `country_policy:cpp_01J00000000000000000000020:${"5".repeat(64)}`,
        content_hash: "5".repeat(64),
        country: "Synthetic B Market",
        country_key: "synthetic b market",
        country_policy_version_id: "cpp_01J00000000000000000000020",
      };
      const status = (version: CountryPolicyVersionFixture, approvalId: string) => ({
        application_error_code: null,
        approval_decided_at: null,
        approval_decided_by: null,
        approval_id: approvalId,
        approval_state: "pending",
        version,
      });
      const { root } = await mountSettings(countryPolicyFetch({
        onHistory: async (country) => country === "Synthetic Market"
          ? delayedA
          : jsonResponse([status(versionB, "apr_country_b")]),
      }));
      await eventually(() => expect(
        root.querySelector<HTMLInputElement>('[name="country_history_query"]'),
      ).not.toBeNull());
      const query = root.querySelector<HTMLInputElement>('[name="country_history_query"]');
      if (!query) return;
      query.value = "Synthetic B Market";
      query.dispatchEvent(new Event("input", { bubbles: true }));
      clickButton(root, "查询国家历史");
      await eventually(() => expect(root.textContent).toContain("apr_country_b"));

      if (lateOutcome === "late-success") {
        resolveA(jsonResponse([status(activeCountryPolicyVersion, "apr_country_a")]));
      } else {
        rejectA(new TypeError("stale A failed"));
      }
      await new Promise((resolve) => setTimeout(resolve, 0));
      await nextTick();

      const history = root.querySelector<HTMLElement>('[aria-label="国家政策版本历史"]');
      expect(history?.textContent).toContain("Synthetic B Market");
      expect(history?.textContent).toContain("apr_country_b");
      expect(history?.textContent).not.toContain("apr_country_a");
      expect(history?.textContent).not.toContain("国家政策历史读取失败");
    },
  );

  it.each([
    ["forbidden", "国家政策历史暂不可用"],
    ["server", "国家政策历史读取失败，请稍后重试"],
    ["network", "国家政策历史读取失败，请稍后重试"],
  ] as const)("distinguishes %s history failure from empty and recovers on retry", async (failure, message) => {
    let historyCalls = 0;
    const recovered = {
      application_error_code: null,
      approval_decided_at: null,
      approval_decided_by: null,
      approval_id: `apr_recovered_${failure}`,
      approval_state: "pending",
      version: candidateCountryPolicyVersion,
    };
    const { root } = await mountSettings(countryPolicyFetch({
      activePolicies: [],
      allowedCount: 0,
      onHistory: async () => {
        historyCalls += 1;
        if (historyCalls > 1) return jsonResponse([recovered]);
        if (failure === "network") throw new TypeError("history network failed");
        return jsonResponse(
          { code: "history_unavailable", message: "safe" },
          failure === "forbidden" ? 403 : 500,
        );
      },
      reason: "COUNTRY_POLICY_NOT_CONFIGURED",
    }));
    await eventually(() => expect(root.textContent).toContain("尚无已激活国家政策"));
    const query = root.querySelector<HTMLInputElement>('[name="country_history_query"]');
    if (!query) throw new Error("missing country history query");
    query.value = "Synthetic Market";
    query.dispatchEvent(new Event("input", { bubbles: true }));
    clickButton(root, "查询国家历史");

    await eventually(() => expect(root.textContent).toContain(message));
    const history = root.querySelector<HTMLElement>('[aria-label="国家政策版本历史"]');
    expect(history?.textContent).not.toContain("尚无可显示的国家政策历史");

    clickButton(root, "查询国家历史");
    await eventually(() => expect(root.textContent).toContain(`apr_recovered_${failure}`));
    expect(root.textContent).not.toContain(message);
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

  it("maps missing Provider configuration without claiming ready", async () => {
    const { root } = await mountSettings(countryPolicyFetch());

    await eventually(() => expect(root.textContent).toContain("部署尚未声明 Hunter 安全配置版本"));
    expect(root.textContent).not.toContain("联系人补全生产组合已就绪");
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
