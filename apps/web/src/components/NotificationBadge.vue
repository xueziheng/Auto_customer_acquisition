<script setup lang="ts">
/* global window */
import { computed, inject, onBeforeUnmount, onMounted, ref } from "vue";
import { useQuoteRequestScope } from "../views/costing-quotes/quote-request-scope";
import { RouterLink } from "vue-router";

import { apiClient, createApiClient } from "../api/client";

type ApiClient = ReturnType<typeof createApiClient>;

const client = inject<ApiClient>("tradeos-api-client", apiClient);
const unreadCount = ref<number | null>(null);
const stale = ref(false);
let requestVersion = 0;

const gate = useQuoteRequestScope(client, () => [], () => {
  requestVersion++; unreadCount.value = null; stale.value = false;
  globalThis.queueMicrotask(() => void refresh());
});

const NOTIFICATIONS_CHANGED = "tradeos:notifications-changed";

async function refresh(): Promise<void> {
  const op = gate.begin("count"); if (!op?.valid()) return;
  const version = ++requestVersion;
  const previous = unreadCount.value;
  try {
    const { data, response } = await client.GET("/notifications", {
      params: { query: { limit: 100 } },
    });
    if (!op.valid() || version !== requestVersion) return; // stale response 忽略
    if (response.status !== 200) {
      if ([401, 403].includes(response.status)) { unreadCount.value = null; stale.value = true; return; }
      // 失败不清零：保留最近可信计数并标记过期
      if (unreadCount.value === null && previous !== null) unreadCount.value = previous;
      stale.value = true;
      return;
    }
    const items = data ?? [];
    unreadCount.value = items.filter((item) => item.read_at === null).length;
    stale.value = false;
  } catch {
    if (!op.valid() || version !== requestVersion) return;
    if (unreadCount.value === null && previous !== null) unreadCount.value = previous;
    stale.value = true;
  }
}

onMounted(() => {
  void refresh();
  window.addEventListener(NOTIFICATIONS_CHANGED, onNotificationsChanged);
});

onBeforeUnmount(() => {
  window.removeEventListener(NOTIFICATIONS_CHANGED, onNotificationsChanged);
});

function onNotificationsChanged(): void {
  void refresh();
}

const display = computed(() => {
  if (unreadCount.value === null) return "未知";
  const count = unreadCount.value;
  return count > 99 ? "99+" : String(count);
});
</script>

<template>
  <RouterLink
    class="badge"
    to="/notifications"
    :aria-label="`通知，${display} 条未读`"
  >
    <span>通知</span>
    <span
      class="badge-num"
      aria-hidden="true"
    >{{ display }}</span>
    <span>未读</span>
    <span
      v-if="stale"
      class="badge-stale"
      role="status"
    >状态可能已过期</span>
  </RouterLink>
</template>

<style scoped>
.badge {
  display: inline-flex;
  align-items: center;
  flex-shrink: 0;
  gap: 6px;
  color: var(--topbar-text);
  text-decoration: none;
  padding: 6px 10px;
  border-radius: var(--radius-sm);
  background: rgba(255, 255, 255, 0.14);
  white-space: nowrap;
}
.badge-num {
  background: var(--danger);
  color: #fff;
  font-weight: 700;
  border-radius: 999px;
  min-width: 20px;
  text-align: center;
  padding: 0 6px;
  font-size: 12px;
}
.badge-stale {
  font-size: 11px;
  color: var(--warning-soft);
}
@media (max-width: 700px) {
  .badge-stale {
    display: none;
  }
}
</style>
