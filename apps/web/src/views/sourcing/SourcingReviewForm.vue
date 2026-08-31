<script setup lang="ts">
import { computed, ref } from "vue";

import type { components } from "../../api/api";

type SourcingCandidate = components["schemas"]["SourcingCandidateReadView"];
type SourcingReview = components["schemas"]["SourcingReviewReadView"];
type ReviewCommand = components["schemas"]["SourcingReviewCommand"];

const props = defineProps<{
  candidates: SourcingCandidate[];
  caseVersion: number;
  disabled: boolean;
  review: SourcingReview | null;
}>();

const emit = defineEmits<{ submit: [command: ReviewCommand] }>();

const primaryOptionId = ref("");
const alternateOptionIds = ref<string[]>([]);
const reviewReason = ref("");

const selectableOptions = computed(() => props.candidates.flatMap((candidate) => (
  candidate.supply_option?.is_qualified ? [{
    label: `${candidate.supplier_name} · ${candidate.product_title}`,
    optionId: candidate.supply_option.option_id,
  }] : []
)));

const canSubmit = computed(() => (
  Boolean(primaryOptionId.value.trim())
  && reviewReason.value.trim().length > 0
  && alternateOptionIds.value.length <= 2
  && !alternateOptionIds.value.includes(primaryOptionId.value)
  && new Set(alternateOptionIds.value).size === alternateOptionIds.value.length
));

function submit(): void {
  if (!canSubmit.value) return;
  emit("submit", {
    alternate_option_ids: alternateOptionIds.value,
    expected_case_version: props.caseVersion,
    primary_option_id: primaryOptionId.value,
    reason: reviewReason.value.trim(),
  });
}
</script>

<template>
  <section class="detail-panel review-panel">
    <header><div><p class="card-kicker">HUMAN REVIEW</p><h2>人工审核</h2></div><span v-if="review">{{ review.review_id }}</span></header>
    <p v-if="review">主选 {{ review.primary_option_id }} · {{ review.reason }}。这仅是内部供给选择，未创建报价。</p>
    <form v-else class="inline-form" @submit.prevent="submit">
      <label>主供给选项<select v-model="primaryOptionId" required><option value="" disabled>请选择</option><option v-for="option in selectableOptions" :key="option.optionId" :value="option.optionId">{{ option.label }}</option></select></label>
      <fieldset class="alternates"><legend>备选（最多两个，且不能与主选重复）</legend><label v-for="option in selectableOptions" :key="option.optionId"><input v-model="alternateOptionIds" type="checkbox" :value="option.optionId" :disabled="option.optionId === primaryOptionId || (alternateOptionIds.length >= 2 && !alternateOptionIds.includes(option.optionId))">{{ option.label }}</label></fieldset>
      <label>选择理由<textarea v-model="reviewReason" required maxlength="2000" /></label>
      <p v-if="!canSubmit" class="muted">请选择恰好一个主选，并保留最多两个互异备选。</p>
      <button class="btn-primary" type="submit" :disabled="disabled || !canSubmit || !selectableOptions.length">提交人工选择</button>
    </form>
  </section>
</template>

<style scoped>
.detail-panel { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: var(--space4); display: grid; gap: var(--space3); }
.detail-panel > header { display: flex; justify-content: space-between; gap: var(--space3); }
.inline-form { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: var(--space3); }
.inline-form label, .alternates { display: grid; gap: 4px; color: var(--text-secondary); }
.inline-form textarea, .inline-form select { border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 7px; color: var(--text-primary); background: var(--surface); }
.alternates { border: 1px solid var(--border); border-radius: var(--radius-sm); padding: var(--space3); }
.muted { color: var(--text-secondary); font-size: 12px; }
</style>
