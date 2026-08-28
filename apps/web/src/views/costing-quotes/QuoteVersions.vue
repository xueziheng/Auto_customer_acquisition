<script setup lang="ts">
/* global URL, document */
import { computed, inject, ref, watch } from "vue";
import { useRouter } from "vue-router";
import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";
import { quoteError, useQuoteRequestScope } from "./quote-request-scope";
import { profitMetricLabels } from "./quote-input";
const props = defineProps<{ opportunityId: string; quoteId: string }>();
const emit = defineEmits<{ selected: [quote: components["schemas"]["QuoteInternalPublicView"] | null] }>();
const client = inject<ReturnType<typeof createApiClient>>("tradeos-api-client", apiClient);
const router = useRouter();
const internal = ref<components["schemas"]["QuoteInternalPublicView"][]>([]);
const customer = ref<components["schemas"]["QuoteCustomerVersionView"][]>([]);
const current = ref<components["schemas"]["QuoteInternalPublicView"] | null>(null);
const files = ref<components["schemas"]["QuoteFileView"][]>([]);
const next = ref<number | null>(null);
const internalError = ref(""); const fileError = ref(""); const notice = ref("");
const originalCall = ref(""); const working = ref(false); const blobUrl = ref(""); const previewing = ref(false);
const recoveryCall = ref("");
let recoveryGeneration = 0;
let recovering = false;
watch(originalCall, () => {
  recoveryGeneration += 1;
  recoveryCall.value = "";
  if (recovering) { working.value = false; recovering = false; notice.value = "原恢复操作结果待核对；调用引用已变化，不自动重发"; }
}, { flush: "sync" });
function revoke(): void { if (blobUrl.value) URL.revokeObjectURL(blobUrl.value); blobUrl.value = ""; previewing.value = false; }
function reset(): void { recovering = false; internal.value = []; customer.value = []; current.value = null; files.value = []; next.value = null; internalError.value = ""; fileError.value = ""; notice.value = ""; originalCall.value = ""; recoveryCall.value = ""; working.value = false; revoke(); }
const { begin, hasIdentity } = useQuoteRequestScope(client, () => [props.opportunityId, props.quoteId], reset);
const stateLabels: Record<components["schemas"]["QuoteState"], string> = { draft: "草稿", pending_approval: "等待审批", approved: "已批准", expired: "已过期", superseded: "已被新版替代", rejected: "已否决", sent: "历史已发送", accepted: "历史已接受" };
const selectedCustomer = computed(() => customer.value.find((item) => item.quote_id === props.quoteId));
function allowed(action: components["schemas"]["QuoteCustomerVersionView"]["allowed_actions"][number]): boolean {
  return !selectedCustomer.value || selectedCustomer.value.allowed_actions.includes(action);
}
async function loadInternal(): Promise<void> {
  if (!props.opportunityId) return;
  const op = begin("internal-list"); if (!op?.valid()) return;
  try {
    const result = await client.GET("/costing-quotes/opportunities/{opportunity_id}/quotes", { params: { path: { opportunity_id: props.opportunityId } }, signal: op.signal });
    if (!op.valid()) return;
    if (result.data) internal.value = result.data; else internalError.value = quoteError(result.response.status, result.error);
  } catch { if (op.valid()) internalError.value = "内部版本读取失败"; }
}
async function loadCustomer(before?: number): Promise<void> {
  if (!props.opportunityId) return;
  const op = begin("customer-list"); if (!op?.valid()) return;
  try {
    const result = await client.GET("/costing-quotes/opportunities/{opportunity_id}/customer-quote-versions", { params: { path: { opportunity_id: props.opportunityId }, query: { limit: 20, before_version: before } }, signal: op.signal });
    if (!op.valid()) return;
    if (result.data) { customer.value = before ? [...customer.value, ...result.data.items] : result.data.items; next.value = result.data.next_before_version; fileError.value = ""; }
    else fileError.value = quoteError(result.response.status, result.error);
  } catch { if (op.valid()) fileError.value = "客户版本读取失败，并非空数据"; }
}
async function loadQuote(): Promise<void> {
  if (!props.quoteId) return;
  const op = begin("quote"); if (!op?.valid()) return;
  try {
    const result = await client.GET("/costing-quotes/quotes/{quote_id}", { params: { path: { quote_id: props.quoteId } }, signal: op.signal });
    if (!op.valid()) return;
    if (result.data) { current.value = result.data; emit("selected", result.data); internalError.value = ""; }
    else internalError.value = quoteError(result.response.status, result.error);
  } catch { if (op.valid()) internalError.value = "指定报价版本读取失败"; }
}
async function loadFiles(): Promise<void> {
  if (!props.quoteId) return;
  const op = begin("files"); if (!op?.valid()) return;
  try {
    const result = await client.GET("/costing-quotes/quotes/{quote_id}/files", { params: { path: { quote_id: props.quoteId } }, signal: op.signal });
    if (!op.valid()) return;
    if (result.data) { files.value = result.data; fileError.value = ""; }
    else fileError.value = quoteError(result.response.status, result.error);
  } catch { if (op.valid()) fileError.value = "客户文件列表读取失败，并非没有文件"; }
}
function refresh(): void { void loadInternal(); void loadCustomer(); void loadQuote(); void loadFiles(); }
async function fileAction(kind: "generate" | "reconcile"): Promise<void> {
  if (!props.quoteId || !hasIdentity.value || working.value) return;
  const op = begin("file-action"); if (!op?.valid()) return;
  const recoveryVersion = recoveryGeneration;
  const valid = () => op.valid() && (kind !== "reconcile" || recoveryVersion === recoveryGeneration);
  recovering = kind === "reconcile";
  working.value = true; fileError.value = ""; recoveryCall.value = "";
  try {
    const result = kind === "generate"
      ? await client.POST("/costing-quotes/quotes/{quote_id}/files", { params: { path: { quote_id: props.quoteId } }, body: {}, signal: op.signal })
      : await client.POST("/costing-quotes/quotes/{quote_id}/files/reconcile", { params: { path: { quote_id: props.quoteId } }, body: { quote_id: props.quoteId, original_generation_call_id: originalCall.value }, signal: op.signal });
    if (!valid()) return;
    if (result.data) {
      if ("outcome" in result.data) { files.value = [result.data.file]; recoveryCall.value = result.data.recovery_call_id; notice.value = "仅恢复 metadata 关联；未证明文件 bytes 可读，原调用仍未决"; }
      else { files.value = [result.data]; notice.value = "文件已生成；未发送"; }
    } else {
      fileError.value = quoteError(result.response.status, result.error);
      if (result.error && "original_generation_call_id" in result.error) {
        recovering = false; working.value = false;
        originalCall.value = result.error.original_generation_call_id ?? originalCall.value;
        if (kind === "reconcile") recoveryCall.value = result.error.tool_call_id ?? "";
      }
    }
  } catch { if (valid()) fileError.value = "文件请求结果未知，待核对。请读取文件记录；不自动生成、不伪造原调用 ID"; }
  finally { if (valid()) { working.value = false; recovering = false; } }
}
async function download(file: components["schemas"]["QuoteFileView"], history = false, preview = false): Promise<void> {
  const op = begin("pdf"); if (!op?.valid()) return;
  revoke();
  try {
    const result = history
      ? await client.GET("/costing-quotes/quotes/{quote_id}/files/{file_id}/history", { params: { path: { quote_id: file.quote_id, file_id: file.file_id } }, parseAs: "blob", signal: op.signal })
      : await client.GET("/costing-quotes/quotes/{quote_id}/files/{file_id}", { params: { path: { quote_id: file.quote_id, file_id: file.file_id } }, parseAs: "blob", signal: op.signal });
    if (!op.valid()) return;
    if (!result.data) { fileError.value = quoteError(result.response.status, result.error); return; }
    blobUrl.value = URL.createObjectURL(result.data); previewing.value = preview;
    if (!preview && op.valid()) {
      const link = document.createElement("a"); link.href = blobUrl.value; link.download = `quote-${file.quote_version}${history ? "-history" : ""}.pdf`; link.click();
    }
  } catch { if (op.valid()) fileError.value = "文件读取失败；不自动改走历史接口"; }
}
async function submit(): Promise<void> {
  if (!props.quoteId || !hasIdentity.value || working.value) return;
  const op = begin("submit"); if (!op?.valid()) return;
  working.value = true;
  try {
    const result = await client.POST("/costing-quotes/quotes/{quote_id}/submit", { params: { path: { quote_id: props.quoteId } }, body: {}, signal: op.signal });
    if (!op.valid()) return;
    if (result.data) { notice.value = `已提交审批，尚未批准、更未发送。Run ${result.data.run_id}`; void loadQuote(); }
    else internalError.value = quoteError(result.response.status, result.error);
  } catch { if (op.valid()) internalError.value = "审批提交结果未知，待核对指定版本与 Run"; }
  finally { if (op.valid()) working.value = false; }
}
watch(() => [props.opportunityId, props.quoteId], () => { reset(); emit("selected", null); refresh(); }, { immediate: true, flush: "sync" });
</script>
<template>
  <section class="panel">
    <h2>报价版本与审批</h2><p>草稿 → 等待审批 → 批准与文件交付；批准不发送，没有自动发送入口。</p><button @click="refresh">
      核对报价与文件记录
    </button>
    <p
      v-if="internalError"
      role="alert"
    >
      内部报价：{{ internalError }}
    </p><p
      v-if="fileError"
      role="alert"
    >
      客户文件：{{ fileError }}
    </p><p role="status">
      {{ notice }}
    </p>
    <p v-if="!internal.length && !current && !internalError">
      暂无已加载内部报价
    </p>
    <button
      v-for="quote in internal"
      :key="quote.quote_id"
      @click="router.push(`/costing-quotes/quotes/${quote.quote_id}`)"
    >
      V{{ quote.version }} · {{ stateLabels[quote.state] }} · {{ quote.quote_id }} · 成本 {{ quote.cost_sheet_id }}
    </button>
    <article
      v-if="current"
      class="internal-quote"
    >
      <h3>指定版本 V{{ current.version }} · {{ stateLabels[current.state] }}</h3><p>{{ current.quote_id }} · {{ current.content_hash }}</p><p>贸易机会 {{ current.opportunity_id }} · 成本 {{ current.cost_sheet_id }} · basis {{ current.basis_id }} / {{ current.basis_hash }}</p><p>有效期 {{ current.valid_until }} · 起草 {{ current.prepared_by }} · 负责人 {{ current.owner_id }}</p><p v-if="current.replaces_quote_id">
        替代前版 {{ current.replaces_quote_id }} / V{{ current.replaced_quote_version }}；前版停用，不覆盖成本引用
      </p>
      <dl>
        <div><dt>客户单价 / 整单</dt><dd>{{ current.calculation.displayed_unit_price.amount }} {{ current.calculation.displayed_unit_price.currency }} / {{ current.calculation.displayed_total.amount }} {{ current.calculation.displayed_total.currency }}</dd></div><div><dt>核算有效单价</dt><dd>{{ current.calculation.effective_unit_revenue.amount }} {{ current.calculation.effective_unit_revenue.currency }}</dd></div><div
          v-for="(value, name) in current.calculation.metrics"
          :key="name"
        >
          <dt>{{ profitMetricLabels[name] ?? name }}</dt><dd>{{ value }}</dd>
        </div>
      </dl>
      <button
        :disabled="!hasIdentity || working || current.state !== 'draft'"
        @click="submit"
      >
        提交此版本审批
      </button>
    </article>
    <section aria-label="客户文件（不含内部成本）">
      <h3>客户用途版本与文件</h3>
      <article
        v-for="quote in customer"
        :key="quote.quote_id"
      >
        <h4>V{{ quote.version }} · {{ stateLabels[quote.state] }} · {{ quote.quote_id }}</h4><p>有效期 {{ quote.valid_until }} {{ quote.is_past_valid_until ? '已过期' : '' }}</p><p>后端 allowed_actions：{{ quote.allowed_actions.join(' / ') || '无' }}</p><p
          v-for="blocker in quote.blockers"
          :key="`${blocker.action}-${blocker.code}`"
        >
          {{ blocker.action }}：{{ blocker.code }}
        </p><button @click="router.push(`/costing-quotes/quotes/${quote.quote_id}`)">
          查看指定版本
        </button><div
          v-for="entry in quote.files"
          :key="entry.file.file_id"
        >
          <span>{{ entry.file.file_id }} · V{{ entry.file.quote_version }}</span><button
            v-if="entry.allowed_actions.includes('download_current')"
            @click="download(entry.file)"
          >
            下载当前文件
          </button><button
            v-if="entry.allowed_actions.includes('read_history')"
            @click="download(entry.file, true)"
          >
            下载历史文件（单独授权）
          </button>
        </div>
      </article>
      <button
        v-if="next !== null"
        @click="loadCustomer(next)"
      >
        更多客户版本
      </button>
      <div v-if="quoteId">
        <p>通知深链指定 {{ quoteId }}；即使无内部成本读取权，文件仍独立授权。</p><button
          :disabled="!hasIdentity || working || !allowed('generate')"
          @click="fileAction('generate')"
        >
          请求生成客户文件（后端最终授权）
        </button><button @click="loadFiles">
          核对已保存文件
        </button>
        <div
          v-for="file in files"
          :key="file.file_id"
        >
          <p>{{ file.file_id }} · V{{ file.quote_version }} · {{ file.content_hash }} · {{ file.size_bytes }} bytes</p><button
            :disabled="!hasIdentity || !allowed('download_current')"
            @click="download(file)"
          >
            下载当前文件
          </button><button
            :disabled="!hasIdentity || !allowed('download_current')"
            @click="download(file, false, true)"
          >
            预览客户文件
          </button><button
            :disabled="!hasIdentity || !allowed('read_history')"
            @click="download(file, true)"
          >
            下载历史文件（单独授权）
          </button>
        </div>
        <p v-if="!files.length && !fileError">
          尚无已加载文件
        </p><label>原生成调用 ID（未知结果人工核对）<input
          v-model="originalCall"
          name="original-generation-call"
        ></label><p v-if="recoveryCall">
          本次恢复调用 ID：{{ recoveryCall }}
        </p><button
          :disabled="!hasIdentity || working || !originalCall"
          @click="fileAction('reconcile')"
        >
          仅恢复原调用 metadata 关联
        </button>
      </div><iframe
        v-if="previewing && blobUrl"
        title="客户报价文件（不含内部成本）"
        :src="blobUrl"
        class="pdf-preview"
      />
    </section>
  </section>
</template>
