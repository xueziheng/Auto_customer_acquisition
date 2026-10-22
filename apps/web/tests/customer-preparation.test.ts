import { createApp, nextTick, type App } from "vue";
import { afterEach, expect, it } from "vitest";
import { createApiClient, type WebIdentityProvider } from "../src/api/client";
import CustomerPreparation from "../src/views/customers/CustomerPreparation.vue";
const time = "2026-10-13T00:00:00Z";
const allocation = {
  id: "cal_a", employee_id: "emp_a", account_id: "acc_a", email_verification: "unverified",
  allocated_at: time, assigned_at: time, assigned_by: "emp_b", candidate: {
    id: "pci_a", name: "Company", country: "NG", canonical_domain: "company.test", product_tags: ["electric_tricycle"],
    public_emails: ["info@company.test"], source_id: "source", source_url: "https://company.test/tricycles",
    evidence_text: "Company distributes tricycles.", observed_at: time, expires_at: "2027-01-01T00:00:00Z",
    provenance: { source_type: "web_page", source_id: "source", extracted_by: "human", extracted_at: time,
      confirmed_by: null, confirmed_at: null, page_hash: "b".repeat(64) as string | null, source_quote: null, source_url: "https://company.test/tricycles" },
  },
};
function view(canPrepare = true) {
  return { allocation_id: "cal_a", can_prepare: canPrepare, account: {
    account_id: "acc_a", tenant_id: "tn_a", name: "Company", country: "NG", created_at: time,
    website_domain: "company.test", entity_type: null, industry: null, source_signal_refs: [], field_provenance: {} }, contacts: [], hypotheses: [] };
}
const mounted: App[] = [];
afterEach(() => { mounted.splice(0).forEach(a => a.unmount()); document.body.replaceChildren(); });
async function settle() { for (let i = 0; i < 8; i++) { await nextTick(); await new Promise(r => setTimeout(r, 0)); } }
function json(body: unknown, status = 200) { return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }); }
async function mount(config: { canPrepare?: boolean; post?: (r: Request) => Promise<Response>; read?: () => Promise<Response> } = {}) {
  let generation = 0, employeeId = "emp_a";
  const listeners = new Set<() => void>(), writes: Request[] = [];
  const provider: WebIdentityProvider = { generation: () => generation,
    current: () => ({ tenantId: "tn_a", employeeId, mode: "fixed-dev" }),
    subscribe: l => { listeners.add(l); return () => listeners.delete(l); } };
  const client = createApiClient({ baseUrl: "https://tradeos.test", fetch: async input => {
    const r = input as Request;
    if (r.method === "POST") { writes.push(r.clone()); return config.post ? config.post(r) : json(view()); }
    return config.read ? config.read() : json(view(config.canPrepare));
  } }, provider);
  const host = document.createElement("div"); document.body.append(host);
  const app = createApp(CustomerPreparation, { allocation }); app.provide("tradeos-api-client", client);
  mounted.push(app); app.mount(host); await settle();
  return { host, writes, switchIdentity: () => { employeeId = "emp_other"; generation++; listeners.forEach(l => l()); } };
}
function input(host: Element, name: string, value: string) {
  const field = host.querySelector(`[name="${name}"]`) as HTMLInputElement | HTMLSelectElement;
  expect(field).not.toBeNull(); field.value = value;
  field.dispatchEvent(new Event("input", { bubbles: true })); field.dispatchEvent(new Event("change", { bubbles: true }));
}
function fillPublic(host: Element) {
  input(host, "entity_type", "distributor"); input(host, "industry", "Motor vehicles");
  input(host, "snapshot_artifact_ref", "art_01K00000000000000000000000");
  input(host, "page_hash", "b".repeat(64));
  input(host, "product_category", "electric_tricycle"); input(host, "fit_reasoning", "员工推断：渠道可能适配，需沟通验证。");
}
function save(host: Element) { (host.querySelector('[data-action="prepare"]') as HTMLButtonElement).click(); }
it("saves observed facts and an inference without inventing a legal basis or verification", async () => {
  const { host, writes } = await mount(); fillPublic(host); save(host); await settle();
  expect(writes).toHaveLength(1); const body = await writes[0]!.json();
  expect(body.public_observation).toBe("Company distributes tricycles.");
  expect(body.fit_reasoning).toBe("员工推断：渠道可能适配，需沟通验证。");
  expect(body.public_email).toBeNull(); expect(body.legal_basis).toBeNull(); expect(body.legal_basis_confirmed).toBe(false);
  expect(body).not.toHaveProperty("verification"); expect(body).not.toHaveProperty("confirmed_by");
  expect(host.textContent).toContain("推断");
});
it("blocks email recording until a real legal basis and assessment reference are entered", async () => {
  const { host, writes } = await mount(); fillPublic(host); input(host, "public_email", "info@company.test"); save(host); await settle();
  expect(writes).toHaveLength(0);
  input(host, "legal_basis", "legitimate_interest"); input(host, "legal_basis_source", "本人已查阅评估记录");
  input(host, "assessment_ref", "assessment-reviewed-001"); input(host, "subject_type", "legal_entity"); input(host, "contact_type", "role_based");
  const checkbox = host.querySelector('[name="legal_basis_confirmed"]') as HTMLInputElement;
  checkbox.checked = true; checkbox.dispatchEvent(new Event("change", { bubbles: true }));
  save(host); await settle(); expect(writes).toHaveLength(1);
  expect((await writes[0]!.json()).legal_basis.assessment_ref).toBe("assessment-reviewed-001");
});
it("hides mutation controls when the current server scope cannot prepare this customer", async () => {
  const { host } = await mount({ canPrepare: false }); expect(host.querySelector('[data-action="prepare"]')).toBeNull();
});
it("clears private form data on identity changes and ignores late save responses", async () => {
  let resolve!: (r: Response) => void; const pending = new Promise<Response>(r => { resolve = r; });
  const { host, switchIdentity } = await mount({ post: () => pending }); fillPublic(host); save(host); await settle();
  switchIdentity(); await settle(); resolve(json(view())); await settle();
  expect(host.textContent).not.toContain("资料已保存"); expect(host.querySelector('[name="fit_reasoning"]')).toBeNull();
});

it("reuses a genuine web page artifact reference from candidate provenance without manual entry", async () => {
  const sourceId = allocation.candidate.provenance.source_id;
  allocation.candidate.provenance.source_id = "art_01K00000000000000000000000";
  try {
    const { host, writes } = await mount();
    input(host, "entity_type", "distributor"); input(host, "industry", "Motor vehicles");
    input(host, "product_category", "electric_tricycle"); input(host, "fit_reasoning", "员工推断：渠道可能适配，需沟通验证。");
    save(host); await settle();
    expect(writes).toHaveLength(1);
    expect((await writes[0]!.json()).snapshot_artifact_ref).toBe("art_01K00000000000000000000000");
  } finally { allocation.candidate.provenance.source_id = sourceId; }
});

it.each([
  ["web_page", "source-without-artifact"],
  ["external_api", "art_01K00000000000000000000000"],
  ["web_page", "art_invalid"],
])("does not fabricate an artifact reference for incompatible provenance %s %s", async (sourceType, sourceId) => {
  const original = { ...allocation.candidate.provenance };
  allocation.candidate.provenance.source_type = sourceType;
  allocation.candidate.provenance.source_id = sourceId;
  try {
    const { host, writes } = await mount();
    input(host, "entity_type", "distributor"); input(host, "industry", "Motor vehicles");
    input(host, "product_category", "electric_tricycle"); input(host, "fit_reasoning", "员工推断：渠道可能适配，需沟通验证。");
    const field = host.querySelector('[name="snapshot_artifact_ref"]') as HTMLInputElement;
    expect(field.value).toBe(""); save(host); await settle(); expect(writes).toHaveLength(0);
  } finally { Object.assign(allocation.candidate.provenance, original); }
});


it("does not reuse an artifact when its page hash is missing", async () => {
  const original = { ...allocation.candidate.provenance };
  allocation.candidate.provenance.source_id = "art_01K00000000000000000000000";
  allocation.candidate.provenance.page_hash = null;
  try {
    const { host, writes } = await mount();
    input(host, "entity_type", "distributor"); input(host, "industry", "Motor vehicles");
    input(host, "product_category", "electric_tricycle"); input(host, "fit_reasoning", "员工推断：渠道可能适配，需沟通验证。");
    expect((host.querySelector('[name="snapshot_artifact_ref"]') as HTMLInputElement).value).toBe("");
    expect((host.querySelector('[name="page_hash"]') as HTMLInputElement).value).toBe("");
    save(host); await settle(); expect(writes).toHaveLength(0);
  } finally { Object.assign(allocation.candidate.provenance, original); }
});
