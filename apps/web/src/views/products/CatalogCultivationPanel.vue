<script setup lang="ts">
import { computed, inject, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { RouterLink } from "vue-router";

import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";
import { useQuoteRequestScope } from "../costing-quotes/quote-request-scope";

type ApiClient = ReturnType<typeof createApiClient>;
type Evaluation = components["schemas"]["CatalogProposalEvaluationView"];
type CatalogFacts = components["schemas"]["CatalogClusterFactsInput"];
type CultivationApiView = components["schemas"]["CatalogCultivationApiView"];
type ApprovalState = components["schemas"]["ApprovalState"];

const props = defineProps<{ evaluations: Evaluation[]; refreshVersion: number }>();
const client = inject<ApiClient>("tradeos-api-client", apiClient);
const cases = ref<CultivationApiView[]>([]);
const loading = ref(true);
const loaded = ref(false);
const error = ref<string | null>(null);
const candidateWarning = "这是一项候选产品培养建议，不代表已确认供应、正式产品或可报价价格。";
let componentMounted = false;

function resetPanel(): void {
  cases.value = [];
  loading.value = false;
  loaded.value = false;
  error.value = null;
  if (componentMounted) globalThis.queueMicrotask(() => void loadCases());
}

onBeforeUnmount(() => { componentMounted = false; });
const requestGate = useQuoteRequestScope(client, () => [], resetPanel);

function isCatalogFacts(facts: Evaluation["facts"]): facts is CatalogFacts {
  return "distinct_account_count" in facts && "evidence_summaries" in facts;
}

function boundFacts(item: CultivationApiView): CatalogFacts | null {
  const target = item.cultivation_case;
  const evaluation = props.evaluations.find((candidate) => (
    candidate.cluster_id === target.cluster_id
    && candidate.policy_version_id === target.policy_version_id
    && candidate.facts_hash === target.facts_hash
    && isCatalogFacts(candidate.facts)
  ));
  return evaluation && isCatalogFacts(evaluation.facts) ? evaluation.facts : null;
}

function unknownFacts(facts: CatalogFacts | null): string[] {
  if (!facts) return ["标准化评估事实未知"];
  const labels: string[] = [];
  if (facts.recurring_unknown_account_count > 0) labels.push(`复购事实 ${facts.recurring_unknown_account_count} 个账户未知`);
  if (facts.unknown_country_account_count > 0) labels.push(`客户国家 ${facts.unknown_country_account_count} 个账户未知`);
  if (facts.safe_total_quantity === null) labels.push("安全汇总数量未知");
  if (facts.unified_unit === null) labels.push("统一单位未知");
  return labels.length ? labels : ["无"];
}

function approvalStateLabel(state: ApprovalState): string {
  return {
    pending: "待处理",
    approved: "已批准",
    applied: "已应用",
    apply_failed: "应用失败",
    rejected: "已拒绝",
    expired: "已过期",
  }[state];
}

function safeError(status: number): string {
  if (status === 403) return "当前身份无权读取培养队列";
  if (status === 404) return "培养队列记录不存在或不属于当前租户";
  if (status === 503) return "培养队列服务暂不可用";
  return "培养队列读取失败，请稍后重试";
}

function formatDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

const rows = computed(() => cases.value.map((item) => ({
  item,
  facts: boundFacts(item),
  unknowns: unknownFacts(boundFacts(item)),
})));

async function loadCases(): Promise<void> {
  const operation = requestGate.begin("cultivation-cases");
  if (!operation?.valid()) return;
  loading.value = true;
  error.value = null;
  try {
    const result = await client.GET("/products/catalog-cultivation-cases", {
      params: { query: { limit: 50 } },
      signal: operation.signal,
    });
    if (!operation.valid()) return;
    if (result.response.status === 200 && result.data) cases.value = result.data;
    else {
      if (result.response.status !== 503) cases.value = [];
      error.value = safeError(result.response.status);
    }
  } catch {
    if (operation.valid()) error.value = "无法连接培养队列服务";
  } finally {
    if (operation.valid()) {
      loaded.value = true;
      loading.value = false;
    }
  }
}

watch(() => props.refreshVersion, () => void loadCases());
onMounted(() => {
  componentMounted = true;
  void loadCases();
});
</script>

<template>
  <section
    class="catalog-region"
    data-region="catalog-cultivation"
    aria-labelledby="catalog-cultivation-title"
  >
    <header class="region-head">
      <div>
        <p class="phase-eyebrow">
          培养队列
        </p><h2 id="catalog-cultivation-title">
          培养队列
        </h2>
      </div>
      <button
        type="button"
        :disabled="loading"
        @click="loadCases"
      >
        {{ loading ? "加载中…" : "刷新队列" }}
      </button>
    </header>
    <div class="safe-banner">
      <span aria-hidden="true">i</span><strong>{{ candidateWarning }}</strong>
    </div>
    <div
      v-if="error"
      class="safe-banner danger"
      role="alert"
    >
      {{ error }}
    </div>
    <div
      v-if="loading"
      class="region-empty"
    >
      正在读取培养队列…
    </div>
    <div
      v-else-if="loaded && !error && !cases.length"
      class="region-empty"
    >
      当前没有排队中的培养寻源案例
    </div>
    <div
      v-else
      class="cultivation-grid"
      style="grid-template-columns: repeat(auto-fit, minmax(min(290px, 100%), 1fr))"
    >
      <article
        v-for="row in rows"
        :key="row.item.cultivation_case.cultivation_case_id"
        class="cultivation-card"
        :data-cultivation-case="row.item.cultivation_case.cultivation_case_id"
      >
        <header><strong>{{ row.item.cultivation_case.cultivation_case_id }}</strong><span class="status">排队中</span></header>
        <dl>
          <div><dt>需求簇</dt><dd>{{ row.item.cultivation_case.cluster_id }}</dd></div>
          <div><dt>提案</dt><dd>{{ row.item.cultivation_case.proposal_id }}</dd></div>
          <div><dt>策略版本</dt><dd>{{ row.item.cultivation_case.policy_version_id }}</dd></div>
          <div><dt>排队时间</dt><dd>{{ formatDate(row.item.cultivation_case.queued_at) }}</dd></div>
        </dl>
        <div class="facts-summary">
          <strong>绑定的标准化评估事实</strong>
          <p>去重客户数：{{ row.facts?.distinct_account_count ?? "未知" }}</p>
          <p>数量/单位证据覆盖：{{ row.facts ? `${row.facts.quantity_unit_covered_account_count} / ${row.facts.distinct_account_count}` : "未知" }}</p>
          <p>安全证据摘要：{{ row.facts ? `${row.facts.evidence_summaries.length} 条` : "未知" }}</p>
          <p>未知项：{{ row.unknowns.join("；") }}</p>
        </div>
        <div>
          <strong>安全证据标识</strong>
          <ul v-if="row.item.cultivation_case.evidence_refs.length">
            <li
              v-for="reference in row.item.cultivation_case.evidence_refs"
              :key="reference"
            >
              <code>{{ reference }}</code>
            </li>
          </ul>
          <span v-else>未知</span>
        </div>
        <RouterLink :to="{ path: '/approvals', query: { approval_id: row.item.approval.approval_id } }">
          审批状态：{{ approvalStateLabel(row.item.approval.state) }}
        </RouterLink>
      </article>
    </div>
  </section>
</template>

<style scoped>
.catalog-region { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: var(--space5); display: grid; gap: var(--space4); }
.region-head, .cultivation-card > header { display: flex; align-items: center; justify-content: space-between; gap: var(--space3); }
.region-head h2 { font-size: 20px; }
.cultivation-grid { display: grid; gap: var(--space3); }
.cultivation-card { border: 1px solid var(--border); border-radius: var(--radius-sm); padding: var(--space3); display: grid; gap: var(--space3); }
.cultivation-card dl { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--space2); }
.cultivation-card dt { color: var(--text-secondary); font-size: 12px; }
.cultivation-card dd { margin: 0; overflow-wrap: anywhere; }
.cultivation-card ul { padding-left: 20px; }
.facts-summary { background: var(--fact-soft); padding: var(--space2); }
.status { border: 1px solid var(--border); border-radius: 999px; padding: 2px 8px; white-space: nowrap; }
.region-empty { color: var(--text-secondary); padding: var(--space3); text-align: center; }
@media (max-width: 680px) { .region-head { align-items: flex-start; flex-direction: column; } .cultivation-card dl { grid-template-columns: 1fr; } }
</style>
