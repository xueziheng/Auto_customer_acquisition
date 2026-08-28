<script setup lang="ts">
import { computed, inject, reactive, ref } from "vue";
import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";
import { quoteError, useQuoteConfirmation } from "./quote-request-scope";
const props = defineProps<{
  context: components["schemas"]["QuotePreparationPublicView"];
  sheet: components["schemas"]["CostSheetView"];
  coverage: components["schemas"]["CostCoveragePublicView"] | null;
  terms: components["schemas"]["QuoteTerm"][];
  validUntil: string;
  evidence: (components["schemas"]["SupplierPriceEvidencePublicView"] | components["schemas"]["ExpenseEvidencePublicView"])[];
}>();
const emit = defineEmits<{ selected: [value: components["schemas"]["CostScopePublicView"] | null] }>();
const client = inject<ReturnType<typeof createApiClient>>("tradeos-api-client", apiClient);
const notes = reactive<Record<string, string>>({});
const records = ref<components["schemas"]["CostScopePublicView"][]>([]);
const selected = ref<components["schemas"]["CostScopePublicView"] | null>(null);
const sourceIds = computed(() => [...new Set(props.coverage?.decisions.flatMap((item) => item.item_bindings.map((binding) => binding.evidence_id)) ?? [])]);
const { begin, hasIdentity, confirm, message, key, pending } = useQuoteConfirmation(client, () => [props.context.need_facts_hash, props.sheet.content_hash, props.coverage?.content_hash, JSON.stringify(props.terms), props.validUntil, JSON.stringify(notes), JSON.stringify(props.evidence.map((item) => [item.evidence_id, item.evidence_hash]))], () => {
  records.value = []; selected.value = null; Object.keys(notes).forEach((name) => delete notes[name]);
});
function choose(record: components["schemas"]["CostScopePublicView"]): void { selected.value = record; emit("selected", record); }
function stale(record: components["schemas"]["CostScopePublicView"]): boolean {
  return record.need_facts_hash !== props.context.need_facts_hash || record.sheet_hash !== props.sheet.content_hash || record.coverage_hash !== props.coverage?.content_hash || new Date(record.valid_until).getTime() !== new Date(props.validUntil).getTime() || JSON.stringify(record.terms) !== JSON.stringify(props.terms)
    || record.evidence_bindings.some((binding) => !props.evidence.some((item) => item.evidence_id === binding.evidence_id && item.evidence_hash === binding.evidence_hash));
}
async function save(): Promise<void> {
  if (!props.coverage) { message.value = "请先读取或确认成本适用清单"; return; }
  const evidence_bindings: components["schemas"]["CostScopeEvidenceBinding"][] = [];
  for (const id of sourceIds.value) {
    const evidence = props.evidence.find((item) => item.evidence_id === id);
    if (!evidence || !notes[id]?.trim()) { message.value = "请核对每项持久依据，并填写人工适用映射说明"; return; }
    evidence_bindings.push({ evidence_id: id, evidence_hash: evidence.evidence_hash, applicability_note: notes[id]! });
  }
  const body: components["schemas"]["CostScopeConfirmationCommand"] = { coverage_id: props.coverage.coverage_id, expected_coverage_hash: props.coverage.content_hash, expected_need_facts_hash: props.context.need_facts_hash, expected_sheet_hash: props.sheet.content_hash, evidence_bindings, terms: props.terms.map((term) => ({ ...term })), valid_until: props.validUntil };
  await confirm(body, (id, signal) => client.POST("/costing-quotes/cost-sheets/{sheet_id}/scope-confirmations", { params: { path: { sheet_id: props.sheet.cost_sheet_id }, header: { "Idempotency-Key": id } }, body, signal }), (data) => { records.value = [data, ...records.value.filter((item) => item.confirmation_id !== data.confirmation_id)]; choose(data); });
}
async function read(): Promise<void> {
  const op = begin("read"); if (!op?.valid()) return;
  try {
    const result = await client.GET("/costing-quotes/cost-sheets/{sheet_id}/scope-confirmations", { params: { path: { sheet_id: props.sheet.cost_sheet_id } }, signal: op.signal });
    if (!op.valid()) return;
    if (result.data) records.value = result.data; else message.value = quoteError(result.response.status, result.error);
  } catch { if (op.valid()) message.value = "适用性确认核对失败"; }
}
</script>
<template>
  <section class="panel">
    <h2>成本适用性确认</h2><p>确认完整目标需求、条款、期限与每项成本来源的人工映射；不是供应商原话。</p>
    <dl>
      <div><dt>产品 / 规格</dt><dd>{{ context.specification.product_category }} · {{ context.specification.material ?? '材料缺失' }} · {{ context.specification.size_spec ?? '尺寸缺失' }}</dd></div><div><dt>用途 / 包装 / 认证</dt><dd>{{ context.specification.application ?? '用途缺失' }} · {{ context.specification.packaging ?? '包装缺失' }} · {{ context.specification.certification_required ?? '认证缺失' }}</dd></div><div><dt>数量 / 单位</dt><dd>{{ context.need.quantity }} · {{ context.need.unit ?? '单位缺失' }}</dd></div><div><dt>目的地 / 时间</dt><dd>{{ context.need.destination ?? '目的地缺失' }} · {{ context.need.required_by ?? '时间要求缺失' }}</dd></div><div><dt>完整需求 hash</dt><dd>{{ context.need_facts_hash }}</dd></div><div><dt>报价期限</dt><dd>{{ validUntil || '尚未填写' }}</dd></div><div
        v-for="term in terms"
        :key="term.kind"
      >
        <dt>{{ term.kind }}</dt><dd>{{ term.text }}</dd>
      </div>
    </dl>
    <label
      v-for="id in sourceIds"
      :key="id"
    >依据 {{ id }} · {{ evidence.find(item => item.evidence_id === id)?.source.source_ref ?? '尚未读取依据' }} 的人工适用说明<textarea
      v-model="notes[id]"
      :name="`scope-note-${id}`"
      rows="2"
    /></label>
    <button
      :disabled="!hasIdentity || pending || !coverage || !validUntil"
      @click="save"
    >
      确认成本适用性
    </button><button @click="read">
      核对已保存适用性确认
    </button><p role="status">
      {{ message }} <code v-if="key">{{ key }}</code>
    </p>
    <p v-if="selected">
      选择的确认 ID {{ selected.confirmation_id }} <strong v-if="stale(selected)">绑定已变化，旧确认失效；请人工重新确认</strong>
    </p>
    <article
      v-for="record in records"
      :key="record.confirmation_id"
    >
      <p>{{ record.confirmation_id }} · {{ record.content_hash }} · {{ record.provenance.confirmed_by }} · {{ record.provenance.confirmed_at }}</p><p>{{ record.specification }} · {{ record.valid_until }}</p><p
        v-for="binding in record.evidence_bindings"
        :key="binding.evidence_id"
      >
        {{ binding.evidence_id }} · {{ binding.applicability_note }}
      </p><button
        :disabled="stale(record)"
        @click="choose(record)"
      >
        使用此已保存确认
      </button>
    </article>
  </section>
</template>
