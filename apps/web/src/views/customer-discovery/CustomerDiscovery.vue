<script setup lang="ts">
import { inject, ref, onUnmounted } from "vue";
import { apiClient, createApiClient } from "../../api/client";
import { useCustomerAccess } from "../../composables/useCustomerAccess";
import ProspectingServicePlanPanel from "../../components/ProspectingServicePlanPanel.vue";
import InventoryPanel from "./InventoryPanel.vue";
import type { components } from "../../api/api";
const scheduleQuery = ref<components["schemas"]["InventoryQuery"] | null>(null);
const client = inject<ReturnType<typeof createApiClient>>(
  "tradeos-api-client",
  apiClient,
);
const unsubscribe = client.subscribeIdentity(() => { scheduleQuery.value = null; });
onUnmounted(unsubscribe);
const { access, loading, error, load } = useCustomerAccess(client);
</script>
<template>
  <div class="shell discovery-shell">
    <div class="page-head">
      <h1>找客户</h1>
      <span class="meta">搜索公开企业资料，分配后由负责人跟进</span>
    </div>
    <p
      v-if="loading"
      role="status"
    >
      正在确认权限…
    </p>
    <div
      v-else-if="error"
      role="alert"
    >
      <p>{{ error }}</p>
      <button @click="load">
        重试权限读取
      </button>
    </div>
    <template v-else-if="access?.can_search && access.can_assign">
      <InventoryPanel @schedule-query="scheduleQuery = $event" />
      <ProspectingServicePlanPanel :query="scheduleQuery" />
    </template>
    <p v-else>
      找客户与分配由企业管理员操作。请在“我的客户”查看分配给您的客户。
    </p>
  </div>
</template>
<style scoped>
.discovery-shell {
  overflow: auto;
  min-width: 0;
}
</style>
