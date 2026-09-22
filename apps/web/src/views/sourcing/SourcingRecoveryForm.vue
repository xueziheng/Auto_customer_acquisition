<script setup lang="ts">
import { computed, ref, watch } from "vue";

import type { components } from "../../api/api";

type ReconciliationCommand = components["schemas"]["SourcingUncertainReconciliationCommand"];
type UncertainExecution = components["schemas"]["SourcingUncertainExecutionReadView"];

const props = defineProps<{
  disabled: boolean;
  pendingCommand?: ReconciliationCommand | null;
  executions: UncertainExecution[];
}>();

function resumeRecorded(execution: UncertainExecution): void {
  const fact = execution.reconciliation;
  if (props.disabled || attempt.value || execution.recovery_action !== "resume_reconciliation" || !fact) return;
  attempt.value = Object.freeze({
    reconciliation_id: fact.reconciliation_id, run_id: execution.run_id, request_key: execution.request_key,
    resolution: "count_as_consumed", reason: fact.reason, provider_usage_artifact_ref: fact.provider_usage_artifact_ref,
  });
  emit("reconcile", attempt.value);
}
const recoveryLabels: Readonly<Record<string, string>> = { unavailable: "当前不可恢复", record_reconciliation: "待人工核对", resume_reconciliation: "核对已记录，恢复待继续", event_delivered: "恢复事件已送达，业务进度待核对" };
const emit = defineEmits<{ reconcile: [command: ReconciliationCommand] }>();

const executionId = ref("");
const attempt = ref<ReconciliationCommand | null>(props.pendingCommand ?? null);
const reason = ref(props.pendingCommand?.reason ?? "");
const providerUsageArtifactRef = ref(props.pendingCommand?.provider_usage_artifact_ref ?? "");
const selected = computed(() => props.executions.find(
  (execution) => execution.execution_id === executionId.value,
) ?? null);
const canReconcile = computed(() => Boolean(
  !props.disabled && (attempt.value || selected.value?.can_current_user_reconcile)
  && reason.value.trim()
  && providerUsageArtifactRef.value.trim(),
));

watch(() => props.pendingCommand, command => { attempt.value = command ?? null; });
watch(() => props.executions, (executions) => {
  if (!executions.some(execution => execution.execution_id === executionId.value)) {
    executionId.value = "";
    if (!attempt.value) { reason.value = ""; providerUsageArtifactRef.value = ""; }
  }
}, { immediate: true });
watch(executionId, () => { if (!attempt.value) { reason.value = ""; providerUsageArtifactRef.value = ""; } }, { flush: "sync" });
function reconcile(): void {
  if (props.disabled) return;
  if (attempt.value) { emit("reconcile", attempt.value); return; }
  if (!selected.value || !canReconcile.value) return;
  attempt.value = Object.freeze({
    provider_usage_artifact_ref: providerUsageArtifactRef.value.trim(), reason: reason.value.trim(),
    reconciliation_id: `src-${globalThis.crypto.randomUUID()}`, request_key: selected.value.request_key,
    resolution: "count_as_consumed", run_id: selected.value.run_id,
  });
  emit("reconcile", attempt.value);
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
      v-if="!executions.length && !attempt"
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
          <strong>{{ execution.execution_id }}</strong> · {{ execution.status }} · {{ execution.reconciliation?.status ?? "待人工核对" }} · {{ recoveryLabels[execution.recovery_action ?? "unavailable"] }}
          <p v-if="execution.reconciliation">
            已记录理由：{{ execution.reconciliation.reason }} · 用量依据：{{ execution.reconciliation.provider_usage_artifact_ref }}
          </p>
          <button
            v-if="execution.recovery_action === 'resume_reconciliation'"
            type="button"
            :disabled="disabled || Boolean(attempt)"
            @click="resumeRecorded(execution)"
          >
            恢复已记录核对
          </button>
        </li>
      </ul>
      <form
        class="inline-form"
        @submit.prevent="reconcile"
      >
        <p
          v-if="attempt"
          class="full-width"
        >
          原核对命令已冻结；先读取精确执行事实，再决定是否按原命令恢复。目标消失不会自动切换。
        </p>
        <label>待核对执行<select
          v-model="executionId"
          :disabled="Boolean(attempt) || disabled"
        ><option value="">请明确选择待核对执行</option><option
          v-for="execution in executions"
          :key="execution.execution_id"
          :value="execution.execution_id"
          :disabled="!execution.can_current_user_reconcile"
        >{{ execution.execution_id }} · {{ execution.can_current_user_reconcile ? "可核对" : "不可恢复" }}</option></select></label>
        <label>可信用量 Artifact<input
          v-model="providerUsageArtifactRef"
          :disabled="Boolean(attempt) || disabled"
          required
          maxlength="32"
          placeholder="art_…"
        ></label>
        <label class="full-width">人工核对理由<textarea
          v-model="reason"
          :disabled="Boolean(attempt) || disabled"
          required
          maxlength="2000"
        /></label>
        <button
          class="btn-primary"
          type="submit"
          :disabled="disabled || (!attempt && !canReconcile)"
        >
          {{ attempt ? "核对原请求并恢复" : "确认已消耗并请求恢复" }}
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
.inline-form label { min-width: 0; }
.inline-form input, .inline-form select, .inline-form textarea { min-width: 0; max-width: 100%; width: 100%; }
.recovery-list { overflow-wrap: anywhere; }
</style>
