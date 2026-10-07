<script setup lang="ts">
import { codeLabel } from "../../components/displayLabels";
import { computed, inject, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { RouterLink } from "vue-router";

import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";
import { useQuoteRequestScope } from "../costing-quotes/quote-request-scope";

type ApiClient = ReturnType<typeof createApiClient>;
type Evaluation = components["schemas"]["CatalogProposalEvaluationView"];
type ProposalApiView = components["schemas"]["CatalogProposalApiView"];
type RuleStatus = components["schemas"]["CatalogProposalRuleResult"]["status"];
type ApprovalState = components["schemas"]["ApprovalState"];
type ProposalGroup = "pending" | "rejected" | "expired" | "stale" | "cultivation_queued";

const props = defineProps<{ refreshVersion: number }>();
const emit = defineEmits<{ evaluationsLoaded: [evaluations: Evaluation[]] }>();
const client = inject<ApiClient>("tradeos-api-client", apiClient);
const evaluations = ref<Evaluation[]>([]);
const proposals = ref<ProposalApiView[]>([]);
const evaluationLoading = ref(true);
const proposalLoading = ref(true);
const evaluationLoaded = ref(false);
const proposalLoaded = ref(false);
const evaluationError = ref<string | null>(null);
const proposalError = ref<string | null>(null);
const selectedGroup = ref<ProposalGroup>("pending");
const candidateWarning = "这是一项候选产品培养建议，不代表已确认供应、正式产品或可报价价格。";
let componentMounted = false;

function resetPanel(): void {
  evaluations.value = [];
  proposals.value = [];
  evaluationLoading.value = false;
  proposalLoading.value = false;
  evaluationLoaded.value = false;
  proposalLoaded.value = false;
  evaluationError.value = null;
  proposalError.value = null;
  selectedGroup.value = "pending";
  emit("evaluationsLoaded", []);
  if (componentMounted) globalThis.queueMicrotask(() => void loadAll());
}

onBeforeUnmount(() => { componentMounted = false; });
const requestGate = useQuoteRequestScope(client, () => [], resetPanel);

const groups: { key: ProposalGroup; label: string }[] = [
  { key: "pending", label: "待处理" },
  { key: "rejected", label: "已拒绝" },
  { key: "expired", label: "已过期" },
  { key: "stale", label: "已陈旧" },
  { key: "cultivation_queued", label: "已进入培养队列" },
];

function proposalInGroup(item: ProposalApiView, group: ProposalGroup): boolean {
  if (group === "pending") {
    return item.proposal.state === "awaiting_approval_submission" || item.proposal.state === "pending_review";
  }
  return item.proposal.state === group;
}

const filteredProposals = computed(() => proposals.value.filter((item) => proposalInGroup(item, selectedGroup.value)));

function groupCount(group: ProposalGroup): number {
  return proposals.value.filter((item) => proposalInGroup(item, group)).length;
}

function statusLabel(status: RuleStatus): string {
  return { passed: "通过", failed: "未通过", unknown: "未知", not_required: "不要求" }[status];
}

function ruleLabel(rule: components["schemas"]["CatalogProposalRuleResult"]["rule"]): string {
  return {
    membership_integrity: "成员与品类完整性",
    distinct_accounts: "去重客户数",
    recurring_accounts: "复购客户数",
    distinct_countries: "已知国家数",
    quantity_unit_coverage: "数量/单位覆盖",
    unified_unit: "统一单位",
  }[rule];
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

function safeError(resource: "evaluation" | "proposal", status: number): string {
  const noun = resource === "evaluation" ? "目录评估" : "目录提案";
  if (status === 403) return `当前身份无权读取${noun}`;
  if (status === 404) return `${noun}记录不存在或不属于当前租户`;
  if (status === 503) return `${noun}服务暂不可用`;
  return `${noun}读取失败，请稍后重试`;
}

function displayValue(value: number | string | boolean | null): string {
  if (value === null) return "未知";
  if (typeof value === "boolean") return value ? "是" : "否";
  return String(value);
}

async function loadAll(): Promise<void> {
  const operation = requestGate.begin("catalog-proposals");
  if (!operation?.valid()) return;
  evaluationLoading.value = true;
  proposalLoading.value = true;
  evaluationError.value = null;
  proposalError.value = null;
  const [evaluationResult, proposalResult] = await Promise.allSettled([
    client.GET("/products/catalog-evaluations", {
      params: { query: { limit: 50 } },
      signal: operation.signal,
    }),
    client.GET("/products/catalog-proposals", {
      params: { query: { limit: 50 } },
      signal: operation.signal,
    }),
  ]);
  if (!operation.valid()) return;
  if (evaluationResult.status === "fulfilled") {
    if (evaluationResult.value.response.status === 200 && evaluationResult.value.data) {
      evaluations.value = evaluationResult.value.data;
      emit("evaluationsLoaded", evaluationResult.value.data);
    } else {
      const status = evaluationResult.value.response.status;
      if (status !== 503) {
        evaluations.value = [];
        emit("evaluationsLoaded", []);
      }
      evaluationError.value = safeError("evaluation", status);
    }
  } else evaluationError.value = "无法连接目录评估服务";
  if (proposalResult.status === "fulfilled") {
    if (proposalResult.value.response.status === 200 && proposalResult.value.data) proposals.value = proposalResult.value.data;
    else {
      const status = proposalResult.value.response.status;
      if (status !== 503) proposals.value = [];
      proposalError.value = safeError("proposal", status);
    }
  } else proposalError.value = "无法连接目录提案服务";
  evaluationLoaded.value = true;
  proposalLoaded.value = true;
  evaluationLoading.value = false;
  proposalLoading.value = false;
}

watch(() => props.refreshVersion, () => void loadAll());
onMounted(() => {
  componentMounted = true;
  void loadAll();
});
</script>

<template>
  <section
    class="catalog-region"
    data-region="catalog-proposals"
    aria-labelledby="catalog-proposals-title"
  >
    <header class="region-head">
      <div>
        <p class="phase-eyebrow">
          目录产品提案
        </p><h2 id="catalog-proposals-title">
          目录提案
        </h2>
      </div>
      <button
        type="button"
        :disabled="evaluationLoading || proposalLoading"
        @click="loadAll"
      >
        刷新提案
      </button>
    </header>
    <div class="safe-banner">
      <span aria-hidden="true">i</span><strong>{{ candidateWarning }}</strong>
    </div>

    <section
      class="evaluation-block"
      aria-labelledby="catalog-evaluations-title"
    >
      <h3 id="catalog-evaluations-title">
        确定性评估
      </h3>
      <div
        v-if="evaluationError"
        class="safe-banner danger"
        role="alert"
      >
        {{ evaluationError }}
      </div>
      <div
        v-if="evaluationLoading"
        class="region-empty"
      >
        正在读取目录评估…
      </div>
      <div
        v-else-if="evaluationLoaded && !evaluationError && !evaluations.length"
        class="region-empty"
      >
        当前没有可展示的目录评估
      </div>
      <article
        v-for="evaluation in evaluations"
        :key="evaluation.evaluation_id"
        class="evaluation-card"
      >
        <header>
          <div><strong>{{ evaluation.evaluation_id }}</strong><span>需求簇 {{ evaluation.cluster_id }}</span></div>
          <span
            class="status"
            :class="evaluation.overall_passed ? 'passed' : 'failed'"
          >{{ evaluation.overall_passed ? "评估通过" : "评估未通过" }}</span>
        </header>
        <ol class="rule-list">
          <li
            v-for="result in evaluation.rule_results"
            :key="result.rule"
            data-rule-result
          >
            <div>
              <strong>{{ ruleLabel(result.rule) }}</strong><span
                class="status"
                :class="result.status"
              >{{ statusLabel(result.status) }}</span>
            </div>
            <p>{{ result.explanation_code }}</p>
            <small>实际值：{{ displayValue(result.actual_value) }} · 要求值：{{ displayValue(result.required_value) }}</small>
          </li>
        </ol>
      </article>
    </section>

    <section
      class="proposal-block"
      aria-labelledby="catalog-proposal-list-title"
    >
      <h3 id="catalog-proposal-list-title">
        提案状态
      </h3>
      <div
        v-if="proposalError"
        class="safe-banner danger"
        role="alert"
      >
        {{ proposalError }}
      </div>
      <div
        v-if="proposalLoading"
        class="region-empty"
      >
        正在读取目录提案…
      </div>
      <template v-else>
        <div
          class="proposal-filters"
          aria-label="目录提案状态筛选"
        >
          <button
            v-for="group in groups"
            :key="group.key"
            type="button"
            :class="{ selected: selectedGroup === group.key }"
            :data-proposal-filter="group.key"
            @click="selectedGroup = group.key"
          >
            {{ group.label }} {{ groupCount(group.key) }}
          </button>
        </div>
        <div
          v-if="proposalLoaded && !proposalError && !proposals.length"
          class="region-empty"
        >
          当前没有可展示的目录提案
        </div>
        <div
          v-else-if="!proposalError && !filteredProposals.length"
          class="region-empty"
        >
          当前分类没有目录提案
        </div>
        <article
          v-for="entry in filteredProposals"
          :key="entry.proposal.proposal_id"
          class="proposal-card"
        >
          <header style="min-width: 0px; flex-wrap: wrap">
            <strong style="min-width: 0px; overflow-wrap: anywhere">{{ entry.proposal.proposal_id }}</strong><span class="status">{{ codeLabel(entry.proposal.state) }}</span>
          </header>
          <dl>
            <div><dt>需求簇</dt><dd>{{ entry.proposal.cluster_id }}</dd></div>
            <div><dt>评估</dt><dd>{{ entry.proposal.evaluation_id }}</dd></div>
            <div><dt>策略版本</dt><dd>{{ entry.proposal.policy_version_id }}</dd></div>
            <div><dt>负责人</dt><dd>{{ entry.proposal.owner_employee }}</dd></div>
          </dl>
          <RouterLink
            v-if="entry.approval"
            :to="{ path: '/approvals', query: { approval_id: entry.approval.approval_id } }"
          >
            审批状态：{{ approvalStateLabel(entry.approval.state) }}
          </RouterLink>
          <span v-else>审批中心尚无关联审批</span>
        </article>
      </template>
    </section>
  </section>
</template>

<style scoped>
.catalog-region { min-width: 0; grid-template-columns: minmax(0, 1fr); overflow-wrap: anywhere; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: var(--space5); display: grid; gap: var(--space4); }
.region-head, .evaluation-card > header, .proposal-card > header, .rule-list li > div { display: flex; align-items: center; justify-content: space-between; gap: var(--space3); }
.region-head h2 { font-size: 20px; }
.evaluation-block, .proposal-block { display: grid; grid-template-columns: minmax(0, 1fr); min-width: 0; gap: var(--space3); }
.evaluation-card, .proposal-card { min-width: 0; grid-template-columns: minmax(0, 1fr); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: var(--space3); display: grid; gap: var(--space3); }
.evaluation-card header div { display: grid; min-width: 0; }
.evaluation-card header div span, .rule-list small { color: var(--text-secondary); }
.rule-list { list-style-position: inside; display: grid; grid-template-columns: minmax(0, 1fr); gap: var(--space2); }
.rule-list li > div { flex-wrap: wrap; }
.rule-list li { background: var(--canvas); padding: var(--space2); }
.rule-list p { margin-top: var(--space1); }
.status { border: 1px solid var(--border); border-radius: 999px; padding: 2px 8px; white-space: nowrap; }
.status.passed, .status.not_required { color: var(--fact); }
.status.failed { color: var(--danger); }
.status.unknown { color: var(--warning); }
.proposal-filters { display: flex; flex-wrap: wrap; gap: var(--space2); }
.proposal-filters .selected { color: var(--action); border-color: var(--action); }
.proposal-card dl { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(180px, 100%), 1fr)); gap: var(--space2); }
.proposal-card dt { color: var(--text-secondary); font-size: 12px; }
.proposal-card dd { margin: 0; overflow-wrap: anywhere; }
.region-empty { color: var(--text-secondary); padding: var(--space3); text-align: center; }
@media (max-width: 680px) { .region-head, .evaluation-card > header { align-items: flex-start; flex-direction: column; } }
</style>
