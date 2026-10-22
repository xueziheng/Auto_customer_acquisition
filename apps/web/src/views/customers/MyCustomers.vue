<script setup lang="ts">
import { computed, inject, onMounted, ref } from "vue";
import { RouterLink } from "vue-router";
import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";
import { useQuoteRequestScope } from "../costing-quotes/quote-request-scope";
import CustomerFollowup from "./CustomerFollowup.vue";
import { followupRecovery } from "./followupRecovery";
const client = inject<ReturnType<typeof createApiClient>>(
  "tradeos-api-client",
  apiClient,
);
const customers = ref<components["schemas"]["CustomerAllocationView"][]>([]),
  selected = ref<string[]>([]);
const loading = ref(false),
  batchBusy = ref(false),
  error = ref(""),
  nextCursor = ref<string | null>(null);
const batchMessage = ref("");
const refreshRevision = ref(0),
  pageSize = 50;
const panels = new Map<string, InstanceType<typeof CustomerFollowup>>();
const recovery = followupRecovery(client);
const pendingIds = computed(() =>
  recovery.records.value.map((p) => p.allocationId),
);
const followupIds = computed(() => [
  ...new Set([...selected.value, ...pendingIds.value]),
]);
const gate = useQuoteRequestScope(client, () => [], reset);
function reset() {
  batchMessage.value = "";
  customers.value = [];
  selected.value = [];
  panels.clear();
  error.value = "";
  nextCursor.value = null;
  loading.value = false;
  batchBusy.value = false;
}
function forbidden() {
  gate.invalidate();
  recovery.clearAll();
  reset();
  error.value = "当前身份无权读取客户资料，请核对账号。";
}
async function load(more = false) {
  if (
    !gate.hasIdentity.value ||
    batchBusy.value ||
    (more && (!nextCursor.value || loading.value))
  )
    return;
  const operation = gate.begin("list");
  if (!operation?.valid()) return;
  loading.value = true;
  error.value = "";
  if (!more) {
    customers.value = [];
    selected.value = [];
    panels.clear();
    refreshRevision.value++;
  }
  try {
    const result = await client.GET("/prospects/customers", {
      params: {
        query: {
          limit: pageSize,
          ...(more && nextCursor.value ? { cursor: nextCursor.value } : {}),
        },
      },
      signal: operation.signal,
      cache: "no-store",
    });
    if (!operation.valid()) return;
    if (result.response.ok && result.data) {
      const known = new Set(customers.value.map((c) => c.id));
      customers.value = more
        ? [...customers.value, ...result.data.filter((c) => !known.has(c.id))]
        : result.data;
      nextCursor.value =
        result.data.length === pageSize ? result.data.at(-1)!.id : null;
    } else if (result.response.status === 403) forbidden();
    else {
      reset();
      error.value = "客户读取失败，请刷新重试。";
    }
  } catch {
    if (operation.valid()) {
      reset();
      error.value = "客户读取失败，请刷新重试。";
    }
  } finally {
    if (operation.valid()) loading.value = false;
  }
}
function selectLoaded(checked: boolean) {
  selected.value = checked
    ? customers.value.slice(0, 100).map((c) => c.id)
    : [];
}
function toggle(id: string, checked: boolean) {
  selected.value = checked
    ? [...new Set([...selected.value, id])].slice(0, 100)
    : selected.value.filter((v) => v !== id);
}
async function startBatch() {
  if (batchBusy.value || !selected.value.length) return;
  const operation = gate.begin("batch");
  if (!operation?.valid()) return;
  batchBusy.value = true;
  batchMessage.value = "";
  try {
    // 每条保留自己的任务、联系人、假设、版本和恢复键；不是跨客户事务。
    for (const id of [...selected.value]) {
      if (!operation.valid()) break;
      await panels.get(id)?.start();
    }
  } catch {
    if (operation.valid()) batchMessage.value = "批量跟进未完成，请核对各客户的结果后重试。";
  } finally {
    if (operation.valid()) batchBusy.value = false;
  }
}
onMounted(() => void load());
</script>
<template>
  <div class="shell customers-shell">
    <div class="page-head">
      <h1>我的客户</h1>
      <span class="meta">仅显示分配给您的客户</span><button
        :disabled="loading || batchBusy"
        @click="load()"
      >
        刷新客户
      </button>
    </div>
    <p
      v-if="batchMessage"
      role="status"
    >
      {{ batchMessage }}
    </p>
    <p
      v-if="error"
      role="alert"
    >
      {{ error }}
    </p>
    <p
      v-if="loading"
      role="status"
    >
      正在读取客户…
    </p>
    <div
      v-if="customers.length"
      class="customer-actions"
    >
      <label><input
        type="checkbox"
        :checked="selected.length > 0"
        :disabled="batchBusy"
        @change="selectLoaded(($event.target as HTMLInputElement).checked)"
      >选择当前已加载范围（最多100条）</label>
      <span>已选择 {{ selected.length }} 家</span><button
        :disabled="batchBusy || !selected.length"
        @click="startBatch"
      >
        {{ batchBusy ? "逐项提交中…" : "逐项启动所选跟进" }}
      </button>
      <p class="meta">
        为每家客户选择开发任务、已验证邮箱和来源假设；逐条提交并展示结果。
      </p>
    </div>
    <p v-else-if="!loading && !error">
      暂无本人客户，请联系管理员分配。
    </p>
    <div class="customer-grid">
      <article
        v-for="c in customers"
        :key="c.id"
        class="customer-card"
      >
        <label class="customer-title"><input
          type="checkbox"
          :checked="selected.includes(c.id)"
          :aria-label="`选择 ${c.candidate.name}`"
          :disabled="
            batchBusy || (!selected.includes(c.id) && selected.length >= 100)
          "
          @change="toggle(c.id, ($event.target as HTMLInputElement).checked)"
        ><RouterLink
          :to="{ name: 'customer-detail', params: { allocationId: c.id } }"
        >{{ c.candidate.name }}</RouterLink></label>
        <p>{{ c.candidate.country }} · {{ c.candidate.canonical_domain }}</p>
        <p>
          {{
            c.account_id
              ? "档案已关联，验证及跟进状态见详情"
              : "客户档案待关联，请管理员处理"
          }}
        </p>
        <p class="meta">
          公开资料，需求尚未验证
        </p>
      </article>
    </div>
    <button
      v-if="nextCursor"
      :disabled="loading || batchBusy"
      @click="load(true)"
    >
      加载更多本人客户
    </button>
    <section
      v-if="followupIds.length && !error"
      class="followup-grid"
      aria-label="逐客户跟进结果"
    >
      <article
        v-for="id in followupIds"
        :key="id"
      >
        <h2>
          {{
            customers.find((c) => c.id === id)?.candidate.name ?? "待核对客户"
          }}
        </h2>
        <RouterLink
          :to="{ name: 'customer-detail', params: { allocationId: id } }"
        >
          查看客户详情
        </RouterLink><CustomerFollowup
          :ref="
            (value) => {
              if (value)
                panels.set(id, value as InstanceType<typeof CustomerFollowup>);
              else panels.delete(id);
            }
          "
          :allocation-id="id"
          :locked="batchBusy"
          :refresh-revision="refreshRevision"
          @forbidden="forbidden"
        />
      </article>
    </section>
  </div>
</template>
<style scoped>
.customers-shell {
  overflow: auto;
  min-width: 0;
}
.customer-actions {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.customer-actions p {
  flex-basis: 100%;
}
.customer-grid,
.followup-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 320px), 1fr));
  gap: 16px;
}
.customer-card {
  display: grid;
  gap: 8px;
  padding: 16px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--surface);
  min-width: 0;
  overflow-wrap: anywhere;
}
.customer-title {
  display: flex;
  gap: 8px;
  align-items: start;
  font-weight: 600;
}
.customer-title input {
  flex-shrink: 0;
  margin-top: 4px;
}
.followup-grid article {
  min-width: 0;
}
.followup-grid h2 {
  font-size: 16px;
  overflow-wrap: anywhere;
}
</style>
