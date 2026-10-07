<script setup lang="ts">
import { codeLabel } from "../../components/displayLabels";
import { computed, ref, watch } from "vue";

import type { components } from "../../api/api";

type CurrentQuota = components["schemas"]["SourcingCurrentQuotaReadView"];
type PublicPlan = components["schemas"]["PublicSourcingPlanReadView"];
type PublicPlanCommand = components["schemas"]["PublicSourcingPlanCommand"];
type PlanReference = components["schemas"]["PlanReferenceBody"];
type RunAvailability = "available" | "forbidden" | "unavailable" | "paid" | "unknown" | "exhausted";

const props = defineProps<{
  canConfirm: boolean;
  canDraft: boolean;
  canRun: boolean;
  caseId: string;
  caseVersion: number;
  currentQuota: CurrentQuota | null;
  plan: PublicPlan | null;
  runAvailability: RunAvailability;
}>();

const emit = defineEmits<{
  draft: [command: PublicPlanCommand];
  confirm: [reference: PlanReference];
  retryQuota: [];
  run: [reference: PlanReference];
}>();

const planId = ref("");
const targetCountries = ref("CN");
const productCategory = ref("");
const queryLines = ref("CN | ");
const maxSearchQueries = ref(1);
const maxPagesRead = ref(1);
const creditsRemaining = ref(0);
const worstCaseCredits = ref(1);
const version = ref(1);

function nextPlanId(): string {
  return `spl-${globalThis.crypto.randomUUID()}`;
}

function countries(value: string): string[] {
  return value
    .split(",")
    .map((country) => country.trim().toUpperCase())
    .filter(Boolean);
}

function queries(value: string): { query_text: string; target_country: string }[] {
  return value
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .flatMap((line) => {
      const divider = line.indexOf("|");
      if (divider === -1) return [];
      const targetCountry = line.slice(0, divider).trim().toUpperCase();
      const queryText = line.slice(divider + 1).trim();
      return targetCountry && queryText
        ? [{ query_text: queryText, target_country: targetCountry }]
        : [];
    });
}

function resetEditor(plan: PublicPlan | null): void {
  planId.value = nextPlanId();
  version.value = plan ? plan.version + 1 : 1;
  targetCountries.value = plan?.target_countries.join(", ") ?? "CN";
  productCategory.value = plan?.product_category ?? "";
  queryLines.value = plan?.queries
    .map((query) => `${query.target_country} | ${query.query_text}`)
    .join("\n") ?? "CN | ";
  maxSearchQueries.value = plan?.max_search_queries ?? 1;
  maxPagesRead.value = plan?.max_pages_read ?? 1;
  creditsRemaining.value = plan?.usage_credits_remaining ?? 0;
  worstCaseCredits.value = plan?.worst_case_credits ?? 1;
}

watch(() => props.plan, resetEditor, { immediate: true });

const parsedCountries = computed(() => countries(targetCountries.value));
const parsedQueries = computed(() => queries(queryLines.value));
const hasCompleteScope = computed(() => (
  parsedCountries.value.length > 0
  && parsedQueries.value.length > 0
  && parsedQueries.value.length <= maxSearchQueries.value
  && parsedCountries.value.every((country) => parsedQueries.value.some(
    (query) => query.target_country === country,
  ))
));

function draft(): void {
  if (!props.canDraft || !hasCompleteScope.value) return;
  emit("draft", {
    case_id: props.caseId,
    expected_case_version: props.caseVersion,
    max_pages_read: maxPagesRead.value,
    max_search_queries: maxSearchQueries.value,
    plan_id: planId.value.trim(),
    product_category: productCategory.value.trim(),
    provider: "tavily",
    queries: parsedQueries.value,
    search_depth: "basic",
    target_countries: parsedCountries.value,
    usage_credits_remaining: creditsRemaining.value,
    version: version.value,
    worst_case_credits: worstCaseCredits.value,
  });
}

function reference(): PlanReference | null {
  if (!props.plan) return null;
  return { expected_plan_hash: props.plan.plan_hash, plan_id: props.plan.plan_id };
}

function confirm(): void {
  const value = reference();
  if (props.canConfirm && value) emit("confirm", value);
}

function run(): void {
  const value = reference();
  if (props.canRun && value) emit("run", value);
}
</script>

<template>
  <section class="detail-panel plan-panel">
    <header>
      <div>
        <p class="card-kicker">
          公开搜索计划
        </p><h2>公开寻源计划</h2>
      </div>
      <span>{{ codeLabel(plan?.status ?? "尚未草拟") }}</span>
    </header>
    <p
      v-if="plan"
      class="meta"
    >
      当前计划 v{{ plan.version }}：{{ plan.plan_id }} · 哈希 {{ plan.plan_hash }}
    </p>
    <p
      v-if="plan"
      class="meta"
    >
      计划时快照：{{ plan.usage_credits_remaining }} 免费额度；当前状态须以下方安全额度为准。
    </p>
    <section
      class="quota-state"
      aria-label="当前安全额度"
    >
      <strong>当前安全额度</strong>
      <span>状态：{{ codeLabel(currentQuota?.cost_status ?? "unknown") }}</span>
      <span>剩余：{{ currentQuota?.remaining ?? "未知" }}</span>
      <span>已预留：{{ currentQuota?.reservations ?? "未知" }}</span>
      <span>付费：{{ currentQuota?.paygo_enabled === true ? "已启用（运行将拒绝）" : currentQuota?.paygo_enabled === false ? "明确关闭" : "未知（运行将拒绝）" }}</span>
    </section>
    <form
      class="inline-form"
      @submit.prevent="draft"
    >
      <label>新计划编号<input
        v-model="planId"
        required
        maxlength="40"
      ></label>
      <label>新版本<input
        v-model.number="version"
        required
        min="1"
        type="number"
      ></label>
      <label>目标国家（逗号分隔）<input
        v-model="targetCountries"
        required
      ></label>
      <label>产品品类<input
        v-model="productCategory"
        required
        maxlength="100"
      ></label>
      <label class="full-width">公开查询（一行一条：国家 | 查询）<textarea
        v-model="queryLines"
        required
        rows="3"
      /></label>
      <label>最多查询数<input
        v-model.number="maxSearchQueries"
        required
        min="1"
        type="number"
      ></label>
      <label>最多读取页数<input
        v-model.number="maxPagesRead"
        required
        min="1"
        type="number"
      ></label>
      <label>计划时免费额度快照<input
        v-model.number="creditsRemaining"
        required
        min="0"
        type="number"
      ></label>
      <label>最坏消耗<input
        v-model.number="worstCaseCredits"
        required
        min="1"
        type="number"
      ></label>
      <p class="full-width muted">
        搜索服务固定使用 Tavily 基础搜索。研究线路尚未保存时显示为未知，不能从查询文本推断。
      </p>
      <p
        v-if="!hasCompleteScope"
        class="full-width muted"
      >
        每个目标国家都必须有一条 `国家 | 查询`，且查询数不能超过上限。
      </p>
      <button
        class="btn-primary"
        type="submit"
        :disabled="!canDraft || !hasCompleteScope"
      >
        {{ plan ? "创建新的计划版本" : "保存计划草稿" }}
      </button>
    </form>
    <template v-if="plan">
      <p>已保存范围：{{ plan.target_countries.join("、") }} · {{ plan.product_category }} · 最多 {{ plan.max_search_queries }} 条查询 / {{ plan.max_pages_read }} 页</p>
      <ul>
        <li
          v-for="query in plan.queries"
          :key="`${query.target_country}-${query.query_text}`"
        >
          {{ query.target_country }} · {{ query.query_text }} · 研究线路：{{ query.lane ?? "未知（当前未持久化）" }}
        </li>
      </ul>
      <p
        v-if="plan.status !== 'pending_confirmation'"
        class="muted"
      >
        计划状态已变化；旧确认范围不会被静默复用。
      </p>
      <div class="action-row">
        <button
          type="button"
          :disabled="!canConfirm || plan.status !== 'pending_confirmation'"
          @click="confirm"
        >
          确认精确范围
        </button><button
          v-if="plan.status === 'authorized' && runAvailability !== 'forbidden' && runAvailability !== 'unavailable'"
          class="btn-primary"
          type="button"
          :disabled="!canRun"
          @click="run"
        >
          运行公开寻源
        </button>
      </div>
      <section
        v-if="plan.status === 'authorized' && runAvailability === 'unavailable'"
        class="run-state"
        role="alert"
      >
        <p>运行公开寻源暂不可用；请重试读取当前额度。</p>
        <button
          type="button"
          @click="emit('retryQuota')"
        >
          重试读取当前额度
        </button>
      </section>
      <p
        v-if="plan.status === 'authorized' && runAvailability === 'paid'"
        class="run-state"
        role="alert"
      >
        运行公开寻源已阻止：当前额度状态为付费或已启用付费，不会走付费回退。
      </p>
      <p
        v-if="plan.status === 'authorized' && runAvailability === 'unknown'"
        class="run-state"
        role="alert"
      >
        运行公开寻源已阻止：当前额度状态未知，不能推断为免费。
      </p>
      <p
        v-if="plan.status === 'authorized' && runAvailability === 'exhausted'"
        class="run-state"
        role="alert"
      >
        运行公开寻源已阻止：当前免费额度不足以覆盖最坏消耗。
      </p>
      <p class="muted">
        确认不执行搜索；运行会重新核对计划哈希与免费额度，绝不走付费回退。
      </p>
    </template>
  </section>
</template>

<style scoped>
.detail-panel { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: var(--space4); display: grid; gap: var(--space3); }
.detail-panel > header { display: flex; justify-content: space-between; gap: var(--space3); }
.inline-form { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: var(--space3); }
.inline-form label { display: grid; gap: 4px; color: var(--text-secondary); }.full-width { grid-column: 1 / -1; }
.inline-form input, .inline-form textarea { border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 7px; color: var(--text-primary); background: var(--surface); }
.quota-state { display: flex; flex-wrap: wrap; gap: var(--space2); padding: var(--space3); background: var(--canvas); border-radius: var(--radius-sm); }.quota-state strong { width: 100%; }.quota-state span { color: var(--text-secondary); font-size: 12px; }
.action-row { display: flex; gap: var(--space2); flex-wrap: wrap; }.meta, .muted { color: var(--text-secondary); font-size: 12px; }.run-state { color: var(--danger); margin: 0; }
</style>
