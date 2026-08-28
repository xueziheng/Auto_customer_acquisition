<script setup lang="ts">
import { inject, reactive, ref } from "vue";
import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";
import { costItemLabels } from "./quote-input";
import { quoteError, useQuoteConfirmation } from "./quote-request-scope";

const client = inject<ReturnType<typeof createApiClient>>("tradeos-api-client", apiClient);
const empty = () => ({ target: "", minimum: "", category: "", source: "", effective: "" });
const form = reactive(empty());
const groups = reactive<Record<string, "" | "goods" | "variable" | "fixed">>({});
const saved = ref<components["schemas"]["PricingPolicyPublicView"] | null>(null);
const { begin, hasIdentity, confirm, message, key, pending } = useQuoteConfirmation(client, () => [JSON.stringify(form), JSON.stringify(groups)], () => {
  Object.assign(form, empty()); Object.keys(groups).forEach((name) => delete groups[name]); saved.value = null;
});
async function save(): Promise<void> {
  const cost_groups: components["schemas"]["PricingPolicyCreate"]["cost_groups"] = {};
  for (const [name] of costItemLabels) {
    const group = groups[name];
    if (!group) { message.value = "请明确全部22类费用归类"; return; }
    cost_groups[name] = group;
  }
  const body: components["schemas"]["PricingPolicyCreate"] = {
    cost_groups, target_margin_rate: form.target.trim(), minimum_margin_rate: form.minimum.trim(),
    category: form.category.trim() || null, source_ref: form.source.trim(), effective_from: form.effective.trim(),
  };
  await confirm(body, (id, signal) => client.POST("/costing-quotes/policies", {
    params: { header: { "Idempotency-Key": id } }, body, signal,
  }), (data) => { saved.value = data; });
}
async function read(): Promise<void> {
  const op = begin("read"); if (!op?.valid()) return;
  try {
    const result = await client.GET("/costing-quotes/policies", { params: { query: { category: form.category || null } }, signal: op.signal });
    if (!op.valid()) return;
    if (result.data) saved.value = result.data;
    else message.value = quoteError(result.response.status, result.error);
  } catch { if (op.valid()) message.value = "政策读取失败；未恢复任何丢失的客户端键"; }
}
</script>
<template>
  <section class="panel">
    <h2>老板定价政策</h2><p>利润率与22类成本归类须由老板明确确认；页面不提供默认商业参数。</p>
    <div class="field-grid">
      <label>目标利润率（十进制比例）<input
        v-model="form.target"
        name="policy-target"
      ></label>
      <label>最低利润率（十进制比例）<input
        v-model="form.minimum"
        name="policy-minimum"
      ></label>
      <label>品类（留空为通用）<input
        v-model="form.category"
        name="policy-category"
      ></label>
      <label>政策来源引用<input
        v-model="form.source"
        name="policy-source"
      ></label>
      <label>生效时间（含时区）<input
        v-model="form.effective"
        name="policy-effective"
        placeholder="ISO 8601"
      ></label>
      <label
        v-for="[name, label] in costItemLabels"
        :key="name"
      >{{ label }}
        <select
          v-model="groups[name]"
          :name="`policy-group-${name}`"
        ><option value="">请选择</option><option value="goods">货物成本</option><option value="variable">变动成本</option><option value="fixed">固定成本</option></select>
      </label>
    </div>
    <button
      :disabled="pending || !hasIdentity"
      @click="save"
    >
      老板确认政策
    </button>
    <button @click="read">
      核对已保存政策
    </button>
    <p role="status">
      {{ message }} <code v-if="key">幂等键 {{ key }}</code>
    </p>
    <dl v-if="saved">
      <div><dt>政策 / hash</dt><dd>{{ saved.policy_id }} / {{ saved.content_hash }}</dd></div><div><dt>目标 / 底线比例</dt><dd>{{ saved.target_margin_rate }} / {{ saved.minimum_margin_rate }}</dd></div><div><dt>确认来源</dt><dd>{{ saved.source?.source_ref }} · {{ saved.confirmed_by }} · {{ saved.confirmed_at }}</dd></div>
    </dl>
  </section>
</template>
