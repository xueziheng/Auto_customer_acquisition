<script setup lang="ts">
import { inject, ref } from "vue";
import type { components } from "../api/api";
import { apiClient, createApiClient } from "../api/client";
import ResearchRunSummary from "./ResearchRunSummary.vue";

const client = inject<ReturnType<typeof createApiClient>>("tradeos-api-client", apiClient);
const run = ref<components["schemas"]["RunSummaryView"] | null>(null);
const loading = ref(false);
const failed = ref(false);
let version = 0;
async function refresh(): Promise<void> {
  const request = ++version;
  loading.value = true;
  failed.value = false;
  try {
    const result = await client.GET("/runs", { params: { query: { workflow_type: "demand_discovery", limit: 10 } } });
    if (request !== version) return;
    if (result.response.status !== 200 || !result.data) {
      failed.value = true;
      return;
    }
    run.value = result.data?.find((item) => item.research) ?? null;
  } catch {
    if (request === version) failed.value = true;
  } finally {
    if (request === version) loading.value = false;
  }
}
defineExpose({ refresh });
</script>

<template>
  <div
    class="recent-research"
  >
    <p
      v-if="loading"
      role="status"
    >
      正在刷新研究运行摘要…
    </p>
    <p
      v-else-if="failed"
      role="alert"
    >
      研究运行摘要读取失败，请刷新；已有摘要仅为上次读取结果。
    </p>
    <p v-else-if="!run">
      最近记录中暂无研究运行记录摘要。
    </p>
    <ResearchRunSummary
      v-if="run?.research"
      :research="run.research"
    />
    <RouterLink
      v-if="run"
      :to="{ path: '/runs', query: { run: run.run_id } }"
    >
      查看最近研究运行记录的完整审计 →
    </RouterLink>
  </div>
</template>

<style scoped>
.recent-research { flex-shrink: 0; display: grid; gap: 8px; } a { color: var(--action); }
</style>
