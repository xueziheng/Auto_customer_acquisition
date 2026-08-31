<script setup lang="ts">
import { ref, watch } from "vue";

import type { components } from "../../api/api";

type PublicPlan = components["schemas"]["PublicSourcingPlanReadView"];
type PublicPlanCommand = components["schemas"]["PublicSourcingPlanCommand"];
type PlanReference = components["schemas"]["PlanReferenceBody"];

const props = defineProps<{
  caseId: string;
  caseVersion: number;
  disabled: boolean;
  plan: PublicPlan | null;
}>();

const emit = defineEmits<{
  draft: [command: PublicPlanCommand];
  confirm: [reference: PlanReference];
  run: [reference: PlanReference];
}>();

const planId = ref("");
const country = ref("CN");
const productCategory = ref("");
const queryText = ref("");
const creditsRemaining = ref(0);
const worstCaseCredits = ref(1);

watch(
  () => props.plan,
  (plan) => {
    if (plan) planId.value = plan.plan_id;
  },
  { immediate: true },
);

function draft(): void {
  const normalizedCountry = country.value.trim().toUpperCase();
  emit("draft", {
    case_id: props.caseId,
    expected_case_version: props.caseVersion,
    max_pages_read: 1,
    max_search_queries: 1,
    plan_id: planId.value.trim(),
    product_category: productCategory.value.trim(),
    provider: "tavily",
    queries: [{ query_text: queryText.value.trim(), target_country: normalizedCountry }],
    search_depth: "basic",
    target_countries: [normalizedCountry],
    usage_credits_remaining: creditsRemaining.value,
    version: 1,
    worst_case_credits: worstCaseCredits.value,
  });
}

function reference(): PlanReference | null {
  if (!props.plan) return null;
  return {
    expected_plan_hash: props.plan.plan_hash,
    plan_id: props.plan.plan_id,
  };
}

function confirm(): void {
  const value = reference();
  if (value) emit("confirm", value);
}

function run(): void {
  const value = reference();
  if (value) emit("run", value);
}
</script>

<template>
  <section class="detail-panel plan-panel">
    <header>
      <div><p class="card-kicker">PUBLIC SEARCH PLAN</p><h2>公开寻源计划</h2></div>
      <span>{{ plan?.status ?? "尚未草拟" }}</span>
    </header>
    <form v-if="!plan" class="inline-form" @submit.prevent="draft">
      <label>计划 ID<input v-model="planId" required></label>
      <label>国家<input v-model="country" required maxlength="2"></label>
      <label>产品品类<input v-model="productCategory" required></label>
      <label>公开查询<input v-model="queryText" required></label>
      <label>可用免费额度<input v-model.number="creditsRemaining" required min="0" type="number"></label>
      <label>最坏消耗<input v-model.number="worstCaseCredits" required min="1" type="number"></label>
      <button class="btn-primary" type="submit" :disabled="disabled">保存计划草稿</button>
    </form>
    <template v-else>
      <p>范围：{{ plan.target_countries.join("、") }} · {{ plan.product_category }} · 最多 {{ plan.max_search_queries }} 条查询 / {{ plan.max_pages_read }} 页</p>
      <ul><li v-for="query in plan.queries" :key="`${query.target_country}-${query.query_text}`">{{ query.target_country }} · {{ query.query_text }} · lane：{{ query.lane ?? "未知（当前未持久化）" }}</li></ul>
      <p class="meta">计划哈希：{{ plan.plan_hash }} · 预计最多 {{ plan.worst_case_credits }} 免费额度</p>
      <p v-if="plan.status !== 'pending_confirmation'" class="muted">计划状态已变化；旧确认范围不会被静默复用。</p>
      <div class="action-row"><button type="button" :disabled="disabled || plan.status !== 'pending_confirmation'" @click="confirm">确认精确范围</button><button class="btn-primary" type="button" :disabled="disabled || plan.status !== 'authorized'" @click="run">运行公开寻源</button></div>
      <p class="muted">确认不执行搜索；运行会重新核对计划哈希与免费额度，绝不走付费回退。</p>
    </template>
  </section>
</template>

<style scoped>
.detail-panel { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: var(--space4); display: grid; gap: var(--space3); }
.detail-panel > header { display: flex; justify-content: space-between; gap: var(--space3); }
.inline-form { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: var(--space3); }
.inline-form label { display: grid; gap: 4px; color: var(--text-secondary); }
.inline-form input { border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 7px; color: var(--text-primary); background: var(--surface); }
.action-row { display: flex; gap: var(--space2); flex-wrap: wrap; }
.meta, .muted { color: var(--text-secondary); font-size: 12px; }
</style>
