<script setup lang="ts">
import { inject, onMounted, reactive, ref, watch } from "vue";
import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";
import { useQuoteRequestScope } from "../costing-quotes/quote-request-scope";

type Command = components["schemas"]["CustomerPreparationCommand"];
type View = components["schemas"]["CustomerPreparationView"];
type Basis = components["schemas"]["LegalBasisInput"];
const props = defineProps<{ allocation: components["schemas"]["CustomerAllocationView"]; refreshRevision?: number }>();
const emit = defineEmits<{ saved: [view: View]; forbidden: [] }>();
const client = inject<ReturnType<typeof createApiClient>>("tradeos-api-client", apiClient);
const state = ref<View | null>(null), loading = ref(false), busy = ref(false), notice = ref("");
const uncertain = ref<Command | null>(null);
const form = reactive({ entity_type: "", industry: "", source_url: "", page_hash: "", snapshot_artifact_ref: "",
  observed_at: "", public_observation: "", product_category: "", fit_reasoning: "", public_email: "",
  legal_basis: "" as Basis["basis"] | "", legal_basis_source: "", assessment_ref: "",
  subject_type: "" as Basis["subject_type"] | "", contact_type: "" as Basis["contact_type"] | "",
  legal_basis_confirmed: false });
function reset() {
  state.value = null; loading.value = false; busy.value = false; notice.value = ""; uncertain.value = null;
  Object.assign(form, { entity_type: "", industry: "", source_url: "", page_hash: "", snapshot_artifact_ref: "",
    observed_at: "", public_observation: "", product_category: "", fit_reasoning: "", public_email: "",
    legal_basis: "", legal_basis_source: "", assessment_ref: "", subject_type: "", contact_type: "", legal_basis_confirmed: false });
}
const gate = useQuoteRequestScope(client, () => [props.allocation.id], reset);
function forbidden() { gate.invalidate(); reset(); emit("forbidden"); notice.value = "当前身份无权准备该客户资料。"; }
async function load() {
  if (!gate.hasIdentity.value || busy.value) return;
  const operation = gate.begin("read"); if (!operation?.valid()) return;
  loading.value = true; notice.value = "";
  try {
    const result = await client.GET("/prospects/customers/{allocation_id}/preparation", {
      params: { path: { allocation_id: props.allocation.id } }, signal: operation.signal, cache: "no-store" });
    if (!operation.valid()) return;
    if (result.response.ok && result.data?.allocation_id === props.allocation.id) {
      state.value = result.data;
      form.entity_type = result.data.account.entity_type ?? ""; form.industry = result.data.account.industry ?? "";
      if (!form.source_url) {
        form.source_url = props.allocation.candidate.source_url;
        form.observed_at = props.allocation.candidate.observed_at;
        const source = props.allocation.candidate.provenance;
        if (source.source_type === "web_page"
          && /^art_[0-7][0-9A-HJKMNP-TV-Z]{25}$/.test(source.source_id)
          && /^[0-9a-f]{64}$/.test(source.page_hash ?? "")) {
          form.snapshot_artifact_ref = source.source_id;
          form.page_hash = source.page_hash!;
        }
        form.public_observation = props.allocation.candidate.evidence_text;
      }
    } else if ([401, 403].includes(result.response.status)) forbidden();
    else { state.value = null; notice.value = "资料准备读取失败，请重新读取。"; }
  } catch { if (operation.valid()) { state.value = null; notice.value = "资料准备读取失败，请重新读取。"; } }
  finally { if (operation.valid()) loading.value = false; }
}
function command(): Command | null {
  if (![form.entity_type, form.industry, form.source_url, form.page_hash, form.snapshot_artifact_ref,
    form.observed_at, form.public_observation, form.product_category, form.fit_reasoning].every(v => v.trim())) {
    notice.value = "请补齐公开来源凭证、客户类型/行业与产品适配推断。"; return null;
  }
  if (form.public_email && (!form.legal_basis || !form.legal_basis_source.trim() || !form.assessment_ref.trim()
    || !form.subject_type || !form.contact_type || !form.legal_basis_confirmed)) {
    notice.value = "录入邮箱前，请填写实际持有的处理依据和评估记录引用，并确认已查阅。"; return null;
  }
  return {
    entity_type: form.entity_type, industry: form.industry, source_url: form.source_url,
    page_hash: form.page_hash, snapshot_artifact_ref: form.snapshot_artifact_ref, observed_at: form.observed_at,
    public_observation: form.public_observation, product_category: form.product_category, fit_reasoning: form.fit_reasoning,
    public_email: form.public_email || null, legal_basis_confirmed: Boolean(form.public_email && form.legal_basis_confirmed),
    legal_basis: form.public_email && form.legal_basis && form.subject_type && form.contact_type ? {
      basis: form.legal_basis, subject_type: form.subject_type, contact_type: form.contact_type,
      source: form.legal_basis_source, assessment_ref: form.assessment_ref, source_url: form.source_url,
      collected_at: form.observed_at } : null,
  };
}
async function save(retry = false) {
  if (busy.value || loading.value || !state.value?.can_prepare) return;
  const body = retry ? uncertain.value : command(); if (!body || (!retry && uncertain.value)) return;
  const operation = gate.begin("save"); if (!operation?.valid()) return;
  busy.value = true; notice.value = "正在保存资料…";
  try {
    const result = await client.POST("/prospects/customers/{allocation_id}/preparation", {
      params: { path: { allocation_id: props.allocation.id } }, body, signal: operation.signal });
    if (!operation.valid()) return;
    if (result.response.ok && result.data?.allocation_id === props.allocation.id) {
      state.value = result.data; uncertain.value = null; emit("saved", result.data);
      notice.value = "资料已保存。需求仍是推断；新录入邮箱仍未验证。";
    } else if ([401, 403].includes(result.response.status)) forbidden();
    else if (result.response.status >= 500) {
      uncertain.value = body; notice.value = "保存结果待核对，请重新读取；需要重试时显式重试原资料。";
    } else {
      uncertain.value = null;
      notice.value = result.error?.message ?? "资料未保存，请核对来源凭证、现有字段与真实处理依据。";
    }
  } catch { if (operation.valid()) { uncertain.value = body; notice.value = "保存结果待核对，请重新读取；不会自动重发。"; } }
  finally { if (operation.valid()) busy.value = false; }
}
watch(() => props.allocation.id, () => { reset(); void load(); });
watch(() => props.refreshRevision, () => void load());
onMounted(() => void load());
</script>
<template>
  <section class="preparation-card">
    <div class="preparation-heading">
      <h2>准备客户资料</h2>
      <button
        :disabled="busy || loading"
        @click="load"
      >
        重新读取
      </button>
    </div>
    <p>公开资料用于说明适配理由；客户是否有采购需求，需要沟通验证。</p>
    <p
      v-if="loading"
      role="status"
    >
      正在读取资料…
    </p>
    <p
      v-if="notice"
      role="status"
    >
      {{ notice }}
    </p>
    <p v-if="state && !state.can_prepare">
      仅当前负责人可以准备这位客户的资料。
    </p>
    <form
      v-if="state?.can_prepare"
      @submit.prevent="save()"
    >
      <fieldset :disabled="busy || Boolean(uncertain)">
        <legend>公开事实与来源</legend>
        <div class="preparation-fields">
          <label>客户类型<input
            v-model="form.entity_type"
            name="entity_type"
            placeholder="按公开来源确认，例如 distributor"
          ></label>
          <label>行业<input
            v-model="form.industry"
            name="industry"
          ></label>
          <label class="wide">公开来源网址<input
            v-model="form.source_url"
            name="source_url"
            type="url"
          ></label>
          <label class="wide">公开来源原话<textarea
            v-model="form.public_observation"
            name="public_observation"
            rows="3"
          /></label>
          <label>观察时间（含时区）<input
            v-model="form.observed_at"
            name="observed_at"
          ></label>
        </div>
        <details class="evidence-receipt">
          <summary>来源凭证</summary>
          <p>已有候选的网页凭证会自动填入。缺少凭证时需补充本企业保存的真实网页快照；来源不存在、跨企业或内容哈希不匹配时，整个保存会被拒绝。</p>
          <label>网页快照编号<input
            v-model="form.snapshot_artifact_ref"
            name="snapshot_artifact_ref"
          ></label>
          <label>网页内容哈希<input
            v-model="form.page_hash"
            name="page_hash"
          ></label>
        </details>
      </fieldset>
      <fieldset :disabled="busy || Boolean(uncertain)">
        <legend>产品适配 · 员工推断</legend>
        <label>产品类别<input
          v-model="form.product_category"
          name="product_category"
        ></label>
        <label>适配理由<textarea
          v-model="form.fit_reasoning"
          name="fit_reasoning"
          rows="3"
          placeholder="根据哪些公开事实判断可能适配？还需向客户验证什么？"
        /></label>
        <p class="inference">
          将保存为有证据的需求假设，不代表客户明确表达过采购需求。
        </p>
      </fieldset>
      <fieldset :disabled="busy || Boolean(uncertain)">
        <legend>公开职务邮箱 · 可选</legend>
        <p>可以先保存公开资料与假设。需要录入邮箱时，填写实际持有的处理依据；公开邮箱本身不代表同意。</p>
        <label>公开邮箱<input
          v-model="form.public_email"
          name="public_email"
          type="email"
          placeholder="留空可先保存资料与假设"
        ></label>
        <template v-if="form.public_email">
          <div class="preparation-fields">
            <label>处理依据<select
              v-model="form.legal_basis"
              name="legal_basis"
            ><option value="">请选择真实依据</option><option value="legitimate_interest">正当利益（已有评估）</option><option value="consent">已取得同意</option><option value="existing_customer">既有客户关系</option></select></label>
            <label>依据来源<input
              v-model="form.legal_basis_source"
              name="legal_basis_source"
            ></label>
            <label>主体类型<select
              v-model="form.subject_type"
              name="subject_type"
            ><option value="">请选择</option><option value="legal_entity">法人企业</option></select></label>
            <label>邮箱类型<select
              v-model="form.contact_type"
              name="contact_type"
            ><option value="">请选择</option><option value="role_based">公开职务邮箱</option></select></label>
            <label class="wide">实际评估 / 依据记录引用<input
              v-model="form.assessment_ref"
              name="assessment_ref"
              placeholder="必须是能查阅的真实记录，不能填写占位引用"
            ></label>
          </div>
          <label class="confirmation"><input
            v-model="form.legal_basis_confirmed"
            name="legal_basis_confirmed"
            type="checkbox"
          >我已查阅真实依据和评估记录，确认适用于这次录入。</label>
          <p>录入后的邮箱仍为未验证状态；可达性必须经过独立验证。</p>
        </template>
      </fieldset>
      <button
        data-action="prepare"
        type="submit"
        :disabled="busy || loading || Boolean(uncertain)"
      >
        保存资料与适配推断
      </button>
      <button
        v-if="uncertain"
        type="button"
        :disabled="busy || loading"
        @click="save(true)"
      >
        重试原资料保存
      </button>
    </form>
    <div
      v-if="state?.hypotheses.length"
      class="saved-inferences"
    >
      <h3>已保存的需求假设 · 推断</h3>
      <p
        v-for="hypothesis in state.hypotheses"
        :key="hypothesis.hypothesis_id"
      >
        {{ hypothesis.category }} · 需求待沟通验证
      </p>
    </div>
  </section>
</template>
<style scoped>
.preparation-card { padding: 20px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface); display: grid; gap: 12px; min-width: 0; overflow-wrap: anywhere; }
.preparation-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
h2 { font-size: 18px; } h3 { font-size: 16px; }
form { display: grid; gap: 16px; min-width: 0; } fieldset { padding: 14px; border: 1px solid var(--border); min-width: 0; display: grid; gap: 12px; }
legend { padding: 0 6px; font-weight: 600; } label { display: grid; gap: 6px; min-width: 0; }
.preparation-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.wide { grid-column: 1 / -1; } input, textarea, select { width: 100%; min-width: 0; box-sizing: border-box; }
textarea { resize: vertical; } .confirmation { display: flex; align-items: flex-start; } .confirmation input { width: auto; margin-top: 4px; }
.evidence-receipt { display: grid; gap: 10px; } .evidence-receipt label, .evidence-receipt p { margin-top: 10px; }
.inference { color: var(--warning); } form > button { justify-self: start; }
@media (max-width: 600px) { .preparation-fields { grid-template-columns: minmax(0, 1fr); } .preparation-card { padding: 14px; } }
</style>
