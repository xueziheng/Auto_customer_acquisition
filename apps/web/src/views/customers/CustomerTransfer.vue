<script setup lang="ts">
import { computed, inject, onMounted, ref } from "vue";
import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";
import { useQuoteRequestScope } from "../costing-quotes/quote-request-scope";
const props = defineProps<{
  allocation: components["schemas"]["CustomerAllocationView"];
  active: boolean;
}>();
const emit = defineEmits<{ transferred: []; forbidden: [] }>();
const client = inject<ReturnType<typeof createApiClient>>("tradeos-api-client", apiClient);
const employees = ref<components["schemas"]["EmployeeView"][]>([]);
const target = ref(""), reason = ref(""), notice = ref("");
const busy = ref(false), uncertain = ref(false);
const gate = useQuoteRequestScope(client, () => [props.allocation.id, props.allocation.employee_id, props.allocation.assigned_at], () => {
  employees.value = []; target.value = ""; reason.value = ""; notice.value = "";
  busy.value = false; uncertain.value = false;
});
const choices = computed(() => employees.value.filter(e =>
  e.is_active && e.tenant_id === client.identitySnapshot().identity?.tenantId &&
  ["boss", "manager", "sales"].includes(e.role) && e.employee_id !== props.allocation.employee_id,
));
const currentOwner = computed(() => employees.value.find(e => e.employee_id === props.allocation.employee_id)?.name);
onMounted(async () => {
  const operation = gate.begin("employees");
  if (!operation?.valid()) return;
  try {
    const result = await client.GET("/team/employees", { signal: operation.signal, cache: "no-store" });
    if (!operation.valid()) return;
    if (result.response.ok && Array.isArray(result.data)) employees.value = result.data;
    else if (result.response.status === 403) emit("forbidden");
    else notice.value = "员工列表读取失败，请刷新详情。";
  } catch { if (operation.valid()) notice.value = "员工列表读取失败，请刷新详情。"; }
});
async function transfer() {
  if (busy.value || uncertain.value || props.active || !props.allocation.employee_id ||
    !choices.value.some(e => e.employee_id === target.value) || !reason.value.trim()) return;
  const operation = gate.begin("transfer");
  if (!operation?.valid()) return;
  busy.value = true; notice.value = "";
  try {
    const result = await client.POST("/prospects/inventory/allocations/{allocation_id}/transfer", {
      params: { path: { allocation_id: props.allocation.id } }, signal: operation.signal,
      body: { employee_id: target.value, expected_employee_id: props.allocation.employee_id,
        expected_assigned_at: props.allocation.assigned_at ?? null, reason: reason.value.trim() },
    });
    if (!operation.valid()) return;
    if (result.response.ok && result.data?.employee_id === target.value) emit("transferred");
    else if (result.response.status === 403) emit("forbidden");
    else {
      uncertain.value = true;
      notice.value = "转交未确认：请刷新核对负责人及活动跟进，不会自动重复提交。";
    }
  } catch {
    if (operation.valid()) { uncertain.value = true; notice.value = "转交结果待核对，请刷新详情确认负责人。"; }
  } finally { if (operation.valid()) busy.value = false; }
}
</script>
<template>
  <section
    class="transfer-card"
    aria-label="管理员转交客户"
  >
    <h2>转交客户</h2>
    <p v-if="currentOwner">
      当前负责人：{{ currentOwner }}
    </p>
    <p>转交后由新负责人跟进，保留客户资料和转交记录，不重复消耗搜索额度。</p>
    <p
      v-if="active"
      role="status"
    >
      请先停止正在执行的跟进，再转交客户。
    </p>
    <label>新负责人
      <select
        v-model="target"
        aria-label="新负责人"
        :disabled="busy || uncertain || active"
      >
        <option value="">选择员工</option>
        <option
          v-for="e in choices"
          :key="e.employee_id"
          :value="e.employee_id"
        >{{ e.name }}</option>
      </select>
    </label>
    <label>转交原因<textarea
      v-model="reason"
      aria-label="转交原因"
      maxlength="500"
      :disabled="busy || uncertain || active"
    /></label>
    <button
      :disabled="busy || uncertain || active || !target || !reason.trim()"
      @click="transfer"
    >
      确认转交客户
    </button>
    <p
      v-if="notice"
      role="status"
    >
      {{ notice }}
    </p>
  </section>
</template>
<style scoped>
.transfer-card { display: grid; gap: 12px; padding: 20px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface); min-width: 0; }
label { display: grid; gap: 6px; }
select, textarea { width: 100%; min-width: 0; }
button { justify-self: start; }
</style>
