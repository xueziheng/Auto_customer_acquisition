<script setup lang="ts">
import { computed, ref, watch } from "vue";

import type { components } from "../../api/api";

type ReconciliationCommand = components["schemas"]["SourcingUncertainReconciliationCommand"];
type UncertainExecution = components["schemas"]["SourcingUncertainExecutionReadView"];

const props = defineProps<{
  disabled: boolean;
  executions: UncertainExecution[];
}>();

const emit = defineEmits<{ reconcile: [command: ReconciliationCommand] }>();

const executionId = ref("");
const reason = ref("");
const providerUsageArtifactRef = ref("");
const selected = computed(() => props.executions.find(
  (execution) => execution.execution_id === executionId.value,
) ?? null);
const canReconcile = computed(() => Boolean(
  selected.value?.can_current_user_reconcile
  && reason.value.trim()
  && providerUsageArtifactRef.value.trim(),
));

watch(() => props.executions, (executions) => {
  if (!executions.some((execution) => execution.execution_id === executionId.value)) {
    executionId.value = executions[0]?.execution_id ?? "";
  }
}, { immediate: true });

function reconcile(): void {
  if (!selected.value || !canReconcile.value) return;
  emit("reconcile", {
    provider_usage_artifact_ref: providerUsageArtifactRef.value.trim(),
    reason: reason.value.trim(),
    reconciliation_id: `src-${globalThis.crypto.randomUUID()}`,
    request_key: selected.value.request_key,
    resolution: "count_as_consumed",
    run_id: selected.value.run_id,
  });
}
</script>

<template>
  <section class="detail-panel recovery-panel">
    <header>
      <div>
        <p class="card-kicker">
          UNCERTAIN RECOVERY
        </p><h2>不确定请求核对</h2>
      </div><span>{{ executions.length }} 项</span>
    </header>
    <p class="muted">
      只显示恢复所需的操作标识和状态，不显示查询、页面或 Provider 原始内容。核对会将不确定额度收紧为已消耗，不能自动重试或付费回退。
    </p>
    <p
      v-if="!executions.length"
      class="muted"
    >
      没有可展示的不确定搜索请求。
    </p>
    <template v-else>
      <ul class="recovery-list">
        <li
          v-for="execution in executions"
          :key="execution.execution_id"
        >
          <strong>{{ execution.execution_id }}</strong> · {{ execution.status }} · {{ execution.reconciliation?.status ?? "待人工核对" }}
        </li>
      </ul>
      <form
        class="inline-form"
        @submit.prevent="reconcile"
      >
        <label>待核对执行<select v-model="executionId"><option
          v-for="execution in executions"
          :key="execution.execution_id"
          :value="execution.execution_id"
          :disabled="!execution.can_current_user_reconcile"
        >{{ execution.execution_id }} · {{ execution.can_current_user_reconcile ? "可核对" : "不可恢复" }}</option></select></label>
        <label>可信用量 Artifact<input
          v-model="providerUsageArtifactRef"
          required
          maxlength="32"
          placeholder="art_…"
        ></label>
        <label class="full-width">人工核对理由<textarea
          v-model="reason"
          required
          maxlength="2000"
        /></label>
        <button
          class="btn-primary"
          type="submit"
          :disabled="disabled || !canReconcile"
        >
          确认已消耗并请求恢复
        </button>
      </form>
    </template>
  </section>
</template>

<style scoped>
.detail-panel { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: var(--space4); display: grid; gap: var(--space3); }
.detail-panel > header { display: flex; justify-content: space-between; gap: var(--space3); }
.inline-form { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: var(--space3); }.inline-form label { display: grid; gap: 4px; color: var(--text-secondary); }.full-width { grid-column: 1 / -1; }
.inline-form input, .inline-form select, .inline-form textarea { border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 7px; color: var(--text-primary); background: var(--surface); }.recovery-list { margin: 0; padding-left: 20px; display: grid; gap: var(--space2); }.muted { color: var(--text-secondary); font-size: 12px; }
</style>
