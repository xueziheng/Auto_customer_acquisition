<script setup lang="ts">
import { codeLabel } from "../../components/displayLabels";
/* global Event, HTMLTextAreaElement */
import { inject, reactive, ref, watch } from "vue";
import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";
import { costItemLabel, costItemLabels, createQuotePriceBody, utf16SelectionToCodepoints } from "./quote-input";
import { quoteError, useQuoteConfirmation, useQuoteRequestScope } from "./quote-request-scope";
const props = defineProps<{ opportunityId: string; needId: string }>();
const emit = defineEmits<{ saved: [] }>();
const client = inject<ReturnType<typeof createApiClient>>("tradeos-api-client", apiClient);
const source = ref(""); const page = ref("");
const profile = ref<components["schemas"]["EvidencePreviewRequest"]["profile"]>("pdf-text-v1");
const preview = ref<components["schemas"]["EvidencePreviewPublicView"] | null>(null);
const locator = ref<components["schemas"]["EvidenceLocatorPublicView"] | null>(null);
const selection = ref<readonly [number, number] | null>(null);
const rawError = ref("");
const empty = () => ({ kind: "supplier_price" as "supplier_price" | "confirmed_expense", basis: "quoted" as "quoted" | "indicative" | "actual", amount: "", currency: "", unit: "", moq: "", min: "", max: "", supplier: "", spec: "", destination: "", observed: "", valid: "", itemType: "", allocation: "", quantity: "", perUnit: true });
const form = reactive(empty());
const records = ref<(components["schemas"]["SupplierPriceEvidencePublicView"] | components["schemas"]["ExpenseEvidencePublicView"])[]>([]);
function clearRaw(): void { preview.value = null; locator.value = null; selection.value = null; rawError.value = ""; }
const sourceScope = () => [props.opportunityId, props.needId, source.value, page.value, profile.value];
const rawGate = useQuoteRequestScope(client, () => [...sourceScope(), selection.value?.[0], selection.value?.[1]], clearRaw);
watch(sourceScope, clearRaw, { flush: "sync" });
watch(selection, () => { locator.value = null; }, { flush: "sync" });
const { begin, hasIdentity, confirm, message, key, pending } = useQuoteConfirmation(client, () => [...sourceScope(), JSON.stringify(form), locator.value?.locator], () => {
  source.value = ""; page.value = ""; Object.assign(form, empty()); records.value = []; clearRaw();
});
async function readPreview(): Promise<void> {
  clearRaw(); const op = rawGate.begin("preview"); if (!op?.valid()) return;
  const body: components["schemas"]["EvidencePreviewRequest"] = { operation: "preview", source_ref: source.value, page: profile.value === "pdf-text-v1" ? Number(page.value) : null, profile: profile.value, scope: { purpose: "pricing" } };
  try {
    const result = await client.POST("/costing-quotes/evidence/preview", { body, signal: op.signal });
    if (!op.valid()) return;
    if (result.data) preview.value = result.data; else rawError.value = quoteError(result.response.status, result.error);
  } catch { if (op.valid()) rawError.value = "原文请求失败"; }
}
function selectText(event: Event): void {
  const element = event.target; if (!(element instanceof HTMLTextAreaElement) || !preview.value) return;
  try {
    const next = utf16SelectionToCodepoints(preview.value.text, element.selectionStart, element.selectionEnd);
    if (selection.value?.[0] !== next[0] || selection.value?.[1] !== next[1]) selection.value = next;
  }
  catch { selection.value = null; locator.value = null; rawError.value = "请选择完整的原文字符"; }
}
async function locate(): Promise<void> {
  if (!preview.value || !selection.value) return;
  const op = rawGate.begin("locator"); if (!op?.valid()) return;
  const p = preview.value;
  const body: components["schemas"]["EvidenceLocateRequest"] = { operation: "locate", scope: p.scope, source_ref: p.source_ref, profile: p.profile, page: p.page, expected_raw_hash: p.raw_hash, expected_text_hash: p.text_hash, start: selection.value[0], end: selection.value[1] };
  try {
    const result = await client.POST("/costing-quotes/evidence/locator", { body, signal: op.signal });
    if (!op.valid()) return;
    if (result.data) locator.value = result.data; else rawError.value = quoteError(result.response.status, result.error);
  } catch { if (op.valid()) rawError.value = "选区定位失败"; }
}
async function save(): Promise<void> {
  if (!locator.value) return;
  let money: components["schemas"]["Money"];
  try { money = createQuotePriceBody(form.amount, form.currency); } catch { message.value = "请填写金额和币种"; return; }
  let body: components["schemas"]["SupplierPriceEvidenceCreate"] | components["schemas"]["ExpenseEvidenceCreate"];
  if (form.kind === "supplier_price") {
    if (form.basis === "actual") { message.value = "采购价格请选择已报价或参考价"; return; }
    body = { ...money, kind: "supplier_price", basis: form.basis, opportunity_id: props.opportunityId, need_id: props.needId, source_ref: locator.value.source_ref, locator: locator.value.locator, unit: form.unit, moq: Number(form.moq), quantity_min: Number(form.min), quantity_max: Number(form.max), supplier_ref: form.supplier, specification: form.spec, destination: form.destination, quoted_at: form.observed, valid_until: form.valid };
  } else {
    if (form.basis === "indicative") { message.value = "费用依据请选择已报价或实际"; return; }
    body = { ...money, kind: "confirmed_expense", basis: form.basis, opportunity_id: props.opportunityId, source_ref: locator.value.source_ref, locator: locator.value.locator, item_type: form.itemType, quantity: Number(form.quantity), allocation_scope: form.allocation, is_per_unit: form.perUnit, observed_at: form.observed, valid_until: form.valid || null };
  }
  await confirm(body, (id, signal) => client.POST("/costing-quotes/price-evidence", { params: { header: { "Idempotency-Key": id } }, body, signal }), (data) => { records.value = [data, ...records.value.filter((item) => item.evidence_id !== data.evidence_id)]; emit("saved"); });
}
async function read(): Promise<void> {
  const op = begin("records"); if (!op?.valid()) return;
  try {
    const result = await client.GET("/costing-quotes/opportunities/{opportunity_id}/price-evidence", { params: { path: { opportunity_id: props.opportunityId } }, signal: op.signal });
    if (!op.valid()) return;
    if (result.data) records.value = result.data; else message.value = quoteError(result.response.status, result.error);
  } catch { if (op.valid()) message.value = "价格记录核对失败"; }
}
</script>
<template>
  <section class="panel">
    <h2>价格与费用证据</h2><p>原价须人工确认；原文读取独立授权。网页参考价不等于正式报价。</p>
    <div class="field-grid">
      <label>原件引用<input
        v-model="source"
        name="price-source"
      ></label><label>提取格式<select v-model="profile"><option value="pdf-text-v1">PDF 文本</option><option value="rfc822-plain-v1">邮件正文</option></select></label><label v-if="profile === 'pdf-text-v1'">页码<input
        v-model="page"
        name="price-page"
        inputmode="numeric"
      ></label>
    </div>
    <button
      :disabled="!hasIdentity || !source"
      @click="readPreview"
    >
      预览价格原文
    </button><p role="alert">
      {{ rawError }}
    </p>
    <div v-if="preview">
      <p>原件 {{ preview.raw_hash }} · 正文 {{ preview.text_hash }}</p><textarea
        name="price-preview"
        :value="preview.text"
        readonly
        rows="6"
        @select="selectText"
        @keyup="selectText"
        @mouseup="selectText"
      /><button
        :disabled="!selection"
        @click="locate"
      >
        定位价格选区
      </button>
    </div>
    <p v-if="locator">
      已核验选区：{{ locator.excerpt }} <code>{{ locator.locator }}</code>
    </p>
    <div class="field-grid">
      <label>依据类型<select
        v-model="form.kind"
        name="price-kind"
      ><option value="supplier_price">供应商价格</option><option value="confirmed_expense">费用依据</option></select></label>
      <label>价格性质<select
        v-model="form.basis"
        name="price-basis"
      ><option value="quoted">已报价</option><option
        v-if="form.kind === 'supplier_price'"
        value="indicative"
      >参考价</option><option
        v-else
        value="actual"
      >实际凭证</option></select></label>
      <label>原始金额<input
        v-model="form.amount"
        name="price-amount"
      ></label><label>原币种<input
        v-model="form.currency"
        name="price-currency"
      ></label>
      <template v-if="form.kind === 'supplier_price'">
        <label>计价单位<input
          v-model="form.unit"
          name="price-unit"
        ></label><label>最小起订量<input
          v-model="form.moq"
          name="price-moq"
        ></label><label>数量档下限<input
          v-model="form.min"
          name="price-min"
        ></label><label>数量档上限<input
          v-model="form.max"
          name="price-max"
        ></label><label>供应商引用<input
          v-model="form.supplier"
          name="price-supplier"
        ></label><label>原始规格<input
          v-model="form.spec"
          name="price-spec"
        ></label><label>目的地<input
          v-model="form.destination"
          name="price-destination"
        ></label>
      </template>
      <template v-else>
        <label>费用类型<select v-model="form.itemType"><option value="">请选择</option><option
          v-for="[name, label] in costItemLabels"
          :key="name"
          :value="name"
        >{{ label }}</option></select></label><label>数量<input v-model="form.quantity"></label><label>分摊范围<input v-model="form.allocation"></label><label><input
          v-model="form.perUnit"
          type="checkbox"
        >单件金额（否则为整单）</label>
      </template>
      <label>报价 / 观察时间（含时区）<input
        v-model="form.observed"
        name="price-observed"
      ></label><label>有效期（实际可空）<input
        v-model="form.valid"
        name="price-valid"
      ></label>
    </div>
    <button
      :disabled="!hasIdentity || !locator || pending"
      @click="save"
    >
      确认价格依据
    </button><button @click="read">
      核对已保存价格依据
    </button><p role="status">
      {{ message }} <code v-if="key">{{ key }}</code>
    </p>
    <article
      v-for="record in records"
      :key="record.evidence_id"
    >
      <h3>{{ record.evidence_id }} · {{ codeLabel(record.basis) }}</h3><p>{{ record.amount }} {{ record.currency }} · {{ record.source.source_ref }} · {{ record.evidence_hash }}</p><p>确认人 {{ record.confirmed_by }} · {{ record.confirmed_at }}</p><p v-if="record.kind === 'supplier_price'">
        {{ record.specification }} · {{ record.quantity_min }}–{{ record.quantity_max }} {{ record.unit }} · {{ record.destination }} · {{ record.valid_until }}
      </p><p v-else>
        {{ costItemLabel(record.item_type) }} · {{ record.allocation_scope }} · {{ record.is_per_unit ? '单件' : '整单' }} · {{ record.valid_until ?? '无有效期（实际凭证）' }}
      </p>
    </article>
  </section>
</template>
