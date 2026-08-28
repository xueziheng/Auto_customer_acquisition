<script setup lang="ts">
import { inject, reactive, ref, watch } from "vue";
import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";
import { costItemLabels } from "./quote-input";
import { quoteError, useQuoteConfirmation } from "./quote-request-scope";
const props = defineProps<{ sheet: components["schemas"]["CostSheetView"] }>();
const emit = defineEmits<{ saved: [value: components["schemas"]["CostCoveragePublicView"] | null] }>();
const client = inject<ReturnType<typeof createApiClient>>("tradeos-api-client", apiClient);
const mode = ref<"" | "summary" | "detail">("");
const choices = reactive<Record<string, "" | "yes" | "no">>({});
const reasons = reactive<Record<string, string>>({});
const bindings = reactive<Record<number, { evidence: string; line: string; allocation: string }>>({});
const coverage = ref<components["schemas"]["CostCoveragePublicView"] | null>(null);
const { begin, hasIdentity, confirm, message, key, pending } = useQuoteConfirmation(client, () => [props.sheet.cost_sheet_id, props.sheet.content_hash, mode.value, JSON.stringify(choices), JSON.stringify(reasons), JSON.stringify(bindings)], () => {
  mode.value = ""; coverage.value = null;
  for (const name of Object.keys(choices)) delete choices[name];
  for (const name of Object.keys(reasons)) delete reasons[name];
  for (const name of Object.keys(bindings)) delete bindings[Number(name)];
});
watch(() => props.sheet, (sheet) => {
  for (const item of sheet.items) if (item.item_sequence !== undefined && item.item_sequence !== null && !bindings[item.item_sequence]) bindings[item.item_sequence] = { evidence: "", line: "", allocation: "" };
}, { immediate: true });
async function read(): Promise<void> {
  const op = begin("read"); if (!op?.valid()) return;
  try {
    const result = await client.GET("/costing-quotes/cost-sheets/{sheet_id}/coverage", { params: { path: { sheet_id: props.sheet.cost_sheet_id } }, signal: op.signal });
    if (!op.valid()) return;
    if (result.response.ok) { coverage.value = result.data ?? null; emit("saved", coverage.value); message.value = coverage.value ? "已读取费用确认；仍须检查成本 hash" : "尚未确认适用清单"; }
    else message.value = quoteError(result.response.status, result.error);
  } catch { if (op.valid()) message.value = "费用适用清单读取失败"; }
}
async function save(): Promise<void> {
  if (!mode.value || costItemLabels.some(([name]) => !choices[name])) { message.value = "请明确获客口径和全部22类适用性；缺失不能当作零或不适用"; return; }
  const decisions: components["schemas"]["CostCoverageDecision"][] = [];
  for (const [name] of costItemLabels) {
    const item_bindings: components["schemas"]["CostItemBinding"][] = [];
    if (choices[name] === "yes") {
      for (const item of props.sheet.items.filter((entry) => entry.item_type === name)) {
        if (item.item_sequence == null) { message.value = "缺少持久成本项序号，不能按数组位置绑定"; return; }
        const binding = bindings[item.item_sequence];
        if (!binding) { message.value = "请填写每条原文费用行映射"; return; }
        item_bindings.push({ item_sequence: item.item_sequence, evidence_id: binding.evidence, source_line_ref: binding.line, allocation_scope: binding.allocation });
      }
    }
    decisions.push({ item_type: name, applicable: choices[name] === "yes", reason: reasons[name] ?? "", item_bindings });
  }
  const body: components["schemas"]["CostCoverageCreate"] = { acquisition_mode: mode.value, expected_sheet_hash: props.sheet.content_hash, decisions };
  await confirm(body, (id, signal) => client.POST("/costing-quotes/cost-sheets/{sheet_id}/coverage", { params: { path: { sheet_id: props.sheet.cost_sheet_id }, header: { "Idempotency-Key": id } }, body, signal }), (data) => { coverage.value = data; emit("saved", data); });
}
</script>
<template>
  <section class="panel">
    <h2>22 项成本适用清单</h2><p>缺失 ≠ 零金额 ≠ 不适用。零金额需原始依据；不适用需人工说明。旧 readiness 保持独立只读。</p>
    <label>获客归集口径<select
      v-model="mode"
      name="coverage-mode"
    ><option value="">请选择</option><option value="summary">汇总</option><option value="detail">明细</option></select></label>
    <article
      v-for="[name, label] in costItemLabels"
      :key="name"
      class="coverage-row"
    >
      <h3>{{ label }}</h3>
      <div class="field-grid">
        <label>适用性<select
          v-model="choices[name]"
          :name="`coverage-${name}`"
        ><option value="">未确认</option><option value="yes">适用（必须有金额依据）</option><option value="no">不适用（必须说明理由）</option></select></label><label>确认理由<input
          v-model="reasons[name]"
          :name="`reason-${name}`"
        ></label>
      </div>
      <div
        v-for="item in sheet.items.filter(entry => entry.item_type === name)"
        :key="item.item_sequence ?? item.source_ref ?? item.item_type"
      >
        <p>持久序号 {{ item.item_sequence ?? '缺失，禁止绑定' }} · {{ item.amount.amount }} {{ item.amount.currency }} · {{ item.is_per_unit ? '单件' : '整单' }} · {{ item.source_ref }}</p>
        <div
          v-if="item.item_sequence != null && bindings[item.item_sequence]"
          class="field-grid"
        >
          <label>依据 ID<input
            v-model="bindings[item.item_sequence]!.evidence"
            :name="`binding-evidence-${item.item_sequence}`"
          ></label><label>原文费用行引用<input
            v-model="bindings[item.item_sequence]!.line"
            :name="`binding-line-${item.item_sequence}`"
          ></label><label>分摊范围<input
            v-model="bindings[item.item_sequence]!.allocation"
            :name="`binding-allocation-${item.item_sequence}`"
          ></label>
        </div>
      </div><p v-if="!sheet.items.some(entry => entry.item_type === name)">
        未录入金额（不等于零）
      </p>
    </article>
    <button
      :disabled="!hasIdentity || pending || !sheet.content_hash"
      @click="save"
    >
      确认成本适用清单
    </button><button @click="read">
      核对已保存适用清单
    </button><p role="status">
      {{ message }} <code v-if="key">{{ key }}</code>
    </p>
    <div v-if="coverage">
      <p>{{ coverage.coverage_id }} · {{ coverage.content_hash }} · {{ coverage.confirmed_by }} · {{ coverage.confirmed_at }}</p><p
        v-if="coverage.expected_sheet_hash !== sheet.content_hash"
        role="alert"
      >
        成本内容已变化，旧适用清单失效，必须重新确认
      </p><p
        v-for="decision in coverage.decisions"
        :key="decision.item_type"
      >
        {{ decision.item_type }}：{{ decision.applicable ? '适用' : '不适用' }} · {{ decision.reason }}
      </p>
    </div>
  </section>
</template>
