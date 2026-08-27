<script setup lang="ts">
import { inject, onMounted, ref } from "vue";
import type { components } from "../api/api";
import { apiClient, createApiClient } from "../api/client";
import ResearchRunSummary from "./ResearchRunSummary.vue";

const client = inject<ReturnType<typeof createApiClient>>("tradeos-api-client", apiClient);
const run = ref<components["schemas"]["RunSummaryView"] | null>(null);
onMounted(async () => {
  try {
    const result = await client.GET("/runs", { params: { query: { workflow_type: "demand_discovery", limit: 10 } } });
    run.value = result.data?.find((item) => item.research) ?? null;
  } catch { run.value = null; }
});
</script>

<template>
  <div
    v-if="run?.research"
    class="recent-research"
  >
    <ResearchRunSummary :research="run.research" />
    <RouterLink :to="{ path: '/runs', query: { run: run.run_id } }">
      查看最近研究 Run 的完整审计 →
    </RouterLink>
  </div>
</template>

<style scoped>
.recent-research { flex-shrink: 0; display: grid; gap: 8px; } a { color: var(--action); }
</style>
