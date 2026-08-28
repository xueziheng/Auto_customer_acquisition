import { afterEach, expect, it, vi } from "vitest";
import { createApp, h, nextTick, ref, type App as VueApp, type Ref } from "vue";
import App from "../src/App.vue";
import router from "../src/router";
import { configureAuthenticatedIdentity, clearAuthenticatedIdentity, createApiClient, type WebIdentityProvider } from "../src/api/client";
import type { components } from "../src/api/api";
import { createQuotePriceBody, utf16SelectionToCodepoints } from "../src/views/costing-quotes/quote-input";
import NeedUnitConfirmationForm from "../src/views/costing-quotes/NeedUnitConfirmationForm.vue";

it("keeps precise money text without Number coercion", () => {
  expect(createQuotePriceBody("0.123456789012", "USD")).toEqual({ amount: "0.123456789012", currency: "USD" });
  expect(createQuotePriceBody(" 00012.3400 ", " EUR ")).toEqual({ amount: "00012.3400", currency: "EUR" });
  expect(() => createQuotePriceBody(" ", "USD")).toThrow();
  expect(() => createQuotePriceBody("1", " ")).toThrow();
});

const opp = "opp_01M0PWRX23T9DP9ENM9PW5GFC8";
const need = "need_01M0PWRX23T9DP9ENM9PW5GFC8";
const messageId = "msg_01M0PWRX23T9DP9ENM9PW5GFC8";
const sheetId = "cost_01M0PWSK0GJTR12HTQC29BSDBG";
const provenance: components["schemas"]["shared__schemas__provenance__ProvenanceSummary"] = {
  source_type: "employee_input", source_id: "source-1", extracted_by: "human", extracted_at: "2026-08-28T00:00:00Z", confirmed_by: "employee-a", confirmed_at: "2026-08-28T00:00:00Z",
};
const context: components["schemas"]["QuotePreparationPublicView"] = {
  account_id: "account", account_name: "客户", blockers: [], checked_at: "2026-08-28T00:00:00Z",
  context_hash: "context-hash", country: "DE", issuer: null, need_facts_hash: "need-hash", opportunity_id: opp,
  owner_id: "owner", prepared_by: "employee-a", quantity_fact_hash: "quantity-hash", quantity_status: "current", unit_status: "missing",
  specification_hash: "spec-hash", specification: { product_category: "parts", material: "steel", size_spec: "M8", application: null, certification_required: null, packaging: null },
  need: { account_id: "account", application: null, certification_required: null, current_supply_issue: null, destination: "Hamburg", material: "steel", need_id: need, origins: { quantity: provenance }, packaging: null, product_category: "parts", quantity: 100, required_by: "2026-10-01", size_spec: "M8", status: "validated", target_price: null, unit: null, unit_confirmation_id: null, unit_quantity_fact_hash: null },
};
const sheet: components["schemas"]["CostSheetView"] = {
  base_currency: "USD", content_hash: "sheet-hash", cost_sheet_id: sheetId, created_at: "2026-08-28T00:00:00Z", fx_rates: [], has_indicative_items: false, is_locked: false,
  items: [{ amount: { amount: "0", currency: "USD" }, is_pending_confirmation: false, is_per_unit: true, item_label: "产品采购", item_sequence: 7, item_type: "product_purchase", price_basis: "quoted", source_ref: "evidence-1" }], opportunity_id: opp, quantity: 100, quote_currency: "USD", version_number: 1, version_type: "quoted",
};
const file: components["schemas"]["QuoteFileView"] = {
  artifact_id: "artifact", content_hash: "bytes-hash", customer_content_hash: "customer-hash", file_id: "file-1", generated_at: "2026-08-28T00:00:00Z", quote_content_hash: "quote-hash", quote_id: "quote-1", quote_version: 1, size_bytes: 3, template_version: "v1",
};
const supplierEvidence: components["schemas"]["SupplierPriceEvidencePublicView"] = {
  amount: "0.123456789012", basis: "quoted", confirmed_at: "2026-08-28T00:00:00Z", confirmed_by: "employee-a", currency: "USD", destination: "Hamburg", evidence_hash: "evidence-hash", evidence_id: "evidence-1", field_provenance: {}, kind: "supplier_price", moq: 10, need_id: need, opportunity_id: opp, quantity_max: 200, quantity_min: 100, quoted_at: "2026-08-28T00:00:00Z", source: { artifact_id: "artifact", content_hash: "raw-hash", observed_at: "2026-08-28T00:00:00Z", source_ref: "source-a", source_type: "upload" }, specification: "M8 steel", supplier_ref: "supplier-1", unit: "piece", valid_until: "2026-10-01T00:00:00Z",
};
const calculation: components["schemas"]["CalculationSnapshot"] = {
  base_currency: "USD", computed_at: "2026-08-28T00:00:00Z", context_hash: "context-hash", cost_sheet_id: sheetId, displayed_total: { amount: "12.35", currency: "USD" }, displayed_unit_price: { amount: "0.1235", currency: "USD" }, effective_unit_revenue: { amount: "0.1235", currency: "USD" }, inputs_hash: "inputs-hash", metrics: { additional_acquisition_headroom: "0.01", contribution_profit: "0.02", discount_headroom: "0.01", full_cost_profit: "0.02", gross_profit: "0.02", margin_rate: "0.15", minimum_price: "0.10", target_price: "0.12", unit_full_cost: "0.10" }, policy_id: "policy-1", quote_currency: "USD", version_number: 1,
};
const issuer: components["schemas"]["QuoteIssuerPublicView"] = { address: "Company address", confirmed_at: "2026-08-28T00:00:00Z", confirmed_by: "boss", contact: "Business contact", content_hash: "issuer-hash", field_provenance: {}, issuer_id: "issuer-1", name: "Our Company", source_ref: "issuer-confirmation" };
const quote: components["schemas"]["QuoteInternalPublicView"] = {
  account_name: "客户", basis_hash: "basis-hash", basis_id: "basis-1", calculation, content_hash: "quote-hash", cost_sheet_id: sheetId, country: "DE", created_at: "2026-08-28T00:00:00Z", issuer, lines: [{ description: "Parts", line_number: 1, line_total: { amount: "12.35", currency: "USD" }, quantity: 100, rounding: { strategy: "half_up", total_places: 2, unit_places: 4 }, specification: "M8 steel", unit: "piece", unit_price: { amount: "0.1235", currency: "USD" } }], opportunity_id: opp, owner_id: "owner", prepared_by: "employee-a", quote_id: "quote-1", replaced_quote_version: null, replaces_quote_id: null, request_hash: "request-hash", scope_confirmation_id: "scope-1", state: "draft", terms: [], valid_until: "2026-10-01T00:00:00Z", version: 1,
};
const apps: VueApp[] = [];
afterEach(() => {
  apps.splice(0).forEach((app) => app.unmount());
  clearAuthenticatedIdentity();
  vi.restoreAllMocks();
  document.body.replaceChildren();
});
function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}
function deferred(): { promise: Promise<Response>; resolve: (value: Response) => void; reject: (reason: Error) => void } {
  let resolve!: (value: Response) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<Response>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
async function eventually(assertion: () => void): Promise<void> {
  let error: unknown;
  for (let index = 0; index < 60; index += 1) {
    await nextTick();
    await new Promise((resolve) => setTimeout(resolve, 0));
    try { assertion(); return; } catch (reason) { error = reason; }
  }
  throw error;
}
async function mount(fetch: typeof globalThis.fetch, path = "/costing-quotes", provider?: WebIdentityProvider): Promise<HTMLElement> {
  configureAuthenticatedIdentity("tenant-a", "employee-a");
  await router.replace(path);
  const root = document.createElement("div");
  document.body.append(root);
  const app = createApp(App);
  apps.push(app);
  app.provide("tradeos-api-client", createApiClient({ baseUrl: "https://tradeos.test", fetch }, provider));
  app.use(router);
  app.mount(root);
  await nextTick();
  return root;
}
function field(root: HTMLElement, name: string, value: string): void {
  const input = root.querySelector<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>(`[name="${name}"]`);
  expect(input, name).not.toBeNull();
  if (!input) return;
  input.value = value;
  input.dispatchEvent(new Event(input instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
}
function click(root: HTMLElement, label: string): void {
  const button = [...root.querySelectorAll("button")].find((item) => item.textContent?.includes(label));
  expect(button, label).toBeTruthy();
  button?.click();
}
function basic(request: Request): Response {
  const path = new URL(request.url).pathname;
  if (path.endsWith("/quote-context")) return json(context);
  if (path.endsWith("/cost-sheets")) return json([sheet]);
  if (path.endsWith("/customer-quote-versions")) return json({ items: [], next_before_version: null });
  if (path.endsWith("/unit")) return json({ account_id: "account", need_id: need, quantity: 100, quantity_fact_hash: "quantity-hash", quantity_origin: provenance, quantity_status: "current", unit: null, unit_confirmation_id: null, unit_origin: null, unit_status: "missing" });
  if (path.endsWith("/policies")) return json({ code: "record_not_found", message: "未配置" }, 404);
  if (path.endsWith("/issuer") || path.endsWith("/coverage")) return json(null);
  return json([]);
}

function unitPreparation(hash: string, needId = need): components["schemas"]["NeedUnitPreparationView"] {
  return { account_id: "account", need_id: needId, quantity: 100, quantity_fact_hash: hash, quantity_origin: provenance, quantity_status: "current", unit: null, unit_confirmation_id: null, unit_origin: null, unit_status: "missing" };
}
async function mountUnit(fetch: typeof globalThis.fetch): Promise<{ root: HTMLElement; currentNeed: Ref<string> }> {
  configureAuthenticatedIdentity("tenant-a", "employee-a");
  const currentNeed = ref(need);
  const root = document.createElement("div"); document.body.append(root);
  const app = createApp({ setup: () => () => h(NeedUnitConfirmationForm, { needId: currentNeed.value }) });
  apps.push(app);
  app.provide("tradeos-api-client", createApiClient({ baseUrl: "https://tradeos.test", fetch }));
  app.mount(root); await nextTick();
  return { root, currentNeed };
}

it("keeps unit preparation alive through editing and locating, then confirms the actual returned hash", async () => {
  const initial = deferred(); const nextNeed = deferred(); const reads: Request[] = []; const bodies: unknown[] = [];
  const preview = { artifact_id: "artifact", page: null, profile: "rfc822-plain-v1", raw_hash: "raw", scope: { purpose: "need_unit", action: "confirm", need_id: need }, source_ref: `message:${messageId}`, text: "100 pieces", text_hash: "text" };
  const { root, currentNeed } = await mountUnit(async (input) => {
    if (!(input instanceof Request)) throw new Error(); const path = new URL(input.url).pathname;
    if (path.endsWith("/unit")) { reads.push(input); return reads.length === 1 ? initial.promise : nextNeed.promise; }
    if (path.endsWith("/evidence/preview")) return json(preview);
    if (path.endsWith("/evidence/locator")) return json({ ...preview, start: 0, end: 10, excerpt: "100 pieces", excerpt_hash: "excerpt", locator: "pending-read-locator" });
    if (path.endsWith("/unit-confirmations")) { bodies.push(await input.json()); return json({ artifact_id: "artifact", confirmation_id: "pending-read-confirmed", confirmed_at: "2026-08-28T00:00:00Z", confirmed_by: "employee-a", content_hash: "unit-hash", need_id: need, observed_at: "2026-08-28T00:00:00Z", quantity_fact_hash: "actual-read-hash", source_message_id: messageId, unit: "pieces", unit_origin: provenance }); }
    return basic(input);
  });
  await eventually(() => expect(reads).toHaveLength(1));
  field(root, "unit-source", messageId); field(root, "unit-value", "pieces"); await nextTick();
  click(root, "预览客户消息"); await eventually(() => expect(root.querySelector('[name="unit-preview"]')).not.toBeNull());
  const area = root.querySelector<HTMLTextAreaElement>('[name="unit-preview"]')!;
  area.setSelectionRange(0, 10); area.dispatchEvent(new Event("select")); await nextTick();
  click(root, "定位客户单位原话"); await eventually(() => expect(root.textContent).toContain("pending-read-locator"));
  expect(reads[0]?.signal.aborted).toBe(false);
  initial.resolve(json(unitPreparation("actual-read-hash")));
  await eventually(() => expect(root.textContent).toContain("actual-read-hash"));
  click(root, "确认客户单位"); await eventually(() => expect(bodies).toHaveLength(1));
  expect(bodies[0]).toMatchObject({ expected_quantity_fact_hash: "actual-read-hash", expected_unit_confirmation_id: null, source_message_id: messageId, locator: "pending-read-locator" });
  await eventually(() => expect(root.textContent).toContain("pending-read-confirmed"));
  currentNeed.value = "need-other"; await nextTick();
  expect(root.textContent).not.toContain("pending-read-confirmed");
  expect(root.textContent).not.toContain("actual-read-hash");
});

it.each(["need", "identity", "newer-read"] as const)("discards late unit preparation after %s for both success and failure", async (change) => {
  for (const outcome of ["success", "failure"] as const) {
    const old = deferred(); const fresh = deferred(); const reads: Request[] = [];
    const { root, currentNeed } = await mountUnit(async (input) => {
      if (!(input instanceof Request)) throw new Error();
      if (new URL(input.url).pathname.endsWith("/unit")) { reads.push(input); return reads.length === 1 ? old.promise : fresh.promise; }
      return basic(input);
    });
    await eventually(() => expect(reads).toHaveLength(1));
    if (change === "need") { currentNeed.value = "need-new"; await nextTick(); }
    else {
      if (change === "identity") { configureAuthenticatedIdentity("tenant-b", "employee-b"); configureAuthenticatedIdentity("tenant-a", "employee-a"); await nextTick(); }
      click(root, "核对已保存客户单位");
    }
    await eventually(() => expect(reads).toHaveLength(2)); expect(reads[0]?.signal.aborted).toBe(true);
    fresh.resolve(json(unitPreparation("fresh-unit-hash", currentNeed.value)));
    await eventually(() => expect(root.textContent).toContain("fresh-unit-hash"));
    if (outcome === "success") old.resolve(json(unitPreparation("stale-unit-hash")));
    else old.reject(new Error("stale-unit-error"));
    await nextTick(); await new Promise((resolve) => setTimeout(resolve, 0));
    expect(root.textContent).toContain("fresh-unit-hash"); expect(root.textContent).not.toContain("stale-unit");
    expect(root.textContent).not.toContain("客户单位核对失败");
    apps.pop()?.unmount(); root.remove();
  }
});

it("clears old unit preparation synchronously on Need change before the new read resolves", async () => {
  const fresh = deferred(); let calls = 0;
  const { root, currentNeed } = await mountUnit(async (input) => {
    if (!(input instanceof Request)) throw new Error();
    if (new URL(input.url).pathname.endsWith("/unit")) { calls += 1; return calls === 1 ? json(unitPreparation("old-need-hash")) : fresh.promise; }
    return basic(input);
  });
  await eventually(() => expect(root.textContent).toContain("old-need-hash"));
  currentNeed.value = "need-new"; await nextTick();
  expect(root.textContent).not.toContain("old-need-hash");
  fresh.resolve(json(unitPreparation("new-need-hash", "need-new")));
  await eventually(() => expect(root.textContent).toContain("new-need-hash"));
});

it("loads the exact notification quote and independent authorized PDF despite internal 403", async () => {
  const paths: string[] = [];
  const root = await mount(async (input) => {
    if (!(input instanceof Request)) throw new Error();
    const path = new URL(input.url).pathname; paths.push(path);
    if (path.endsWith("/quotes/quote-1")) return json({ code: "permission_denied", message: "拒绝" }, 403);
    if (path.endsWith("/quotes/quote-1/files")) return json([file]);
    if (path.endsWith("/files/file-1")) return new Response("pdf", { headers: { "content-type": "application/pdf" } });
    return basic(input);
  }, "/costing-quotes/quotes/quote-1");
  await eventually(() => expect(root.textContent).toContain("file-1"));
  expect(paths).toContain("/costing-quotes/quotes/quote-1");
  expect(paths.some((path) => path.includes("/opportunities/"))).toBe(false);
  const createUrl = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:authorized-file");
  const revoke = vi.spyOn(URL, "revokeObjectURL");
  click(root, "下载当前文件");
  await eventually(() => expect(createUrl).toHaveBeenCalledTimes(1));
  configureAuthenticatedIdentity("tenant-b", "employee-b");
  await nextTick();
  expect(root.textContent).not.toContain("file-1");
  expect(revoke).toHaveBeenCalledWith("blob:authorized-file");
});

it.each(["success", "error"])("discards late %s preview after A-B-A identity changes", async (outcome) => {
  const pending = deferred();
  let started = false;
  const root = await mount(async (input) => {
    if (!(input instanceof Request)) throw new Error();
    if (new URL(input.url).pathname.endsWith("/evidence/preview")) { started = true; return pending.promise; }
    return basic(input);
  });
  field(root, "opportunity-id", opp); click(root, "读取成本版本");
  await eventually(() => expect(root.querySelector('[name="price-source"]')).not.toBeNull());
  field(root, "price-source", "source-a"); field(root, "price-page", "1");
  await nextTick();
  click(root, "预览价格原文");
  await eventually(() => expect(started).toBe(true));
  configureAuthenticatedIdentity("tenant-b", "employee-b");
  configureAuthenticatedIdentity("tenant-a", "employee-a");
  if (outcome === "success") pending.resolve(json({ artifact_id: "artifact", page: 1, profile: "pdf-text-v1", raw_hash: "raw-hash", scope: { purpose: "pricing" }, source_ref: "source-a", text: "旧员工秘密原文", text_hash: "text-hash" }));
  else pending.reject(new Error("旧员工错误"));
  await nextTick(); await new Promise((resolve) => setTimeout(resolve, 0));
  expect(root.textContent).not.toContain("旧员工");
  expect(root.textContent).not.toContain("原文请求失败");
  expect(root.querySelector('[name="price-source"]')).toBeNull();
});

it("does not download a PDF arriving after identity changes or unmount", async () => {
  const pending = deferred(); let started = false;
  const root = await mount(async (input) => {
    if (!(input instanceof Request)) throw new Error();
    const path = new URL(input.url).pathname;
    if (path.endsWith("/files")) return json([file]);
    if (path.endsWith("/files/file-1")) { started = true; return pending.promise; }
    return json({ code: "permission_denied", message: "拒绝" }, 403);
  }, "/costing-quotes/quotes/quote-1");
  await eventually(() => expect(root.textContent).toContain("file-1"));
  const createUrl = vi.spyOn(URL, "createObjectURL");
  click(root, "下载当前文件");
  await eventually(() => expect(started).toBe(true));
  configureAuthenticatedIdentity("tenant-b", "employee-b");
  apps.pop()?.unmount();
  pending.resolve(new Response("pdf", { headers: { "content-type": "application/pdf" } }));
  await nextTick(); await new Promise((resolve) => setTimeout(resolve, 0));
  expect(createUrl).not.toHaveBeenCalled();
});

it("keeps policy intent key after an unknown result, checks GET, and requires explicit new confirmation", async () => {
  const keys: string[] = []; const bodies: unknown[] = []; let reads = 0;
  const root = await mount(async (input) => {
    if (!(input instanceof Request)) throw new Error();
    if (new URL(input.url).pathname.endsWith("/policies")) {
      if (input.method === "POST") { keys.push(input.headers.get("Idempotency-Key") ?? ""); bodies.push(await input.json()); throw new Error("uncertain"); }
      reads += 1;
    }
    return basic(input);
  });
  field(root, "policy-target", "0.123456789012"); field(root, "policy-minimum", "0.01"); field(root, "policy-source", "boss-note"); field(root, "policy-effective", "2026-08-28T00:00:00Z");
  const groups = root.querySelectorAll<HTMLSelectElement>('[name^="policy-group-"]');
  expect(groups).toHaveLength(22);
  for (const select of groups) field(root, select.name, "goods");
  click(root, "老板确认政策");
  await eventually(() => expect(root.textContent).toContain("待核对"));
  expect(keys[0]).toBeTruthy();
  expect(bodies[0]).toMatchObject({ target_margin_rate: "0.123456789012", minimum_margin_rate: "0.01" });
  click(root, "核对已保存政策");
  await eventually(() => expect(reads).toBeGreaterThan(0));
  click(root, "老板确认政策");
  await eventually(() => expect(keys).toHaveLength(2));
  expect(keys[1]).toBe(keys[0]);
  field(root, "policy-target", "0.2"); await nextTick();
  expect(keys).toHaveLength(2);
  click(root, "老板确认政策");
  await eventually(() => expect(keys).toHaveLength(3));
  expect(keys[2]).not.toBe(keys[0]);
});

it("marks an edited in-flight confirmation as a new intent and never applies its old receipt", async () => {
  const pending = deferred(); let started = false;
  const root = await mount(async (input) => {
    if (!(input instanceof Request)) throw new Error();
    if (new URL(input.url).pathname.endsWith("/issuer") && input.method === "POST") { started = true; return pending.promise; }
    return basic(input);
  });
  field(root, "issuer-name", "Original company"); field(root, "issuer-address", "Original address"); field(root, "issuer-contact", "Original contact");
  click(root, "老板确认抬头"); await eventually(() => expect(started).toBe(true));
  field(root, "issuer-name", "Changed company"); await nextTick();
  expect(root.textContent).toContain("新意图");
  pending.resolve(json(issuer)); await nextTick(); await new Promise((resolve) => setTimeout(resolve, 0));
  expect(root.textContent).not.toContain("issuer-1");
  expect(root.textContent).toContain("待核对");
});

it("uses backend preview hashes and codepoint coordinates, then discards locator on page changes", async () => {
  const located: unknown[] = []; const prices: unknown[] = [];
  const preview = { artifact_id: "artifact", page: 1, profile: "pdf-text-v1", raw_hash: "raw-hash", scope: { purpose: "pricing" }, source_ref: "source-a", text: "A😀B", text_hash: "text-hash" };
  const root = await mount(async (input) => {
    if (!(input instanceof Request)) throw new Error();
    const path = new URL(input.url).pathname;
    if (path.endsWith("/evidence/preview")) return json(preview);
    if (path.endsWith("/evidence/locator")) { located.push(await input.json()); return json({ ...preview, start: 1, end: 2, excerpt: "😀", excerpt_hash: "excerpt-hash", locator: "server-locator" }); }
    if (path === "/costing-quotes/price-evidence") { prices.push(await input.json()); return json({ code: "invalid_input", message: "受控拒绝" }, 400); }
    return basic(input);
  });
  field(root, "opportunity-id", opp); click(root, "读取成本版本");
  await eventually(() => expect(root.querySelector('[name="price-source"]')).not.toBeNull());
  field(root, "price-source", "source-a"); field(root, "price-page", "1"); await nextTick(); click(root, "预览价格原文");
  await eventually(() => expect(root.querySelector<HTMLTextAreaElement>('[name="price-preview"]')?.value).toBe("A😀B"));
  const textarea = root.querySelector<HTMLTextAreaElement>('[name="price-preview"]')!;
  textarea.setSelectionRange(1, 3); textarea.dispatchEvent(new Event("select")); await nextTick(); click(root, "定位价格选区");
  await eventually(() => expect(root.textContent).toContain("server-locator"));
  expect(located).toEqual([{ operation: "locate", source_ref: "source-a", profile: "pdf-text-v1", page: 1, scope: { purpose: "pricing" }, expected_raw_hash: "raw-hash", expected_text_hash: "text-hash", start: 1, end: 2 }]);
  field(root, "price-amount", "0.123456789012"); field(root, "price-currency", "USD");
  field(root, "price-unit", "piece"); field(root, "price-moq", "10"); field(root, "price-min", "100"); field(root, "price-max", "200");
  field(root, "price-supplier", "supplier-1"); field(root, "price-spec", "M8 steel"); field(root, "price-destination", "Hamburg"); field(root, "price-observed", "2026-08-28T00:00:00Z"); field(root, "price-valid", "2026-10-01T00:00:00Z");
  click(root, "确认价格依据"); await eventually(() => expect(prices).toHaveLength(1));
  expect(prices[0]).toMatchObject({ amount: "0.123456789012", locator: "server-locator", quantity_min: 100, unit: "piece" });
  field(root, "price-page", "2"); await nextTick();
  expect(root.textContent).not.toContain("server-locator");
  const submit = [...root.querySelectorAll("button")].find((item) => item.textContent?.includes("确认价格依据"));
  expect(submit?.disabled).toBe(true);
});

it.each(["price", "unit"] as const)("does not abort a pending %s locator for repeated native events of the same selection", async (kind) => {
  const pending = deferred(); let locatorRequest: Request | null = null;
  const preview = { artifact_id: "artifact", page: kind === "price" ? 1 : null, profile: kind === "price" ? "pdf-text-v1" : "rfc822-plain-v1", raw_hash: "raw-hash", scope: kind === "price" ? { purpose: "pricing" } : { purpose: "need_unit", action: "confirm", need_id: need }, source_ref: kind === "price" ? "source-a" : `message:${messageId}`, text: "100 pieces", text_hash: "text-hash" };
  const root = await mount(async (input) => {
    if (!(input instanceof Request)) throw new Error();
    const path = new URL(input.url).pathname;
    if (path.endsWith("/evidence/preview")) return json(preview);
    if (path.endsWith("/evidence/locator")) { locatorRequest = input; return pending.promise; }
    return basic(input);
  });
  field(root, "opportunity-id", opp); click(root, "读取成本版本");
  await eventually(() => expect(root.querySelector(`[name="${kind}-source"]`)).not.toBeNull());
  field(root, `${kind}-source`, kind === "price" ? "source-a" : messageId);
  if (kind === "price") field(root, "price-page", "1");
  await nextTick(); click(root, kind === "price" ? "预览价格原文" : "预览客户消息");
  await eventually(() => expect(root.querySelector(`[name="${kind}-preview"]`)).not.toBeNull());
  const area = root.querySelector<HTMLTextAreaElement>(`[name="${kind}-preview"]`)!;
  area.setSelectionRange(0, 3); area.dispatchEvent(new Event("select")); await nextTick();
  click(root, kind === "price" ? "定位价格选区" : "定位客户单位原话");
  await eventually(() => expect(locatorRequest).not.toBeNull());
  for (const event of ["select", "keyup", "mouseup"]) area.dispatchEvent(new Event(event));
  await nextTick();
  expect(locatorRequest!.signal.aborted).toBe(false);
  pending.resolve(json({ ...preview, start: 0, end: 3, excerpt: "100", excerpt_hash: "excerpt", locator: "repeat-locator" }));
  await eventually(() => expect(root.textContent).toContain("repeat-locator"));
  for (const event of ["select", "keyup", "mouseup"]) area.dispatchEvent(new Event(event));
  await nextTick();
  expect(root.textContent).toContain("repeat-locator");
  area.setSelectionRange(0, 4); area.dispatchEvent(new Event("select")); await nextTick();
  expect(root.textContent).not.toContain("repeat-locator");
});

it.each([
  ["price", "selection"], ["price", "source"], ["price", "identity"],
  ["unit", "selection"], ["unit", "source"], ["unit", "identity"],
] as const)("still discards a late %s locator after a real %s change", async (kind, change) => {
  const pending = deferred(); let request: Request | null = null;
  const preview = { artifact_id: "artifact", page: kind === "price" ? 1 : null, profile: kind === "price" ? "pdf-text-v1" : "rfc822-plain-v1", raw_hash: "raw", scope: kind === "price" ? { purpose: "pricing" } : { purpose: "need_unit", action: "confirm", need_id: need }, source_ref: kind === "price" ? "source-a" : `message:${messageId}`, text: "100 pieces", text_hash: "text" };
  const root = await mount(async (input) => {
    if (!(input instanceof Request)) throw new Error();
    const path = new URL(input.url).pathname;
    if (path.endsWith("/evidence/preview")) return json(preview);
    if (path.endsWith("/evidence/locator")) { request = input; return pending.promise; }
    return basic(input);
  });
  field(root, "opportunity-id", opp); click(root, "读取成本版本");
  await eventually(() => expect(root.querySelector(`[name="${kind}-source"]`)).not.toBeNull());
  field(root, `${kind}-source`, kind === "price" ? "source-a" : messageId); if (kind === "price") field(root, "price-page", "1");
  await nextTick(); click(root, kind === "price" ? "预览价格原文" : "预览客户消息");
  await eventually(() => expect(root.querySelector(`[name="${kind}-preview"]`)).not.toBeNull());
  const area = root.querySelector<HTMLTextAreaElement>(`[name="${kind}-preview"]`)!;
  area.setSelectionRange(0, 3); area.dispatchEvent(new Event("select")); await nextTick();
  click(root, kind === "price" ? "定位价格选区" : "定位客户单位原话");
  await eventually(() => expect(request).not.toBeNull());
  if (change === "selection") { area.setSelectionRange(0, 4); area.dispatchEvent(new Event("select")); }
  else if (change === "source") field(root, `${kind}-source`, "source-b");
  else configureAuthenticatedIdentity("tenant-a", "employee-b");
  await nextTick(); expect(request!.signal.aborted).toBe(true);
  pending.resolve(json({ ...preview, start: 0, end: 3, excerpt: "100", excerpt_hash: "excerpt", locator: "late-locator-must-not-show" }));
  await nextTick(); await new Promise((resolve) => setTimeout(resolve, 0));
  expect(root.textContent).not.toContain("late-locator-must-not-show");
});

it("shows the full scope and requires explicit coverage classification instead of treating zero as missing", async () => {
  const root = await mount(async (input) => {
    if (!(input instanceof Request)) throw new Error(); return basic(input);
  });
  field(root, "opportunity-id", opp); click(root, "读取成本版本");
  await eventually(() => expect(root.querySelector('[name="coverage-product_purchase"]')).not.toBeNull());
  expect(root.textContent).toContain("0 USD");
  expect(root.textContent).toContain("Hamburg");
  expect(root.textContent).toContain("2026-10-01");
  expect(root.textContent).toContain("客户单位确认");
  expect(root.textContent).toContain("成本适用性确认");
  expect(root.querySelectorAll('[name^="coverage-"]:not([name="coverage-mode"])')).toHaveLength(22);
});

const approval: components["schemas"]["ApprovalView"] = {
  approval_id: "approval-quote", approval_type: "quote", affected_entities: ["quote-1"], can_current_user_decide: true,
  created_at: "2026-08-28T00:00:00Z", expires_at: "2026-09-01T00:00:00Z", if_approved: "批准不发送", if_rejected: "退回", proposed_by: "drafter", owner_name: "owner", proposed_change_display: { "客户单价": "0.123456789012 USD", "低于底线例外": "须独立审批", "前版成本": "cost-old", "汇率口径": "USD → EUR · 0.923456789012" }, reason: "人工确认的报价", reversible: false, state: "pending", title: "报价审批敏感详情", type_label: "正式报价",
};
const run: components["schemas"]["RunSummaryView"] = { created_at: "2026-08-28T00:00:00Z", current_step: "approval", last_activity_at: "2026-08-28T00:00:00Z", last_error: null, next_poll_at: null, retry_count: 0, run_id: "run-quote", status: "waiting_human", subject_ref: "quote-1", workflow_type: "quote_approval", workflow_version: 1 };
const runDetail: components["schemas"]["RunDetailView"] = { summary: run, approvals: [{ approval_id: "approval-quote", approval_type: "quote", created_at: run.created_at, decided_at: null, expires_at: "2026-09-01T00:00:00Z", state: "pending" }], artifacts: [], steps: [], tool_calls: [] };

it.each(["success", "http-error", "network-error"])("freezes the rejection reason and releases decision controls after %s", async (outcome) => {
  const pending = deferred(); const bodies: unknown[] = [];
  const root = await mount(async (input) => {
    if (!(input instanceof Request)) throw new Error(); const path = new URL(input.url).pathname;
    if (path === "/approvals/pending") return json([approval]);
    if (path === "/approvals/approval-quote") return json(approval);
    if (path.endsWith("/decide")) { bodies.push(await input.json()); return pending.promise; }
    return json([]);
  }, "/approvals");
  await eventually(() => expect(root.querySelector(".decision-panel textarea")).not.toBeNull());
  const reason = root.querySelector<HTMLTextAreaElement>(".decision-panel textarea")!;
  reason.value = "需要更正付款条件"; reason.dispatchEvent(new Event("input", { bubbles: true }));
  click(root, "否决并退回"); await eventually(() => expect(bodies).toHaveLength(1));
  const couldEditWhilePending = !reason.disabled;
  if (couldEditWhilePending) { reason.value = "修改后的原因"; reason.dispatchEvent(new Event("input", { bubbles: true })); }
  if (outcome === "network-error") pending.reject(new Error("controlled interruption"));
  else pending.resolve(outcome === "success" ? json(null) : json({ code: "dependency_unavailable", message: "受控故障" }, 503));
  await eventually(() => expect(root.querySelector<HTMLButtonElement>(".reject-button")?.disabled).toBe(false));
  expect(couldEditWhilePending).toBe(false);
  expect(root.querySelector<HTMLTextAreaElement>(".decision-panel textarea")?.disabled).toBe(false);
  expect(root.textContent).not.toContain("提交中");
  expect(bodies).toEqual([{ decision: "reject", reason: "需要更正付款条件" }]);
  if (outcome === "http-error") expect(root.textContent).toContain("审批服务暂不可用");
  if (outcome === "network-error") expect(root.textContent).toContain("决定结果未知");
});

it.each(["selection", "identity"])("keeps a newer approval decision busy after an old response and %s changes", async (change) => {
  const old = deferred(); const fresh = deferred(); let calls = 0;
  const second = { ...approval, approval_id: "approval-second", title: "第二个精确审批" };
  const root = await mount(async (input) => {
    if (!(input instanceof Request)) throw new Error(); const path = new URL(input.url).pathname;
    if (path === "/approvals/pending") return json([approval, second]);
    if (path === "/approvals/approval-quote") return json(approval);
    if (path === "/approvals/approval-second") return json(second);
    if (path.endsWith("/decide")) { calls += 1; return calls === 1 ? old.promise : fresh.promise; }
    return json([]);
  }, "/approvals");
  await eventually(() => expect(root.querySelector(".decision-panel")).not.toBeNull());
  click(root, "批准此精确变更"); await eventually(() => expect(calls).toBe(1));
  if (change === "identity") { configureAuthenticatedIdentity("tenant-b", "employee-b"); await nextTick(); click(root, "刷新待办"); }
  else click(root, "第二个精确审批");
  await eventually(() => expect(root.querySelector<HTMLButtonElement>(".reject-button")?.disabled).toBe(false));
  click(root, "批准此精确变更"); await eventually(() => expect(calls).toBe(2));
  old.resolve(json({ code: "state_conflict", message: "旧审批冲突" }, 409));
  await nextTick(); await new Promise((resolve) => setTimeout(resolve, 0));
  expect(root.querySelector<HTMLButtonElement>(".reject-button")?.disabled).toBe(true);
  expect(root.textContent).not.toContain("审批已被他人决定");
  fresh.resolve(json({ code: "dependency_unavailable", message: "当前故障" }, 503));
  await eventually(() => expect(root.textContent).toContain("审批服务暂不可用"));
  expect(root.querySelector<HTMLButtonElement>(".reject-button")?.disabled).toBe(false);
});

it.each(["list", "detail", "action"])("clears quote approval data and rejects late %s responses after identity switch", async (stage) => {
  const pending = deferred(); let started = false; let lists = 0;
  const root = await mount(async (input) => {
    if (!(input instanceof Request)) throw new Error();
    const path = new URL(input.url).pathname;
    if (path === "/approvals/pending") { lists += 1; if (stage === "list") { started = true; return pending.promise; } return json([approval]); }
    if (path === "/approvals/approval-quote") { if (stage === "detail") { started = true; return pending.promise; } return json(approval); }
    if (path.endsWith("/decide")) { started = true; return pending.promise; }
    return json([]);
  }, "/approvals");
  if (stage === "action") {
    await eventually(() => expect(root.textContent).toContain("0.123456789012 USD"));
    expect(root.textContent).toContain("cost-old"); expect(root.textContent).toContain("批准不发送");
    click(root, "批准此精确变更");
  }
  await eventually(() => expect(started).toBe(true));
  configureAuthenticatedIdentity("tenant-b", "employee-b");
  await nextTick();
  expect(root.textContent).not.toContain("报价审批敏感详情");
  pending.resolve(stage === "list" ? json([approval]) : stage === "detail" ? json(approval) : json({ code: "conflict", message: "old-error" }, 409));
  await nextTick(); await new Promise((resolve) => setTimeout(resolve, 0));
  expect(root.textContent).not.toContain("报价审批敏感详情");
  expect(root.textContent).not.toContain("审批已被他人决定");
  expect(lists).toBe(1);
});

it.each(["list", "detail"])("clears quote Run safe summaries and rejects late %s after A-B-A", async (stage) => {
  const pending = deferred(); let started = false;
  const root = await mount(async (input) => {
    if (!(input instanceof Request)) throw new Error();
    const path = new URL(input.url).pathname;
    if (path === "/runs") { if (stage === "list") { started = true; return pending.promise; } return json([run]); }
    if (path === "/runs/run-quote") { started = true; return pending.promise; }
    return json([]);
  }, "/runs");
  await eventually(() => expect(started).toBe(true));
  configureAuthenticatedIdentity("tenant-b", "employee-b"); configureAuthenticatedIdentity("tenant-a", "employee-a");
  await nextTick(); expect(root.textContent).not.toContain("quote-1");
  pending.resolve(stage === "list" ? json([run]) : json(runDetail));
  await nextTick(); await new Promise((resolve) => setTimeout(resolve, 0));
  expect(root.textContent).not.toContain("quote-1"); expect(root.textContent).not.toContain("approval-quote");
});

it("confirms persisted cost bindings and full scope before precise quote creation and submission", async () => {
  const requests: { path: string; body: unknown; key: string | null }[] = [];
  const root = await mount(async (input) => {
    if (!(input instanceof Request)) throw new Error(); const path = new URL(input.url).pathname;
    if (input.method === "POST") {
      const body: unknown = await input.json(); requests.push({ path, body, key: input.headers.get("Idempotency-Key") });
      if (path.endsWith("/coverage")) return json({ acquisition_mode: "summary", confirmed_at: "2026-08-28T00:00:00Z", confirmed_by: "employee-a", content_hash: "coverage-hash", cost_sheet_id: sheetId, coverage_id: "coverage-1", decisions: [{ applicable: true, item_bindings: [{ allocation_scope: "this-order", evidence_id: "evidence-1", item_sequence: 7, source_line_ref: "line-1" }], item_type: "product_purchase", reason: "适用" }], expected_sheet_hash: "sheet-hash", field_provenance: {} });
      if (path.endsWith("/scope-confirmations")) return json({ confirmation_id: "scope-1", content_hash: "scope-hash", cost_sheet_id: sheetId, coverage_hash: "coverage-hash", coverage_id: "coverage-1", evidence_bindings: [{ applicability_note: "该供应商规格和数量档适用于本订单", evidence_hash: "evidence-hash", evidence_id: "evidence-1" }], need_facts_hash: "need-hash", need_id: need, opportunity_id: opp, provenance, sheet_hash: "sheet-hash", specification: "M8 steel", specification_hash: "spec-hash", terms: [], terms_hash: "terms-hash", valid_until: "2026-10-01T00:00:00Z" });
      if (path.endsWith("/quotes")) return json(quote);
      if (path.endsWith("/submit")) return json({ quote_id: "quote-1", run_id: "run-quote" }, 202);
      if (path.endsWith("/calculate")) return json(calculation);
    }
    if (path.endsWith("/price-evidence")) return json([supplierEvidence]);
    if (path.endsWith("/quotes/quote-1")) return json(quote);
    return basic(input);
  });
  field(root, "opportunity-id", opp); click(root, "读取成本版本");
  await eventually(() => expect(root.querySelector('[name="coverage-mode"]')).not.toBeNull());
  field(root, "coverage-mode", "summary");
  for (const select of root.querySelectorAll<HTMLSelectElement>('[name^="coverage-"]:not([name="coverage-mode"])')) { field(root, select.name, select.name === "coverage-product_purchase" ? "yes" : "no"); field(root, select.name.replace("coverage-", "reason-"), "人工核对"); }
  field(root, "binding-evidence-7", "evidence-1"); field(root, "binding-line-7", "line-1"); field(root, "binding-allocation-7", "this-order"); click(root, "确认成本适用清单");
  await eventually(() => expect(root.querySelector('[name="scope-note-evidence-1"]')).not.toBeNull());
  const coverageBody = requests.find((item) => item.path.endsWith("/coverage"))!;
  expect(coverageBody.key).toBeTruthy();
  expect(coverageBody.body).toMatchObject({ expected_sheet_hash: "sheet-hash", decisions: expect.arrayContaining([{ item_type: "product_purchase", applicable: true, reason: "人工核对", item_bindings: [{ item_sequence: 7, evidence_id: "evidence-1", source_line_ref: "line-1", allocation_scope: "this-order" }] }]) });
  field(root, "quote-price", "0.123456789012"); field(root, "quote-currency-exact", "USD"); field(root, "quote-unit-places", "4"); field(root, "quote-total-places", "2"); field(root, "quote-rounding", "half_up"); field(root, "quote-valid-until", "2026-10-01T00:00:00Z");
  field(root, "scope-note-evidence-1", "该供应商规格和数量档适用于本订单"); await nextTick(); click(root, "确认成本适用性");
  await eventually(() => expect(root.textContent).toContain("scope-1"));
  expect(requests.find((item) => item.path.endsWith("/scope-confirmations"))?.body).toEqual({ coverage_id: "coverage-1", expected_coverage_hash: "coverage-hash", expected_need_facts_hash: "need-hash", expected_sheet_hash: "sheet-hash", evidence_bindings: [{ evidence_id: "evidence-1", evidence_hash: "evidence-hash", applicability_note: "该供应商规格和数量档适用于本订单" }], terms: [], valid_until: "2026-10-01T00:00:00Z" });
  click(root, "计算目标报价"); click(root, "计算实际报价收益");
  await eventually(() => expect(requests.filter((item) => item.path.endsWith("/calculate"))).toHaveLength(2));
  expect(requests.filter((item) => item.path.endsWith("/calculate"))[1]?.body).toMatchObject({ unit_price: { amount: "0.123456789012", currency: "USD" }, quote_fx_ref: null });
  field(root, "term-payment", "Payment in advance"); await nextTick();
  const createButton = [...root.querySelectorAll("button")].find((item) => item.textContent?.includes("确认创建新报价"));
  expect(createButton?.disabled).toBe(true);
  expect(root.textContent).toContain("旧确认失效");
  expect(root.textContent).not.toContain("输入 hash inputs-hash");
  field(root, "term-payment", ""); await nextTick();
  click(root, "确认创建新报价");
  await eventually(() => expect(router.currentRoute.value.params.quoteId).toBe("quote-1"));
  expect(requests.find((item) => item.path.endsWith("/quotes"))?.body).toMatchObject({ expected_sheet_hash: "sheet-hash", expected_context_hash: "context-hash", scope_confirmation_id: "scope-1", unit_price: { amount: "0.123456789012", currency: "USD" }, replaces_quote_id: null, expected_quote_version: null });
  await eventually(() => expect(root.textContent).toContain("指定版本 V1"));
  click(root, "提交此版本审批"); await eventually(() => expect(root.textContent).toContain("Run run-quote"));
  expect(root.textContent).toContain("尚未批准、更未发送");
});

it.each(["target", "manual"] as const)("invalidates late %s calculations when the same sheet gets a new content hash", async (mode) => {
  const success = deferred(); const failure = deferred(); const fresh = deferred(); const contextRefresh = deferred();
  let activeSheet = sheet; let calls = 0; let contextReads = 0;
  const root = await mount(async (input) => {
    if (!(input instanceof Request)) throw new Error();
    const path = new URL(input.url).pathname;
    if (path.endsWith("/quote-context")) { contextReads += 1; return contextReads === 1 ? json(context) : contextRefresh.promise; }
    if (path.endsWith("/cost-sheets")) return json([activeSheet]);
    if (path.endsWith("/calculate")) { calls += 1; return [success, failure, fresh][calls - 1]!.promise; }
    return basic(input);
  });
  field(root, "opportunity-id", opp); click(root, "读取成本版本");
  await eventually(() => expect(root.querySelector('[name="quote-price"]')).not.toBeNull());
  field(root, "quote-price", "0.123456789012"); field(root, "quote-currency-exact", "USD");
  field(root, "quote-unit-places", "4"); field(root, "quote-total-places", "2"); field(root, "quote-rounding", "half_up");
  const action = mode === "target" ? "计算目标报价" : "计算实际报价收益";
  click(root, action); await eventually(() => expect(calls).toBe(1));
  activeSheet = { ...sheet, content_hash: "sheet-new-1", items: [{ ...sheet.items[0]!, source_ref: "sheet-new-1" }] }; click(root, "读取成本版本");
  await eventually(() => expect(root.textContent).toContain("sheet-new-1"));
  success.resolve(json({ ...calculation, inputs_hash: "old-calculation" }));
  await nextTick(); await new Promise((resolve) => setTimeout(resolve, 0));
  expect(root.textContent).not.toContain("old-calculation");
  click(root, action); await eventually(() => expect(calls).toBe(2));
  activeSheet = { ...sheet, content_hash: "sheet-new-2", items: [{ ...sheet.items[0]!, source_ref: "sheet-new-2" }] }; click(root, "读取成本版本");
  await eventually(() => expect(root.textContent).toContain("sheet-new-2"));
  failure.resolve(json({ code: "dependency_unavailable", message: "old-calculation-error" }, 503));
  await nextTick(); await new Promise((resolve) => setTimeout(resolve, 0));
  expect(root.textContent).not.toContain("old-calculation-error");
  click(root, action); await eventually(() => expect(calls).toBe(3));
  fresh.resolve(json({ ...calculation, inputs_hash: "fresh-calculation" }));
  await eventually(() => expect(root.textContent).toContain("fresh-calculation"));
});

it.each(["create", "revision"] as const)("invalidates pending quote %s on a same-ID sheet change without clearing a newer operation", async (kind) => {
  const old = deferred(); const fresh = deferred(); const contextRefresh = deferred(); let calls = 0; let activeSheet = sheet; let contextReads = 0;
  const root = await mount(async (input) => {
    if (!(input instanceof Request)) throw new Error(); const path = new URL(input.url).pathname;
    if (path.endsWith("/quote-context")) { contextReads += 1; return contextReads === 1 ? json(context) : contextRefresh.promise; }
    if (path.endsWith("/cost-sheets")) return json([activeSheet]);
    if (path.endsWith("/coverage")) {
      const value: components["schemas"]["CostCoveragePublicView"] = { acquisition_mode: "summary", confirmed_at: "2026-08-28T00:00:00Z", confirmed_by: "employee-a", content_hash: "coverage-hash", cost_sheet_id: sheetId, coverage_id: "coverage-1", decisions: [], expected_sheet_hash: activeSheet.content_hash!, field_provenance: {} };
      return json(value);
    }
    if (path.endsWith("/scope-confirmations")) {
      const value: components["schemas"]["CostScopePublicView"] = { confirmation_id: `scope-${activeSheet.content_hash}`, content_hash: "scope-hash", cost_sheet_id: sheetId, coverage_hash: "coverage-hash", coverage_id: "coverage-1", evidence_bindings: [], need_facts_hash: "need-hash", need_id: need, opportunity_id: opp, provenance, sheet_hash: activeSheet.content_hash!, specification: "M8 steel", specification_hash: "spec-hash", terms: [], terms_hash: "terms-hash", valid_until: "2026-10-01T00:00:00Z" };
      return json([value]);
    }
    if (input.method === "POST" && (path.endsWith("/quotes") || path.endsWith("/revisions"))) { calls += 1; return calls === 1 ? old.promise : fresh.promise; }
    if (path.endsWith("/quotes/quote-1")) return json(quote);
    return basic(input);
  }, kind === "revision" ? "/costing-quotes/quotes/quote-1" : "/costing-quotes");
  field(root, "opportunity-id", opp); click(root, "读取成本版本");
  await eventually(() => expect(root.querySelector('[name="quote-price"]')).not.toBeNull());
  field(root, "quote-price", "0.123456789012"); field(root, "quote-currency-exact", "USD");
  field(root, "quote-unit-places", "4"); field(root, "quote-total-places", "2"); field(root, "quote-rounding", "half_up"); field(root, "quote-valid-until", "2026-10-01T00:00:00Z");
  const selectScope = async (id: string): Promise<void> => {
    click(root, "核对已保存适用清单");
    await eventually(() => expect(root.textContent).toContain("已读取费用确认"));
    click(root, "核对已保存适用性确认");
    await eventually(() => expect(root.textContent).toContain(id));
    click(root, "使用此已保存确认"); await nextTick();
  };
  await selectScope("scope-sheet-hash");
  if (kind === "revision") {
    await eventually(() => expect(root.querySelector('[name="revision-acknowledged"]')).not.toBeNull());
    root.querySelector<HTMLInputElement>('[name="revision-acknowledged"]')!.click(); await nextTick();
  }
  const action = kind === "create" ? "确认创建新报价" : "确认修订此版本";
  const quotePanel = [...root.querySelectorAll<HTMLElement>("section")].find((item) => item.querySelector("h2")?.textContent === "确认报价版本")!;
  const button = () => [...quotePanel.querySelectorAll("button")].find((item) => item.textContent?.includes(action))!;
  click(root, action); await eventually(() => expect(calls).toBe(1)); expect(button().disabled).toBe(true);
  activeSheet = { ...sheet, content_hash: "sheet-new", items: [{ ...sheet.items[0]!, source_ref: "sheet-new" }] }; click(root, "读取成本版本");
  await eventually(() => expect(root.textContent).toContain("sheet-new"));
  expect(quotePanel.textContent).toContain("原请求结果待核对");
  expect(button().disabled).toBe(true); // 旧确认仍失效，不自动重确认。
  await selectScope("scope-sheet-new");
  expect(button().disabled).toBe(false); click(root, action); await eventually(() => expect(calls).toBe(2));
  old.resolve(kind === "create" ? json({ ...quote, quote_id: "old-quote" }) : json({ code: "state_conflict", message: "old-revision-error" }, 409));
  await nextTick(); await new Promise((resolve) => setTimeout(resolve, 0));
  expect(router.currentRoute.value.params.quoteId).toBe(kind === "revision" ? "quote-1" : undefined);
  expect(quotePanel.textContent).not.toContain("old-revision-error"); expect(quotePanel.textContent).toContain("提交中"); expect(button().disabled).toBe(true);
  fresh.resolve(json({ code: "state_conflict", message: "fresh-quote-error" }, 409));
  await eventually(() => expect(quotePanel.textContent).toContain("fresh-quote-error")); expect(button().disabled).toBe(false);
});

it("sends the selected customer quote verbatim with the current quantity CAS instead of inventing unit evidence", async () => {
  const bodies: unknown[] = []; const previews: unknown[] = [];
  const preview = { artifact_id: "artifact", page: null, profile: "rfc822-plain-v1", raw_hash: "raw", scope: { purpose: "need_unit", action: "confirm", need_id: need }, source_ref: `message:${messageId}`, text: "100 pieces", text_hash: "text" };
  const root = await mount(async (input) => {
    if (!(input instanceof Request)) throw new Error(); const path = new URL(input.url).pathname;
    if (path.endsWith("/evidence/preview")) { previews.push(await input.json()); return json(preview); }
    if (path.endsWith("/evidence/locator")) return json({ ...preview, start: 0, end: 10, excerpt: "100 pieces", excerpt_hash: "excerpt", locator: "server-unit-locator" });
    if (path.endsWith("/unit-confirmations")) { bodies.push(await input.json()); return json({ artifact_id: "artifact", confirmation_id: "unit-confirmed", confirmed_at: "2026-08-28T00:00:00Z", confirmed_by: "employee-a", content_hash: "unit-hash", need_id: need, observed_at: "2026-08-28T00:00:00Z", quantity_fact_hash: "quantity-hash", source_message_id: "message-1", unit: "piece", unit_origin: provenance }); }
    return basic(input);
  });
  field(root, "opportunity-id", opp); click(root, "读取成本版本");
  await eventually(() => expect(root.querySelector('[name="unit-source"]')).not.toBeNull());
  field(root, "unit-source", messageId); field(root, "unit-value", "piece"); await nextTick(); click(root, "预览客户消息");
  await eventually(() => expect(root.querySelector('[name="unit-preview"]')).not.toBeNull());
  const area = root.querySelector<HTMLTextAreaElement>('[name="unit-preview"]')!; area.setSelectionRange(0, 10); area.dispatchEvent(new Event("select")); await nextTick(); click(root, "定位客户单位原话");
  await eventually(() => expect(root.textContent).toContain("server-unit-locator")); click(root, "确认客户单位");
  await eventually(() => expect(root.textContent).toContain("unit-confirmed"));
  expect(previews).toEqual([expect.objectContaining({ source_ref: `message:${messageId}` })]);
  expect(bodies).toEqual([{ expected_quantity_fact_hash: "quantity-hash", expected_unit_confirmation_id: null, locator: "server-unit-locator", source_message_id: messageId, source_quote: "100 pieces", unit: "piece" }]);
});

it.each([`upload:${messageId}`, `message:${messageId}`, `message:message:${messageId}`])("rejects non-bare customer message input %s before preview", async (source) => {
  let previews = 0;
  const root = await mount(async (input) => { if (!(input instanceof Request)) throw new Error(); if (new URL(input.url).pathname.endsWith("/evidence/preview")) previews += 1; return basic(input); });
  field(root, "opportunity-id", opp); click(root, "读取成本版本");
  await eventually(() => expect(root.querySelector('[name="unit-source"]')).not.toBeNull());
  field(root, "unit-source", source); await nextTick(); click(root, "预览客户消息"); await nextTick();
  expect(previews).toBe(0); expect(root.textContent).toContain("请输入裸客户消息 ID");
});

it.each([`upload:${messageId}`, `message:message:${messageId}`, "message:msg_01M0PWRX23T9DP9ENM9PW5GFC9"])("rejects mismatched customer locator %s instead of confirming it", async (wrongSource) => {
  const preview = { artifact_id: "artifact", page: null, profile: "rfc822-plain-v1", raw_hash: "raw", scope: { purpose: "need_unit", action: "confirm", need_id: need }, source_ref: `message:${messageId}`, text: "100 pieces", text_hash: "text" };
  let confirmed = 0;
  const root = await mount(async (input) => {
    if (!(input instanceof Request)) throw new Error(); const path = new URL(input.url).pathname;
    if (path.endsWith("/evidence/preview")) return json(preview);
    if (path.endsWith("/evidence/locator")) return json({ ...preview, source_ref: wrongSource, start: 0, end: 10, excerpt: "100 pieces", excerpt_hash: "excerpt", locator: "wrong-source-locator" });
    if (path.endsWith("/unit-confirmations")) confirmed += 1;
    return basic(input);
  });
  field(root, "opportunity-id", opp); click(root, "读取成本版本");
  await eventually(() => expect(root.querySelector('[name="unit-source"]')).not.toBeNull());
  field(root, "unit-source", messageId); field(root, "unit-value", "pieces"); await nextTick(); click(root, "预览客户消息");
  await eventually(() => expect(root.querySelector('[name="unit-preview"]')).not.toBeNull());
  const area = root.querySelector<HTMLTextAreaElement>('[name="unit-preview"]')!; area.setSelectionRange(0, 10); area.dispatchEvent(new Event("select")); await nextTick(); click(root, "定位客户单位原话");
  await eventually(() => expect(root.textContent).toContain("定位结果与当前客户消息不一致"));
  click(root, "确认客户单位"); await nextTick(); expect(confirmed).toBe(0);
});

it("keeps form input for fresh objects of one identity and unregisters every page subscriber on unmount", async () => {
  const listeners = new Set<() => void>();
  const provider: WebIdentityProvider = { current: () => ({ tenantId: "tenant-a", employeeId: "employee-a", mode: "authenticated" }), generation: () => 0, subscribe: (listener) => { listeners.add(listener); return () => { listeners.delete(listener); }; } };
  createApiClient({}, provider); expect(listeners.size).toBe(0);
  const root = await mount(async (input) => { if (!(input instanceof Request)) throw new Error(); return basic(input); }, "/costing-quotes", provider);
  field(root, "policy-target", "0.123456789012"); click(root, "核对已保存政策");
  await eventually(() => expect(root.textContent).toContain("record_not_found"));
  expect(root.querySelector<HTMLInputElement>('[name="policy-target"]')?.value).toBe("0.123456789012");
  expect(listeners.size).toBeGreaterThan(0);
  apps.pop()?.unmount(); expect(listeners.size).toBe(0);
});

it("ignores the old quote response after path A-B-A while loading the exact new request", async () => {
  const old = deferred(); let first = true;
  const root = await mount(async (input) => {
    if (!(input instanceof Request)) throw new Error(); const path = new URL(input.url).pathname;
    if (path.endsWith("/quotes/quote-1")) { if (first) { first = false; return old.promise; } return json({ ...quote, account_name: "新请求客户" }); }
    if (path.endsWith("/quotes/quote-2")) return json({ ...quote, quote_id: "quote-2", version: 2, state: "superseded" });
    if (path.endsWith("/files")) return json([]);
    return basic(input);
  }, "/costing-quotes/quotes/quote-1");
  await eventually(() => expect(first).toBe(false));
  await router.replace("/costing-quotes/quotes/quote-2"); await eventually(() => expect(root.textContent).toContain("指定版本 V2"));
  await router.replace("/costing-quotes/quotes/quote-1"); await eventually(() => expect(root.textContent).toContain("指定版本 V1"));
  old.resolve(json({ code: "permission_denied", message: "旧路径错误" }, 403));
  await nextTick(); await new Promise((resolve) => setTimeout(resolve, 0));
  expect(root.textContent).toContain("指定版本 V1"); expect(root.textContent).not.toContain("旧路径错误");
});

it.each([[403, "当前身份无权"], [409, "quote_expired"], [503, "并非空数据"]] as const)("keeps customer file %s distinct from an empty list and never falls back to history", async (status, expected) => {
  const paths: string[] = [];
  const root = await mount(async (input) => {
    if (!(input instanceof Request)) throw new Error(); const path = new URL(input.url).pathname; paths.push(path);
    if (path.endsWith("/files")) return json({ code: status === 409 ? "quote_expired" : "dependency_unavailable", message: "受控错误", original_generation_call_id: null, retry_after_seconds: null, tool_call_id: null }, status);
    return json({ code: "permission_denied", message: "拒绝" }, 403);
  }, "/costing-quotes/quotes/quote-1");
  await eventually(() => expect(root.textContent).toContain(expected));
  expect(root.textContent).not.toContain("尚无已加载文件");
  expect(paths.some((path) => path.endsWith("/history"))).toBe(false);
});

it("retains the real generation call id and only reconciles metadata after an explicit action", async () => {
  const writes: { path: string; body: unknown }[] = [];
  const root = await mount(async (input) => {
    if (!(input instanceof Request)) throw new Error(); const path = new URL(input.url).pathname;
    if (input.method === "POST") {
      writes.push({ path, body: await input.json() });
      if (path.endsWith("/reconcile")) return json({ checked_at: "2026-08-28T00:00:00Z", file, original_generation_call_id: "call-actual", original_ledger_modified: false, original_status_at_check: "executing", outcome: "metadata_recovered_original_unresolved", recovery_call_id: "recovery-1" });
      return json({ code: "reconciliation_required", message: "原调用待核对", original_generation_call_id: "call-actual", retry_after_seconds: null, tool_call_id: "call-actual" }, 409);
    }
    if (path.endsWith("/files")) return json([]);
    return json({ code: "permission_denied", message: "拒绝" }, 403);
  }, "/costing-quotes/quotes/quote-1");
  click(root, "请求生成客户文件"); await eventually(() => expect(root.textContent).toContain("reconciliation_required"));
  expect(root.querySelector<HTMLInputElement>('[name="original-generation-call"]')?.value).toBe("call-actual");
  expect(writes).toHaveLength(1); click(root, "仅恢复原调用");
  await eventually(() => expect(root.textContent).toContain("原调用仍未决"));
  expect(writes[1]?.body).toEqual({ quote_id: "quote-1", original_generation_call_id: "call-actual" });
  expect(root.textContent).not.toContain("文件已生成");
});

it("preserves the original generation reference on reconcile errors and displays the recovery call separately", async () => {
  const writes: unknown[] = [];
  const failure: components["schemas"]["QuoteFileApiError"] = { code: "recovery_unavailable", message: "恢复暂不可用", original_generation_call_id: null, retry_after_seconds: null, tool_call_id: "recovery-call-2" };
  const root = await mount(async (input) => {
    if (!(input instanceof Request)) throw new Error(); const path = new URL(input.url).pathname;
    if (path.endsWith("/reconcile")) {
      writes.push(await input.json());
      if (writes.length === 1) return json(failure, 503);
      return json({ checked_at: "2026-08-28T00:00:00Z", file, original_generation_call_id: "generation-call-1", original_ledger_modified: false, original_status_at_check: "executing", outcome: "metadata_recovered_original_unresolved", recovery_call_id: "recovery-call-3" });
    }
    if (path.endsWith("/files")) return json([]);
    return json({ code: "permission_denied", message: "拒绝" }, 403);
  }, "/costing-quotes/quotes/quote-1");
  await eventually(() => expect(root.textContent).toContain("尚无已加载文件"));
  field(root, "original-generation-call", "generation-call-1"); await nextTick(); click(root, "仅恢复原调用");
  await eventually(() => expect(root.textContent).toContain("recovery_unavailable"));
  expect(root.querySelector<HTMLInputElement>('[name="original-generation-call"]')?.value).toBe("generation-call-1");
  expect(root.textContent).toContain("本次恢复调用 ID：recovery-call-2");
  expect(root.textContent).not.toContain("调用引用已变化"); expect(writes).toHaveLength(1);
  click(root, "仅恢复原调用"); await eventually(() => expect(root.textContent).toContain("原调用仍未决"));
  expect(writes).toEqual([{ quote_id: "quote-1", original_generation_call_id: "generation-call-1" }, { quote_id: "quote-1", original_generation_call_id: "generation-call-1" }]);
  expect(root.textContent).toContain("本次恢复调用 ID：recovery-call-3"); expect(root.textContent).not.toContain("recovery-call-2");
  configureAuthenticatedIdentity("tenant-b", "employee-b"); await nextTick();
  expect(root.textContent).not.toContain("recovery-call-3");
  expect(root.querySelector<HTMLInputElement>('[name="original-generation-call"]')?.value).toBe("");
});

it("rejects a recovery receipt after the original-call input changes A-B-A", async () => {
  const pending = deferred(); let started = false;
  const root = await mount(async (input) => {
    if (!(input instanceof Request)) throw new Error(); const path = new URL(input.url).pathname;
    if (path.endsWith("/reconcile")) { started = true; return pending.promise; }
    if (path.endsWith("/files")) return json([]);
    return json({ code: "permission_denied", message: "拒绝" }, 403);
  }, "/costing-quotes/quotes/quote-1");
  field(root, "original-generation-call", "call-a"); await nextTick(); click(root, "仅恢复原调用");
  await eventually(() => expect(started).toBe(true));
  field(root, "original-generation-call", "call-b"); field(root, "original-generation-call", "call-a");
  pending.resolve(json({ checked_at: "2026-08-28T00:00:00Z", file, original_generation_call_id: "call-a", original_ledger_modified: false, original_status_at_check: "executing", outcome: "metadata_recovered_original_unresolved", recovery_call_id: "recovery-1" }));
  await nextTick(); await new Promise((resolve) => setTimeout(resolve, 0));
  expect(root.textContent).not.toContain("file-1");
});

it("invalidates the old opportunity preview even when the input returns to the same opportunity", async () => {
  const pending = deferred(); let started = false;
  const root = await mount(async (input) => {
    if (!(input instanceof Request)) throw new Error();
    if (new URL(input.url).pathname.endsWith("/evidence/preview")) { started = true; return pending.promise; }
    return basic(input);
  });
  field(root, "opportunity-id", opp); click(root, "读取成本版本");
  await eventually(() => expect(root.querySelector('[name="price-source"]')).not.toBeNull());
  field(root, "price-source", "source-a"); field(root, "price-page", "1"); await nextTick(); click(root, "预览价格原文");
  await eventually(() => expect(started).toBe(true));
  field(root, "opportunity-id", "opp_01M0PWRX23T9DP9ENM9PW5GFC9"); await nextTick();
  field(root, "opportunity-id", opp); click(root, "读取成本版本");
  await eventually(() => expect(root.querySelector('[name="price-source"]')).not.toBeNull());
  pending.resolve(json({ artifact_id: "artifact", page: 1, profile: "pdf-text-v1", raw_hash: "raw", scope: { purpose: "pricing" }, source_ref: "source-a", text: "旧机会原文", text_hash: "text" }));
  await nextTick(); await new Promise((resolve) => setTimeout(resolve, 0));
  expect(root.textContent).not.toContain("旧机会原文");
  expect(root.querySelector<HTMLInputElement>('[name="price-source"]')?.value).toBe("");
});

it("discards legacy item save outcomes when the user changes its input and leaves the new intent editable", async () => {
  const pending = deferred(); let started = false;
  const root = await mount(async (input) => {
    if (!(input instanceof Request)) throw new Error();
    if (new URL(input.url).pathname.endsWith("/items")) { started = true; return pending.promise; }
    return basic(input);
  });
  field(root, "opportunity-id", opp); click(root, "读取成本版本");
  await eventually(() => expect(root.querySelector('[name="item-amount"]')).not.toBeNull());
  field(root, "item-amount", "1.234567890123"); field(root, "item-source", "old-source"); click(root, "保存成本项");
  await eventually(() => expect(started).toBe(true));
  field(root, "item-source", "new-source"); await nextTick();
  pending.resolve(new Response(null, { status: 204 })); await nextTick(); await new Promise((resolve) => setTimeout(resolve, 0));
  expect(root.textContent).not.toContain("成本项已保存");
  expect(root.querySelector<HTMLInputElement>('[name="item-source"]')?.value).toBe("new-source");
  expect([...root.querySelectorAll("button")].find((item) => item.textContent?.includes("保存成本项"))?.disabled).toBe(false);
});

it("maps UTF-16 selection without changing evidence text", () => {
  expect(utf16SelectionToCodepoints("A😀B", 1, 3)).toEqual([1, 2]);
  expect(utf16SelectionToCodepoints("A😀B", 3, 4)).toEqual([2, 3]);
  expect(utf16SelectionToCodepoints(" A😀B\n", 0, 6)).toEqual([0, 5]);
  for (const [start, end] of [[1, 2], [2, 3], [3, 3], [-1, 1], [0, 5], [0.5, 1], [0, NaN]]) {
    expect(() => utf16SelectionToCodepoints("A😀B", start!, end!)).toThrow();
  }
});
