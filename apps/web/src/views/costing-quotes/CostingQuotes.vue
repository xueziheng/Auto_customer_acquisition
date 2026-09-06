<script setup lang="ts">
import { computed, inject, reactive, ref, watch } from "vue";
import { RouterLink, useRoute, useRouter } from "vue-router";

import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";
import PricingPolicyForm from "./PricingPolicyForm.vue";
import QuoteIssuerForm from "./QuoteIssuerForm.vue";
import PriceEvidenceForm from "./PriceEvidenceForm.vue";
import NeedUnitConfirmationForm from "./NeedUnitConfirmationForm.vue";
import CostCoverageForm from "./CostCoverageForm.vue";
import CostScopeConfirmationForm from "./CostScopeConfirmationForm.vue";
import QuoteVersions from "./QuoteVersions.vue";
import { createQuotePriceBody, profitMetricLabels } from "./quote-input";
import { quoteError, useQuoteConfirmation, useQuoteRequestScope } from "./quote-request-scope";

type ApiClient = ReturnType<typeof createApiClient>;
type CostItemCreate = components["schemas"]["CostItemCreate"];
type CostSheetCreate = components["schemas"]["CostSheetCreate"];
type CostSheet = components["schemas"]["CostSheetView"];
type Readiness = components["schemas"]["QuoteReadiness"];

const client = inject<ApiClient>("tradeos-api-client", apiClient);
const route = useRoute();
const router = useRouter();
const quoteId = computed(() => typeof route.params.quoteId === "string" ? route.params.quoteId : "");
const opportunityId = ref("");
const routeScope = () => [route.query.opportunity_id, route.query.cost_sheet_id];
const routeOpportunityId = computed(() => typeof route.query.opportunity_id === "string" ? route.query.opportunity_id : "");
const routeSheetId = computed(() => typeof route.query.cost_sheet_id === "string" ? route.query.cost_sheet_id : "");
const routeInputValid = computed(() => {
  if (route.query.opportunity_id === undefined && route.query.cost_sheet_id === undefined) return true;
  return /^opp_[0-7][0-9A-HJKMNP-TV-Z]{25}$/.test(routeOpportunityId.value)
    && (route.query.cost_sheet_id === undefined || !!routeSheetId.value)
    && !quoteId.value;
});
const sheets = ref<CostSheet[]>([]);
const selectedSheetId = ref("");
const loading = ref(false);
const saving = ref(false);
let savingKind: "create" | "item" | null = null;
const checking = ref(false);
const error = ref<string | null>(null);
const notice = ref<string | null>(null);
const readiness = ref<Readiness | null>(null);

const versionType = ref("estimated");
const quantity = ref("");
const baseCurrency = ref("");
const quoteCurrency = ref("");
const fxSnapshotId = ref("");

const itemType = ref("product_purchase");
const itemAmount = ref("");
const itemCurrency = ref("USD");
const priceBasis = ref("quoted");
const isPerUnit = ref(true);
const itemSource = ref("");
const itemNote = ref("");
const expectedItems = ref("product_purchase");
const requestedOpportunity = ref("");
const quoteContext = ref<components["schemas"]["QuotePreparationPublicView"] | null>(null);
const contextError = ref("");
const evidence = ref<(components["schemas"]["SupplierPriceEvidencePublicView"] | components["schemas"]["ExpenseEvidencePublicView"])[]>([]);
const coverage = ref<components["schemas"]["CostCoveragePublicView"] | null>(null);
const scopeConfirmation = ref<components["schemas"]["CostScopePublicView"] | null>(null);
const selectedQuote = ref<components["schemas"]["QuoteInternalPublicView"] | null>(null);
const calculated = ref<components["schemas"]["CalculationSnapshot"] | null>(null);
const targetCalculated = ref<components["schemas"]["CalculationSnapshot"] | null>(null);
const fx = ref<components["schemas"]["QuoteFxPublicView"] | null>(null);
const fxForm = reactive({ base: "", quote: "", rate: "", source: "", observed: "", ref: "" });
const draft = reactive({ amount: "", currency: "", unitPlaces: "", totalPlaces: "", strategy: "", valid: "", revisionAcknowledged: false });
const termDrafts = reactive<Record<components["schemas"]["QuoteTerm"]["kind"], string>>({ discount: "", delivery_commitment: "", payment_terms: "", certification_commitment: "" });
const terms = computed<components["schemas"]["QuoteTerm"][]>(() => {
  const result: components["schemas"]["QuoteTerm"][] = [];
  for (const kind of ["discount", "delivery_commitment", "payment_terms", "certification_commitment"] as const) if (termDrafts[kind].trim()) result.push({ kind, text: termDrafts[kind] });
  return result;
});
function clearBusiness(): void {
  requestedOpportunity.value = ""; sheets.value = []; selectedSheetId.value = ""; readiness.value = null;
  quoteContext.value = null; contextError.value = ""; evidence.value = []; coverage.value = null; scopeConfirmation.value = null; selectedQuote.value = null;
  calculated.value = null; targetCalculated.value = null; fx.value = null; error.value = null; notice.value = null;
  loading.value = false; saving.value = false; savingKind = null; checking.value = false;
}
function resetIdentity(): void {
  clearBusiness(); opportunityId.value = ""; quantity.value = ""; baseCurrency.value = ""; quoteCurrency.value = ""; fxSnapshotId.value = "";
  itemAmount.value = ""; itemSource.value = ""; itemNote.value = ""; itemCurrency.value = "";
  Object.assign(fxForm, { base: "", quote: "", rate: "", source: "", observed: "", ref: "" });
  Object.assign(draft, { amount: "", currency: "", unitPlaces: "", totalPlaces: "", strategy: "", valid: "", revisionAcknowledged: false });
  for (const kind of Object.keys(termDrafts) as (keyof typeof termDrafts)[]) termDrafts[kind] = "";
}
const pageGate = useQuoteRequestScope(client, () => [...routeScope(), opportunityId.value, quoteId.value], resetIdentity);
const itemScope = () => [...routeScope(), opportunityId.value, quoteId.value, selectedSheetId.value, itemAmount.value, itemCurrency.value, itemSource.value, itemNote.value, itemType.value, priceBasis.value, isPerUnit.value, expectedItems.value];
const createScope = () => [...routeScope(), opportunityId.value, quoteId.value, quantity.value, baseCurrency.value, quoteCurrency.value, versionType.value, fxSnapshotId.value];
const itemGate = useQuoteRequestScope(client, itemScope, () => {});
const createGate = useQuoteRequestScope(client, createScope, () => {});
watch(itemScope, () => {
  if (savingKind === "item" && saving.value) { saving.value = false; savingKind = null; notice.value = "原成本项操作结果待核对；输入已变更，不自动重发"; }
  checking.value = false;
}, { flush: "sync" });
watch(createScope, () => {
  if (savingKind === "create" && saving.value) { saving.value = false; savingKind = null; notice.value = "原成本表操作结果待核对；输入已变更，不自动重发"; }
}, { flush: "sync" });
const selectedSheet = computed(() =>
  sheets.value.find((sheet) => sheet.cost_sheet_id === selectedSheetId.value) ?? null,
);
const quoteMutation = useQuoteConfirmation(client, () => [...routeScope(), opportunityId.value, quoteId.value, selectedSheetId.value, selectedSheet.value?.content_hash, JSON.stringify(draft), JSON.stringify(terms.value), fxForm.ref, quoteContext.value?.context_hash, scopeConfirmation.value?.confirmation_id], () => {});
const fxMutation = useQuoteConfirmation(client, () => [...routeScope(), opportunityId.value, quoteId.value, JSON.stringify(fxForm)], () => { fx.value = null; });
watch(() => [...routeScope(), opportunityId.value, quoteId.value], clearBusiness, { flush: "sync" });
function clearCostConfirmation(): void {
  coverage.value = null; scopeConfirmation.value = null; calculated.value = null; targetCalculated.value = null;
}
watch(selectedSheetId, clearCostConfirmation, { flush: "sync" });

const scopeCurrent = computed(() => {
  const confirmation = scopeConfirmation.value;
  return !!confirmation && confirmation.sheet_hash === selectedSheet.value?.content_hash
    && confirmation.need_facts_hash === quoteContext.value?.need_facts_hash
    && confirmation.coverage_hash === coverage.value?.content_hash
    && new Date(confirmation.valid_until).getTime() === new Date(draft.valid).getTime()
    && JSON.stringify(confirmation.terms) === JSON.stringify(terms.value)
    && confirmation.evidence_bindings.every((binding) => evidence.value.some((item) => item.evidence_id === binding.evidence_id && item.evidence_hash === binding.evidence_hash));
});
watch(() => [JSON.stringify(draft), JSON.stringify(terms.value), fxForm.ref, selectedSheet.value?.content_hash, quoteContext.value?.context_hash], () => {
  calculated.value = null; targetCalculated.value = null;
}, { flush: "sync" });

const itemTypes = [
  ["product_purchase", "产品采购"],
  ["sample_fee", "样品"],
  ["mold_fee", "模具"],
  ["customization_fee", "定制"],
  ["logo_printing", "Logo 印刷"],
  ["packaging", "包装"],
  ["quality_inspection", "质检"],
  ["wastage", "损耗"],
  ["domestic_freight", "国内运输"],
  ["international_freight", "国际运输"],
  ["insurance", "保险"],
  ["customs_clearance", "报关"],
  ["duties_and_taxes", "关税与税费"],
  ["destination_freight", "目的地运输"],
  ["warehousing", "仓储"],
  ["payment_fees", "支付手续费"],
  ["sales_commission", "销售佣金"],
  ["customer_acquisition", "获客"],
  ["contact_data_cost", "联系人数据"],
  ["ad_allocation", "广告分摊"],
  ["agent_api_allocation", "Agent/API 分摊"],
  ["returns_reserve", "退货售后预留"],
] as const;

function safeError(status: number): string {
  if (status === 400) return "成本参数无效，请核对金额字符串、币种和来源";
  if (status === 404) return "指定机会不存在或当前不可见";
  if (status === 401) return "当前身份已失效";
  if (status === 403) return "当前角色无权查看或维护成本表";
  if (status === 503) return "成本服务暂不可用";
  return "请求未完成，请稍后重试";
}

function validOpportunity(): boolean {
  if (!routeInputValid.value) { error.value = "路由中的机会或成本引用无效，请从精确对象链接重新进入"; return false; }
  if (!/^opp_[0-7][0-9A-HJKMNP-TV-Z]{25}$/.test(opportunityId.value.trim())) {
    error.value = "请输入有效的 Opportunity ID";
    return false;
  }
  return true;
}

async function loadVersions(): Promise<void> {
  if (!validOpportunity()) return;
  if (route.query.cost_sheet_id !== undefined && opportunityId.value !== routeOpportunityId.value) { clearBusiness(); error.value = "指定成本表必须与路由中的机会一同核对"; return; }
  requestedOpportunity.value = opportunityId.value.trim();
  void loadQuoteContext();
  void loadEvidence();
  const op = pageGate.begin("legacy-list"); if (!op?.valid()) return;
  loading.value = true;
  error.value = null;
  notice.value = null;
  readiness.value = null;
  try {
    const result = await client.GET(
      "/costing-quotes/opportunities/{opportunity_id}/cost-sheets",
      { params: { path: { opportunity_id: opportunityId.value.trim() } }, signal: op.signal },
    );
    if (!op.valid()) return;
    if (result.response.status !== 200 || !result.data) {
      sheets.value = []; clearCostConfirmation();
      if (result.response.status === 401 || result.response.status === 403) revokeBusiness();
      error.value = safeError(result.response.status);
      return;
    }
    if (result.data.some((sheet) => sheet.opportunity_id !== requestedOpportunity.value)) {
      revokeBusiness(); error.value = "成本表与当前机会不一致，请重新核对"; return;
    }
    // query 定位进入目标；同一作用域内保留用户已创建或明确选择的版本。
    const targetSheetId = selectedSheetId.value || routeSheetId.value;
    if (targetSheetId && !result.data.some((sheet) => sheet.cost_sheet_id === targetSheetId)) {
      sheets.value = []; clearCostConfirmation();
      // 缺失时保留目标意图，后续重读不能静默退回原 query 的另一个版本。
      selectedSheetId.value = targetSheetId; error.value = "指定成本表不存在或不属于当前机会"; return;
    }
    sheets.value = result.data;
    if (targetSheetId) selectedSheetId.value = targetSheetId;
    if (!sheets.value.some((sheet) => sheet.cost_sheet_id === selectedSheetId.value)) {
      selectedSheetId.value = sheets.value[0]?.cost_sheet_id ?? "";
    }
  } catch {
    if (op.valid()) { sheets.value = []; clearCostConfirmation(); error.value = "无法连接成本服务"; }
  } finally {
    if (op.valid()) loading.value = false;
  }
}

async function createSheet(): Promise<void> {
  if (!validOpportunity() || saving.value) return;
  const parsedQuantity = Number(quantity.value);
  if (!Number.isInteger(parsedQuantity) || parsedQuantity <= 0) {
    error.value = "数量必须是正整数";
    return;
  }
  const body: CostSheetCreate = {
    base_currency: baseCurrency.value.trim().toUpperCase(),
    fx_rates: [],
    fx_snapshot_id: fxSnapshotId.value.trim() || null,
    quantity: parsedQuantity,
    quote_currency: quoteCurrency.value.trim().toUpperCase(),
    version_type: versionType.value,
  };
  if (body.version_type === "quoted" && !body.fx_snapshot_id) {
    error.value = "QUOTED 版本必须填写汇率快照 ID";
    return;
  }
  saving.value = true;
  savingKind = "create";
  const op = createGate.begin("legacy-create"); if (!op?.valid()) { saving.value = false; return; }
  error.value = null;
  try {
    const result = await client.POST(
      "/costing-quotes/opportunities/{opportunity_id}/cost-sheets",
      {
        params: { path: { opportunity_id: opportunityId.value.trim() } },
        body,
        signal: op.signal,
      },
    );
    if (!op.valid()) return;
    if (result.response.status !== 201 || !result.data) {
      error.value = safeError(result.response.status);
      return;
    }
    selectedSheetId.value = result.data.cost_sheet_id;
    await loadVersions();
    if (op.valid()) notice.value = "成本表版本已创建";
  } catch {
    if (op.valid()) error.value = "成本表创建结果未知，请先核对成本版本";
  } finally {
    if (op.valid()) saving.value = false;
  }
}

async function saveItem(): Promise<void> {
  if (!selectedSheet.value || saving.value) return;
  if (!itemAmount.value.trim() || !itemSource.value.trim()) {
    error.value = "金额和来源引用均为必填项";
    return;
  }
  const body: CostItemCreate = {
    amount: itemAmount.value.trim(),
    currency: itemCurrency.value.trim().toUpperCase(),
    is_per_unit: isPerUnit.value,
    item_type: itemType.value,
    note: itemNote.value.trim() || null,
    price_basis: priceBasis.value,
    source_ref: itemSource.value.trim(),
  };
  saving.value = true;
  savingKind = "item";
  const op = itemGate.begin("legacy-item"); if (!op?.valid()) { saving.value = false; return; }
  error.value = null;
  try {
    const result = await client.POST(
      "/costing-quotes/cost-sheets/{cost_sheet_id}/items",
      {
        params: { path: { cost_sheet_id: selectedSheet.value.cost_sheet_id } },
        body,
        signal: op.signal,
      },
    );
    if (!op.valid()) return;
    if (result.response.status !== 204) {
      error.value = safeError(result.response.status);
      return;
    }
    await loadVersions();
    if (op.valid()) { notice.value = "成本项已保存，来源与录入人已留痕"; saving.value = false; itemAmount.value = ""; itemSource.value = ""; itemNote.value = ""; }
  } catch {
    if (op.valid()) error.value = "成本项保存结果未知，请先核对成本表";
  } finally {
    if (op.valid()) saving.value = false;
  }
}

async function checkReadiness(): Promise<void> {
  if (!selectedSheet.value || checking.value) return;
  const expected = expectedItems.value
    .split(",")
    .map((value) => value.trim())
    .filter((value, index, values) => value && values.indexOf(value) === index);
  checking.value = true;
  const op = itemGate.begin("readiness"); if (!op?.valid()) { checking.value = false; return; }
  error.value = null;
  try {
    const result = await client.POST(
      "/costing-quotes/cost-sheets/{cost_sheet_id}/readiness",
      {
        params: { path: { cost_sheet_id: selectedSheet.value.cost_sheet_id } },
        body: { expected_item_types: expected },
        signal: op.signal,
      },
    );
    if (!op.valid()) return;
    if (result.response.status !== 200 || !result.data) {
      error.value = safeError(result.response.status);
      return;
    }
    readiness.value = result.data;
  } catch {
    if (op.valid()) error.value = "报价就绪检查未完成，请稍后重试";
  } finally {
    if (op.valid()) checking.value = false;
  }
}

function revokeBusiness(): void {
  pageGate.invalidate(); itemGate.invalidate(); createGate.invalidate(); quoteMutation.invalidate(); fxMutation.invalidate();
  clearBusiness();
}

async function loadQuoteContext(): Promise<void> {
  if (!requestedOpportunity.value) return;
  const op = pageGate.begin("context"); if (!op?.valid()) return;
  try {
    const result = await client.GET("/costing-quotes/opportunities/{opportunity_id}/quote-context", { params: { path: { opportunity_id: requestedOpportunity.value } }, signal: op.signal });
    if (!op.valid()) return;
    if (result.data && result.data.opportunity_id === requestedOpportunity.value) { quoteContext.value = result.data; contextError.value = ""; }
    else {
      if (result.response.status === 401 || result.response.status === 403 || result.data) revokeBusiness();
      contextError.value = result.data ? "报价准备与当前机会不一致，请重新核对" : quoteError(result.response.status, result.error);
    }
  } catch { if (op.valid()) contextError.value = "报价准备事实读取失败"; }
}
async function loadEvidence(): Promise<void> {
  if (!requestedOpportunity.value) return;
  const op = pageGate.begin("evidence"); if (!op?.valid()) return;
  try {
    const result = await client.GET("/costing-quotes/opportunities/{opportunity_id}/price-evidence", { params: { path: { opportunity_id: requestedOpportunity.value } }, signal: op.signal });
    if (!op.valid()) return;
    if (result.data) evidence.value = result.data;
    else {
      if (result.response.status === 401 || result.response.status === 403) revokeBusiness();
      contextError.value = quoteError(result.response.status, result.error);
    }
  } catch { if (op.valid()) contextError.value = "价格依据摘要读取失败"; }
}
async function saveFx(): Promise<void> {
  const body: components["schemas"]["QuoteFxCreate"] = { base_currency: fxForm.base.trim(), quote_currency: fxForm.quote.trim(), rate: fxForm.rate.trim(), source_ref: fxForm.source.trim(), observed_at: fxForm.observed.trim() };
  await fxMutation.confirm(body, (id, signal) => client.POST("/costing-quotes/quote-fx", { params: { header: { "Idempotency-Key": id } }, body, signal }), (data) => { fx.value = data; });
}
async function readFx(): Promise<void> {
  const op = fxMutation.begin("read"); if (!op?.valid() || !fxForm.ref) return;
  try {
    const result = await client.GET("/costing-quotes/quote-fx/{fx_id}", { params: { path: { fx_id: fxForm.ref } }, signal: op.signal });
    if (!op.valid()) return;
    if (result.data) fx.value = result.data; else fxMutation.message.value = quoteError(result.response.status, result.error);
  } catch { if (op.valid()) fxMutation.message.value = "汇率记录读取失败"; }
}
function rounding(): components["schemas"]["QuoteRoundingInput"] | null {
  if (!draft.unitPlaces.trim() || !draft.totalPlaces.trim() || !draft.strategy.trim()) { quoteMutation.message.value = "请人工配置单价、整单精度和舍入策略"; return null; }
  return { unit_places: Number(draft.unitPlaces), total_places: Number(draft.totalPlaces), strategy: draft.strategy };
}
async function calculate(mode: "target" | "manual"): Promise<void> {
  if (!selectedSheet.value) return;
  const rules = rounding(); if (!rules) return;
  const op = quoteMutation.begin(`calculate-${mode}`); if (!op?.valid()) return;
  try {
    const body: components["schemas"]["CostCalculationCommand"] = { algorithm_version: "costing-v1", mode, rounding: rules, quote_fx_ref: fxForm.ref || null, unit_price: mode === "manual" ? createQuotePriceBody(draft.amount, draft.currency) : null };
    const result = await client.POST("/costing-quotes/cost-sheets/{sheet_id}/calculate", { params: { path: { sheet_id: selectedSheet.value.cost_sheet_id } }, body, signal: op.signal });
    if (!op.valid()) return;
    if (result.data) { if (mode === "target") targetCalculated.value = result.data; else calculated.value = result.data; }
    else quoteMutation.message.value = quoteError(result.response.status, result.error);
  } catch { if (op.valid()) quoteMutation.message.value = "计算未完成，请核对输入与服务状态"; }
}
async function createQuote(revision = false): Promise<void> {
  if (!selectedSheet.value || !quoteContext.value?.context_hash || !scopeConfirmation.value || !scopeCurrent.value) return;
  if (revision && (!selectedQuote.value || !draft.revisionAcknowledged)) { quoteMutation.message.value = "请确认修订会停用旧版"; return; }
  const rules = rounding(); if (!rules) return;
  let unit_price: components["schemas"]["Money"];
  try { unit_price = createQuotePriceBody(draft.amount, draft.currency); } catch { quoteMutation.message.value = "请填写报价单价和币种"; return; }
  const body: components["schemas"]["QuoteDraftCommand"] = { opportunity_id: requestedOpportunity.value, cost_sheet_id: selectedSheet.value.cost_sheet_id, expected_context_hash: quoteContext.value.context_hash, expected_sheet_hash: selectedSheet.value.content_hash, expected_quote_version: revision ? selectedQuote.value!.version : null, replaces_quote_id: revision ? selectedQuote.value!.quote_id : null, rounding: rules, quote_fx_ref: fxForm.ref || null, scope_confirmation_id: scopeConfirmation.value.confirmation_id, terms: terms.value.map((term) => ({ ...term })), unit_price, valid_until: draft.valid };
  await quoteMutation.confirm(body, (id, signal) => revision
    ? client.POST("/costing-quotes/quotes/{quote_id}/revisions", { params: { path: { quote_id: selectedQuote.value!.quote_id }, header: { "Idempotency-Key": id } }, body, signal })
    : client.POST("/costing-quotes/opportunities/{opportunity_id}/quotes", { params: { path: { opportunity_id: requestedOpportunity.value }, header: { "Idempotency-Key": id } }, body, signal }), (data) => { notice.value = `已保存报价 ${data.quote_id}，尚未审批或发送`; void router.push(`/costing-quotes/quotes/${data.quote_id}`); });
}

function formatDate(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? value
    : parsed.toLocaleString("zh-CN", { hour12: false });
}
watch([() => route.query.opportunity_id, () => route.query.cost_sheet_id], (_value, previous) => {
  if (previous.length) resetIdentity();
  if (route.query.opportunity_id === undefined && route.query.cost_sheet_id === undefined) return;
  if (!routeInputValid.value) {
    error.value = "路由中的机会或成本引用无效，请从精确对象链接重新进入";
    return;
  }
  opportunityId.value = routeOpportunityId.value;
  void loadVersions();
}, { immediate: true, flush: "sync" });
</script>

<template>
  <div class="shell costing-shell">
    <div class="page-head costing-head">
      <div>
        <p class="phase-eyebrow">
          DETERMINISTIC COSTING
        </p><h1>成本与报价</h1>
      </div>
      <span class="status manual-status">Phase 2 · 成本与报价</span>
    </div>
    <div class="safe-banner">
      <span aria-hidden="true">i</span>
      <div>金额、比例与汇率只以 Decimal 字符串提交，由后端确定性计算。本批支持成本确认、报价审批与文件交付，批准不发送；不代表整个 Phase 2 完成。实际能力和权限由后端配置与 allowed_actions 决定。</div>
    </div>
    <div
      v-if="error"
      class="safe-banner danger"
      role="alert"
    >
      {{ error }}
    </div>
    <div
      v-if="notice"
      class="safe-banner success"
      role="status"
    >
      {{ notice }}
    </div>

    <section class="opportunity-bar panel">
      <label>Opportunity ID
        <input
          v-model.trim="opportunityId"
          name="opportunity-id"
          placeholder="opp_…"
        >
      </label>
      <button
        type="button"
        :disabled="loading"
        @click="loadVersions"
      >
        {{ loading ? "读取中…" : "读取成本版本" }}
      </button>
    </section>

    <QuoteVersions
      :opportunity-id="requestedOpportunity"
      :quote-id="routeInputValid ? quoteId : ''"
      @selected="selectedQuote = $event"
    />
    <details class="panel">
      <summary>老板配置：定价政策与本公司抬头</summary><PricingPolicyForm /><QuoteIssuerForm @saved="loadQuoteContext" />
    </details>
    <p
      v-if="contextError"
      role="alert"
    >
      报价准备：{{ contextError }}
    </p>
    <section
      v-if="quoteContext"
      class="panel"
    >
      <h2>报价准备事实</h2><RouterLink :to="{ name: 'validated-need-detail', params: { needId: quoteContext.need.need_id } }">
        查看需求原话与来源 {{ quoteContext.need.need_id }}
      </RouterLink><p>{{ quoteContext.account_name }} · {{ quoteContext.country }} · {{ quoteContext.specification.product_category }}</p><p>单位状态 {{ quoteContext.unit_status }} · 需求 hash {{ quoteContext.need_facts_hash }} · context {{ quoteContext.context_hash ?? '尚不完整' }}</p><p
        v-for="blocker in quoteContext.blockers"
        :key="`${blocker.field}-${blocker.code}`"
      >
        {{ blocker.field }}：{{ blocker.code }}
      </p><button @click="loadQuoteContext">
        核对最新需求事实
      </button>
    </section>
    <NeedUnitConfirmationForm
      v-if="quoteContext"
      :key="`unit-${requestedOpportunity}`"
      :need-id="quoteContext.need.need_id"
      @saved="loadQuoteContext"
    />
    <PriceEvidenceForm
      v-if="quoteContext"
      :key="`price-${requestedOpportunity}`"
      :opportunity-id="requestedOpportunity"
      :need-id="quoteContext.need.need_id"
      @saved="loadEvidence"
    />
    <section class="workspace-grid">
      <article class="panel">
        <header>
          <div>
            <p class="card-kicker">
              NEW VERSION
            </p><h2>创建成本表版本</h2>
          </div>
        </header>
        <div class="field-grid">
          <label>版本类型<select v-model="versionType"><option value="estimated">ESTIMATED</option><option value="quoted">QUOTED</option><option value="actual">ACTUAL</option></select></label>
          <label>数量<input
            v-model="quantity"
            inputmode="numeric"
          ></label>
          <label>核算币种<input
            v-model.trim="baseCurrency"
            maxlength="3"
          ></label>
          <label>报价币种<input
            v-model.trim="quoteCurrency"
            maxlength="3"
          ></label>
          <label class="wide">汇率快照 ID<input
            v-model.trim="fxSnapshotId"
            placeholder="QUOTED 必填"
          ></label>
        </div>
        <button
          class="btn-primary"
          type="button"
          :disabled="saving"
          @click="createSheet"
        >
          创建新版本
        </button>
      </article>

      <article class="panel versions-panel">
        <header>
          <div>
            <p class="card-kicker">
              VERSION LEDGER
            </p><h2>成本版本</h2>
          </div><span>{{ sheets.length }} 个</span>
        </header>
        <div
          v-if="!sheets.length"
          class="empty"
        >
          输入 Opportunity ID 后读取版本
        </div>
        <button
          v-for="sheet in sheets"
          :key="sheet.cost_sheet_id"
          class="version-row"
          :class="{ selected: selectedSheetId === sheet.cost_sheet_id }"
          type="button"
          @click="selectedSheetId = sheet.cost_sheet_id; readiness = null"
        >
          <span><strong>{{ sheet.version_type.toUpperCase() }} · v{{ sheet.version_number }}</strong><small>{{ sheet.quantity }} 件 · {{ sheet.base_currency }} → {{ sheet.quote_currency }}</small></span>
          <span class="state-pill">{{ sheet.is_locked ? "已锁定" : "可编辑" }}</span>
        </button>
      </article>
    </section>

    <section
      v-if="selectedSheet"
      class="detail-grid"
    >
      <article class="panel item-panel">
        <header>
          <div>
            <p class="card-kicker">
              COST PROVENANCE
            </p><h2>成本项与来源</h2>
          </div><span>{{ selectedSheet.cost_sheet_id }}</span>
        </header>
        <div
          v-if="!selectedSheet.items.length"
          class="empty"
        >
          还没有已确认成本项
        </div>
        <div
          v-else
          class="item-list"
        >
          <article
            v-for="item in selectedSheet.items"
            :key="`${item.item_type}:${item.source_ref}`"
            class="item-row"
          >
            <div><strong>{{ item.item_label }}</strong><small>{{ item.price_basis }} · {{ item.is_per_unit ? "单件" : "整单" }}</small></div>
            <div class="amount">
              {{ item.amount.amount }} {{ item.amount.currency }}
            </div>
            <div class="source">
              <span>来源</span><strong>{{ item.source_ref ?? "未提供" }}</strong><small>{{ item.entered_by_id ?? "待人工确认" }}</small>
            </div>
          </article>
        </div>
        <div class="field-grid item-form">
          <label>成本类型<select v-model="itemType"><option
            v-for="option in itemTypes"
            :key="option[0]"
            :value="option[0]"
          >{{ option[1] }}</option></select></label>
          <label>金额<input
            v-model.trim="itemAmount"
            name="item-amount"
            inputmode="decimal"
            placeholder="例如 12.50"
          ></label>
          <label>币种<input
            v-model.trim="itemCurrency"
            maxlength="3"
          ></label>
          <label>价格基准<select v-model="priceBasis"><option value="quoted">quoted</option><option value="indicative">indicative</option><option value="actual">actual</option></select></label>
          <label class="wide">来源引用<input
            v-model.trim="itemSource"
            name="item-source"
            placeholder="供应商报价或证据 Artifact 引用"
          ></label>
          <label class="wide">备注<input
            v-model.trim="itemNote"
            placeholder="可选，不代替来源"
          ></label>
          <label class="check"><input
            v-model="isPerUnit"
            type="checkbox"
          >按单件计价</label>
        </div>
        <button
          class="btn-primary"
          type="button"
          :disabled="saving || selectedSheet.is_locked"
          @click="saveItem"
        >
          保存成本项
        </button>
      </article>

      <aside class="panel readiness-panel">
        <p class="card-kicker">
          QUOTE READINESS
        </p><h2>报价阻断检查</h2>
        <p class="muted">
          只读检查，不会锁定成本表，也不会接受参考价风险。
        </p>
        <label>场景要求的成本类型
          <input
            v-model.trim="expectedItems"
            name="expected-items"
            placeholder="逗号分隔的 CostItemType"
          >
        </label>
        <button
          type="button"
          :disabled="checking"
          @click="checkReadiness"
        >
          {{ checking ? "检查中…" : "检查报价就绪" }}
        </button>
        <div
          v-if="readiness"
          class="readiness-result"
          :class="{ ready: readiness.ready }"
        >
          <strong>{{ readiness.ready ? "结构检查通过" : "仍有阻断项" }}</strong>
          <ul>
            <li
              v-for="blocker in readiness.blockers"
              :key="blocker"
            >
              {{ blocker }}
            </li>
          </ul>
          <div v-if="readiness.missing_items?.length">
            <span>缺少成本项</span><code
              v-for="item in readiness.missing_items"
              :key="item"
            >{{ item }}</code>
          </div>
          <div v-if="readiness.indicative_items?.length">
            <span>参考价成本项</span><code
              v-for="item in readiness.indicative_items"
              :key="item"
            >{{ item }}</code>
          </div>
        </div>
        <dl><div><dt>单位完整成本</dt><dd>{{ selectedSheet.unit_full_cost ? `${selectedSheet.unit_full_cost.amount} ${selectedSheet.unit_full_cost.currency}` : "尚不可计算" }}</dd></div><div><dt>汇率快照</dt><dd>{{ selectedSheet.fx_snapshot_id ?? "未绑定" }}</dd></div><div><dt>创建时间</dt><dd>{{ formatDate(selectedSheet.created_at) }}</dd></div></dl>
      </aside>
    </section>
    <template v-if="selectedSheet && quoteContext">
      <CostCoverageForm
        :key="selectedSheet.cost_sheet_id"
        :sheet="selectedSheet"
        @saved="coverage = $event"
      />
      <section class="panel">
        <h2>报价汇率与精确输入</h2><p>人工填写核算币种 → 报价币种的直连汇率及来源；不推算倒数、不复用旧方向。</p>
        <div class="field-grid">
          <label>核算币种<input
            v-model="fxForm.base"
            name="fx-base"
          ></label><label>报价币种<input
            v-model="fxForm.quote"
            name="fx-quote"
          ></label><label>汇率（十进制字符串）<input
            v-model="fxForm.rate"
            name="fx-rate"
          ></label><label>汇率来源<input
            v-model="fxForm.source"
            name="fx-source"
          ></label><label>观察时间（含时区）<input
            v-model="fxForm.observed"
            name="fx-observed"
          ></label>
        </div>
        <button
          :disabled="!fxMutation.hasIdentity.value || fxMutation.pending.value"
          @click="saveFx"
        >
          确认直连报价汇率
        </button><p role="status">
          {{ fxMutation.message.value }} <code>{{ fxMutation.key.value }}</code>
        </p>
        <label>用于报价的已确认汇率 ID（同币种可空）<input
          v-model="fxForm.ref"
          name="quote-fx-ref"
        ></label><button @click="readFx">
          核对已保存汇率
        </button><p v-if="fx">
          {{ fx.fx_id }} · {{ fx.base_currency }} → {{ fx.quote_currency }} = {{ fx.rate }} · {{ fx.source.source_ref }} · {{ fx.confirmed_by }} · {{ fx.confirmed_at }}
        </p>
        <div class="field-grid">
          <label>客户报价单价<input
            v-model="draft.amount"
            name="quote-price"
          ></label><label>报价币种<input
            v-model="draft.currency"
            name="quote-currency-exact"
          ></label><label>单价小数位<input
            v-model="draft.unitPlaces"
            name="quote-unit-places"
          ></label><label>整单小数位<input
            v-model="draft.totalPlaces"
            name="quote-total-places"
          ></label><label>舍入策略<input
            v-model="draft.strategy"
            name="quote-rounding"
            placeholder="由业务明确配置"
          ></label><label>报价有效期（含时区）<input
            v-model="draft.valid"
            name="quote-valid-until"
          ></label>
          <label>折扣条款（英文）<textarea
            v-model="termDrafts.discount"
            name="term-discount"
          /></label><label>交期承诺（英文）<textarea
            v-model="termDrafts.delivery_commitment"
            name="term-delivery"
          /></label><label>付款条件（英文）<textarea
            v-model="termDrafts.payment_terms"
            name="term-payment"
          /></label><label>认证承诺（英文）<textarea
            v-model="termDrafts.certification_commitment"
            name="term-certification"
          /></label>
        </div><button @click="calculate('target')">
          计算目标报价
        </button><button @click="calculate('manual')">
          计算实际报价收益
        </button>
        <div class="workspace-grid">
          <article
            v-for="[label, result] in [['目标报价', targetCalculated], ['实际报价', calculated]] as const"
            :key="label"
          >
            <h3>{{ label }}</h3><template v-if="result">
              <p>客户单价 {{ result.displayed_unit_price.amount }} {{ result.displayed_unit_price.currency }} / 整单 {{ result.displayed_total.amount }} {{ result.displayed_total.currency }}</p><p>核算有效单价 {{ result.effective_unit_revenue.amount }} {{ result.effective_unit_revenue.currency }}</p><dl>
                <div
                  v-for="(value, name) in result.metrics"
                  :key="name"
                >
                  <dt>{{ profitMetricLabels[name] ?? name }}</dt><dd>{{ value }}</dd>
                </div>
              </dl><p>输入 hash {{ result.inputs_hash }} · 政策 {{ result.policy_id }}</p>
            </template><p v-else>
              尚未计算
            </p>
          </article>
        </div>
      </section>
      <CostScopeConfirmationForm
        :key="`scope-${selectedSheet.cost_sheet_id}`"
        :context="quoteContext"
        :sheet="selectedSheet"
        :coverage="coverage"
        :terms="terms"
        :valid-until="draft.valid"
        :evidence="evidence"
        @selected="scopeConfirmation = $event"
      />
      <section class="panel">
        <h2>确认报价版本</h2><p>数量 {{ selectedSheet.quantity }} · 需求单位 {{ quoteContext.need.unit ?? '缺失' }} · 成本版本 {{ selectedSheet.version_number }} · 适用性确认 {{ scopeConfirmation?.confirmation_id ?? '尚未选择' }}</p>
        <button
          :disabled="!quoteMutation.hasIdentity.value || quoteMutation.pending.value || !scopeCurrent || !quoteContext.context_hash"
          @click="createQuote()"
        >
          确认创建新报价
        </button>
        <template v-if="selectedQuote">
          <p>修订前版 {{ selectedQuote.quote_id }} / V{{ selectedQuote.version }} / 成本 {{ selectedQuote.cost_sheet_id }}；修订将停用旧版，旧成本引用保留。</p><label><input
            v-model="draft.revisionAcknowledged"
            type="checkbox"
            name="revision-acknowledged"
          >我确认修订会停用旧版</label><button
            :disabled="!quoteMutation.hasIdentity.value || quoteMutation.pending.value || !scopeCurrent || !draft.revisionAcknowledged"
            @click="createQuote(true)"
          >
            确认修订此版本
          </button>
        </template>
        <p role="status">
          {{ quoteMutation.message.value }} <code>{{ quoteMutation.key.value }}</code>
        </p><p>结果未知时请使用上方“核对报价与文件记录”；不自动新建，不保证恢复丢失的客户端键。</p>
      </section>
    </template>
  </div>
</template>

<style>
.costing-shell { overflow: auto; }
.costing-shell > section, .costing-shell > details { margin-bottom: var(--space4); }
.costing-shell textarea { width: 100%; box-sizing: border-box; min-height: 60px; border: 1px solid var(--border); padding: 8px; }
.costing-shell a, .costing-shell code, .costing-shell p, .costing-shell dd { overflow-wrap: anywhere; }
.costing-shell .coverage-row { border-top: 1px solid var(--border); padding-top: var(--space3); }
.costing-shell .pdf-preview { width: 100%; height: 600px; border: 1px solid var(--border); background: white; }
.costing-shell .costing-head { justify-content: space-between; padding-top: var(--space3); }
.costing-shell .panel { border: 1px solid var(--border); border-radius: 12px; background: var(--surface); padding: var(--space4); }
.costing-shell .panel > header { display: flex; justify-content: space-between; gap: var(--space3); align-items: start; }
.costing-shell .opportunity-bar { display: flex; gap: var(--space3); align-items: end; }
.costing-shell .opportunity-bar label { flex: 1; }
.costing-shell label { display: grid; gap: var(--space1); color: var(--text-secondary); font-size: 11px; font-weight: 700; }
.costing-shell input, .costing-shell select { min-width: 0; border: 1px solid var(--border); border-radius: var(--radius-sm); background: white; padding: 9px 10px; color: var(--text-primary); }
.costing-shell button { border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 9px 13px; background: white; cursor: pointer; }
.costing-shell .btn-primary { border-color: var(--action); background: var(--action); color: white; }
.costing-shell button:disabled { cursor: not-allowed; opacity: .55; }
.costing-shell .workspace-grid { display: grid; grid-template-columns: 1fr 1fr; gap: var(--space4); }
.costing-shell .detail-grid { display: grid; grid-template-columns: minmax(0, 1.6fr) minmax(280px, .7fr); gap: var(--space4); }
.costing-shell .field-grid { display: grid; grid-template-columns: 1fr 1fr; gap: var(--space3); margin: var(--space4) 0; }
.costing-shell .field-grid .wide { grid-column: 1 / -1; }
.costing-shell .versions-panel { display: grid; align-content: start; gap: var(--space2); }
.costing-shell .version-row { display: flex; justify-content: space-between; align-items: center; width: 100%; text-align: left; }
.costing-shell .version-row.selected { border-color: var(--action); box-shadow: 0 0 0 1px var(--action); }
.costing-shell .version-row span:first-child { display: grid; gap: 3px; }
.costing-shell small, .costing-shell .muted { color: var(--text-secondary); overflow-wrap: anywhere; }
.costing-shell .state-pill { border-radius: 999px; background: var(--fact-soft); color: var(--fact); padding: 3px 8px; font-size: 10px; font-weight: 800; }
.costing-shell .item-list { display: grid; gap: var(--space2); margin-top: var(--space3); }
.costing-shell .item-row { display: grid; grid-template-columns: 1fr auto minmax(170px, .8fr); gap: var(--space3); align-items: center; border: 1px solid var(--border); border-radius: var(--radius-sm); padding: var(--space3); }
.costing-shell .item-row > div { display: grid; gap: 3px; }
.costing-shell .amount { font-variant-numeric: tabular-nums; font-weight: 800; }
.costing-shell .source span { color: var(--text-secondary); font-size: 10px; }
.costing-shell .source strong { overflow-wrap: anywhere; font-size: 11px; }
.costing-shell .item-form { border-top: 1px solid var(--border); padding-top: var(--space4); }
.costing-shell .check { display: flex; align-items: center; gap: var(--space2); }
.costing-shell .check input { width: auto; }
.costing-shell .readiness-panel { align-self: start; display: grid; gap: var(--space3); }
.costing-shell .readiness-result { border-left: 4px solid var(--danger); background: var(--danger-soft); padding: var(--space3); }
.costing-shell .readiness-result.ready { border-left-color: var(--success); background: var(--success-soft); }
.costing-shell .readiness-result ul { padding-left: 18px; }
.costing-shell .readiness-result div { display: flex; flex-wrap: wrap; gap: var(--space1); margin-top: var(--space2); }
.costing-shell code { border-radius: 4px; background: rgba(255,255,255,.7); padding: 2px 5px; }
.costing-shell dl { display: grid; gap: var(--space2); margin: 0; }
.costing-shell dl div { display: grid; grid-template-columns: 90px 1fr; gap: var(--space2); }
.costing-shell dt { color: var(--text-secondary); font-size: 11px; }
.costing-shell dd { margin: 0; overflow-wrap: anywhere; font-size: 12px; }
.costing-shell .empty { display: grid; min-height: 120px; place-items: center; color: var(--text-secondary); }
@media (max-width: 960px) { .costing-shell .workspace-grid, .costing-shell .detail-grid { grid-template-columns: 1fr; } }
@media (max-width: 640px) { .costing-shell .panel > header { flex-direction: column; } .costing-shell .panel > header > span { overflow-wrap: anywhere; max-width: 100%; } .costing-shell .opportunity-bar { align-items: stretch; flex-direction: column; } .costing-shell .field-grid, .costing-shell .item-row { grid-template-columns: 1fr; } .costing-shell .field-grid .wide { grid-column: auto; } }
</style>
