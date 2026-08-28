<script setup lang="ts">
/* global Event, HTMLTextAreaElement */
import { inject, ref, watch } from "vue";
import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";
import { utf16SelectionToCodepoints } from "./quote-input";
import { quoteError, useQuoteConfirmation, useQuoteRequestScope } from "./quote-request-scope";
const props = defineProps<{ needId: string }>();
const emit = defineEmits<{ saved: [] }>();
const client = inject<ReturnType<typeof createApiClient>>("tradeos-api-client", apiClient);
const unit = ref(""); const source = ref(""); const rawError = ref("");
const current = ref<components["schemas"]["NeedUnitPreparationView"] | null>(null);
const receipt = ref<components["schemas"]["NeedUnitConfirmationPublicView"] | null>(null);
const preview = ref<components["schemas"]["EvidencePreviewPublicView"] | null>(null);
const locator = ref<components["schemas"]["EvidenceLocatorPublicView"] | null>(null);
const selection = ref<readonly [number, number] | null>(null);
const messageIdPattern = /^msg_[0-7][0-9A-HJKMNP-TV-Z]{25}$/;
function clearRaw(): void { preview.value = null; locator.value = null; selection.value = null; rawError.value = ""; }
const rawGate = useQuoteRequestScope(client, () => [props.needId, source.value, selection.value?.[0], selection.value?.[1]], clearRaw);
watch(() => [props.needId, source.value], clearRaw, { flush: "sync" });
watch(selection, () => { locator.value = null; }, { flush: "sync" });
const { begin, hasIdentity, confirm, message, key, pending } = useQuoteConfirmation(client, () => [props.needId, source.value, unit.value, locator.value?.locator, current.value?.quantity_fact_hash, current.value?.unit_confirmation_id], () => {
  unit.value = ""; source.value = ""; current.value = null; receipt.value = null; clearRaw();
});
async function read(): Promise<void> {
  const op = begin("read"); if (!op?.valid()) return;
  try {
    const result = await client.GET("/costing-quotes/needs/{need_id}/unit", { params: { path: { need_id: props.needId } }, signal: op.signal });
    if (!op.valid()) return;
    if (result.data) current.value = result.data; else message.value = quoteError(result.response.status, result.error);
  } catch { if (op.valid()) message.value = "客户单位核对失败"; }
}
async function readPreview(): Promise<void> {
  if (!messageIdPattern.test(source.value)) { clearRaw(); rawError.value = "请输入裸客户消息 ID（msg_…），不要填写来源前缀"; return; }
  clearRaw(); const op = rawGate.begin("preview"); if (!op?.valid()) return;
  try {
    const result = await client.POST("/costing-quotes/evidence/preview", { signal: op.signal, body: { operation: "preview", scope: { purpose: "need_unit", action: "confirm", need_id: props.needId }, source_ref: `message:${source.value}`, profile: "rfc822-plain-v1", page: null } });
    if (!op.valid()) return;
    if (result.data) preview.value = result.data; else rawError.value = quoteError(result.response.status, result.error);
  } catch { if (op.valid()) rawError.value = "客户消息读取失败；没有消息读取权不能确认单位"; }
}
function selectText(event: Event): void {
  const element = event.target; if (!(element instanceof HTMLTextAreaElement) || !preview.value) return;
  try {
    const next = utf16SelectionToCodepoints(preview.value.text, element.selectionStart, element.selectionEnd);
    if (selection.value?.[0] !== next[0] || selection.value?.[1] !== next[1]) selection.value = next;
  }
  catch { selection.value = null; locator.value = null; rawError.value = "请选择完整的客户原文字符"; }
}
async function locate(): Promise<void> {
  if (!preview.value || !selection.value) return;
  const op = rawGate.begin("locator"); if (!op?.valid()) return;
  const p = preview.value;
  try {
    const result = await client.POST("/costing-quotes/evidence/locator", { signal: op.signal, body: { operation: "locate", scope: p.scope, source_ref: p.source_ref, profile: p.profile, page: p.page, expected_raw_hash: p.raw_hash, expected_text_hash: p.text_hash, start: selection.value[0], end: selection.value[1] } });
    if (!op.valid()) return;
    if (result.data) {
      if (!messageIdPattern.test(source.value) || result.data.source_ref !== `message:${source.value}`) { locator.value = null; rawError.value = "定位结果与当前客户消息不一致"; return; }
      locator.value = result.data;
    } else rawError.value = quoteError(result.response.status, result.error);
  } catch { if (op.valid()) rawError.value = "客户原文定位失败"; }
}
async function save(): Promise<void> {
  if (!locator.value || !current.value?.quantity_fact_hash) return;
  if (!messageIdPattern.test(source.value) || locator.value.source_ref !== `message:${source.value}`) { message.value = "请重新核对当前客户消息来源"; return; }
  const body: components["schemas"]["NeedUnitConfirmationCommand"] = { unit: unit.value, source_message_id: locator.value.source_ref.slice("message:".length), source_quote: locator.value.excerpt, locator: locator.value.locator, expected_quantity_fact_hash: current.value.quantity_fact_hash, expected_unit_confirmation_id: current.value.unit_confirmation_id };
  await confirm(body, (id, signal) => client.POST("/costing-quotes/needs/{need_id}/unit-confirmations", { params: { path: { need_id: props.needId }, header: { "Idempotency-Key": id } }, body, signal }), (data) => { receipt.value = data; emit("saved"); });
}
watch(() => props.needId, () => void read(), { immediate: true });
</script>
<template>
  <section class="panel">
    <h2>客户单位确认</h2><p>仅从有读取权限的客户消息确认；不扩大收件箱权限。需求数量变更后必须重新核对确认。</p>
    <p v-if="current">
      数量 {{ current.quantity ?? '缺失' }} · 单位 {{ current.unit ?? '缺失' }} · {{ current.unit_status }} · 当前确认 {{ current.unit_confirmation_id ?? '无' }} · 数量 hash {{ current.quantity_fact_hash }}
    </p>
    <div class="field-grid">
      <label>客户消息 ID<input
        v-model="source"
        name="unit-source"
      ></label><label>明确计价单位<input
        v-model="unit"
        name="unit-value"
      ></label>
    </div>
    <button
      :disabled="!hasIdentity || !source"
      @click="readPreview"
    >
      预览客户消息
    </button><p role="alert">
      {{ rawError }}
    </p>
    <div v-if="preview">
      <textarea
        :value="preview.text"
        name="unit-preview"
        readonly
        rows="5"
        @select="selectText"
        @keyup="selectText"
        @mouseup="selectText"
      /><button
        :disabled="!selection"
        @click="locate"
      >
        定位客户单位原话
      </button>
    </div>
    <p v-if="locator">
      {{ locator.excerpt }} · {{ locator.locator }}
    </p>
    <button
      :disabled="!hasIdentity || pending || !locator || !current?.quantity_fact_hash || !unit"
      @click="save"
    >
      确认客户单位
    </button><button @click="read">
      核对已保存客户单位
    </button>
    <p role="status">
      {{ message }} <code v-if="key">{{ key }}</code>
    </p><p v-if="receipt">
      确认 ID {{ receipt.confirmation_id }} · {{ receipt.unit }} · {{ receipt.confirmed_by }} · {{ receipt.confirmed_at }} · {{ receipt.quantity_fact_hash }}
    </p>
  </section>
</template>
