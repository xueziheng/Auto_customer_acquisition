<script setup lang="ts">
/* global CustomEvent, window */
import { computed, inject, onMounted, ref } from "vue";
import { useRouter } from "vue-router";

import { apiClient, createApiClient } from "../api/client";
import type { components } from "../api/api";

type ApiClient = ReturnType<typeof createApiClient>;
type InAppNotificationView = components["schemas"]["InAppNotificationView"];

const client = inject<ApiClient>("tradeos-api-client", apiClient);
const router = useRouter();

const notifications = ref<InAppNotificationView[]>([]);
const selectedId = ref<string | null>(null);
const listLoading = ref(true);
const listError = ref<string | null>(null);
const actionError = ref<string | null>(null);
const writeLock = ref(false);
let listVersion = 0;

const priorityLabels: Record<string, { icon: string; label: string }> = {
  urgent: { icon: "●", label: "紧急" },
  normal: { icon: "●", label: "普通" },
  low: { icon: "●", label: "低" },
};

const kindLabels: Record<string, string> = {
  handoff_escalation: "人工接管升级",
  handoff_queue_backlogged: "接管队列积压",
  sending_identity_suspended: "发件身份已熔断",
  reputation_threshold_breached: "信誉阈值触发",
  commitment_overdue: "承诺处理已到期",
  approval_decided: "审批已决定",
};

function isValidRelativeLink(value: string | null): boolean {
  if (!value) return false;
  if (!value.startsWith("/") || value.startsWith("//")) return false;
  // same-origin：绝对 URL 拒绝；相对单斜杠路径且 router 有已注册路由
  try {
    const resolved = router.resolve(value);
    return resolved.matched.length > 0;
  } catch {
    return false;
  }
}

function priorityOf(item: InAppNotificationView): { icon: string; label: string } {
  return priorityLabels[item.priority] ?? { icon: "●", label: item.priority };
}

function kindLabel(item: InAppNotificationView): string {
  return kindLabels[item.context.kind] ?? item.context.kind;
}

async function loadNotifications(): Promise<void> {
  const version = ++listVersion;
  listLoading.value = true;
  listError.value = null;
  const { data, response } = await client.GET("/notifications", {
    params: { query: { limit: 100 } },
  });
  if (version !== listVersion) return;
  listLoading.value = false;
  if (response.status === 403) {
    listError.value = "当前无法访问通知";
    return;
  }
  if (response.status !== 200) {
    listError.value = "通知加载失败，请刷新后重试";
    return;
  }
  notifications.value = data ?? [];
  if (
    selectedId.value !== null &&
    !notifications.value.some((item) => item.notification_id === selectedId.value)
  ) {
    selectedId.value = null;
  }
}

const selectedNotification = computed(() =>
  notifications.value.find((item) => item.notification_id === selectedId.value) ?? null,
);

async function markRead(): Promise<void> {
  if (!selectedNotification.value || writeLock.value) return; // 同一 item 写锁
  const target = selectedNotification.value;
  writeLock.value = true;
  actionError.value = null;
  const { data, response } = await client.POST(
    "/notifications/{notification_id}/read",
    {
      params: { path: { notification_id: target.notification_id } },
    },
  );
  writeLock.value = false;
  if (response.status === 200) {
    // 通知已变化：通知顶部徽标应重新拉取计数
    window.dispatchEvent(new CustomEvent("tradeos:notifications-changed"));
    // 接受后端返回 DTO；只允许 read_at null→datetime 的前进（单调）
    const updated = data;
    if (updated) {
      const index = notifications.value.findIndex(
        (item) => item.notification_id === updated.notification_id,
      );
      if (index >= 0) {
        const previous = notifications.value[index];
        if (previous.read_at === null && updated.read_at !== null) {
          notifications.value = notifications.value.map((item) =>
            item.notification_id === updated.notification_id ? updated : item,
          );
        }
      }
    }
    return;
  }
  actionError.value = "无法读取或更新该通知";
}

const unreadCount = computed(
  () => notifications.value.filter((item) => item.read_at === null).length,
);

onMounted(() => {
  void loadNotifications();
});
</script>

<template>
  <div class="shell">
    <div class="page-head">
      <h1>通知中心</h1>
      <span class="meta">仅显示本人通知 · {{ unreadCount }} 未读</span>
    </div>
    <div v-if="listLoading" class="state" role="status">正在加载通知…</div>
    <div v-else-if="listError" class="state" role="alert">{{ listError }}</div>
    <section v-else class="inbox" aria-label="通知列表">
      <div class="ledger">
        <div class="ledger-head">
          <span>通知</span>
          <span>{{ unreadCount }} 未读</span>
        </div>
        <ol class="ledger-list">
          <li
            v-for="item in notifications"
            :key="item.notification_id"
            class="row"
            :class="{ selected: item.notification_id === selectedId, unread: item.read_at === null, read: item.read_at !== null }"
            tabindex="0"
            role="button"
            @click="selectedId = item.notification_id"
            @keydown.enter="selectedId = item.notification_id"
          >
            <div class="title">
              <span class="prio" :class="item.priority">
                {{ priorityOf(item).icon }} {{ priorityOf(item).label }}
              </span>
              <span>{{ item.title }}</span>
            </div>
            <div class="sub">
              {{ item.notification_id }} · {{ item.created_at }} ·
              <span class="read-state" :class="item.read_at === null ? 'unread-tag' : 'read-tag'">
                {{ item.read_at === null ? "未读" : "已读" }}
              </span>
            </div>
          </li>
        </ol>
      </div>
      <div class="detail" aria-label="通知详情">
        <template v-if="selectedNotification">
          <h2>{{ selectedNotification.title }}</h2>
          <dl class="kv">
            <dt>类型</dt>
            <dd>{{ kindLabel(selectedNotification) }}（{{ selectedNotification.context.kind }}）</dd>
            <dt>优先级</dt>
            <dd>
              <span class="prio" :class="selectedNotification.priority">
                {{ priorityOf(selectedNotification).icon }}
                {{ priorityOf(selectedNotification).label }}
              </span>
            </dd>
            <dt>发生时间</dt><dd>{{ selectedNotification.created_at }}</dd>
            <dt>已读</dt>
            <dd>
              <span class="read-state" :class="selectedNotification.read_at === null ? 'unread-tag' : 'read-tag'">
                {{ selectedNotification.read_at === null ? "未读" : "已读" }}
              </span>
            </dd>
          </dl>
          <div class="actions">
            <RouterLink
              v-if="isValidRelativeLink(selectedNotification.relative_link)"
              class="btn-primary"
              :to="selectedNotification.relative_link!"
            >
              前往处理
            </RouterLink>
            <button :disabled="writeLock" @click="markRead">标记为已读</button>
          </div>
          <div v-if="actionError" class="safe-banner" role="alert">
            <span aria-hidden="true">⚠</span>
            <div>{{ actionError }}</div>
          </div>
        </template>
        <div v-else class="empty-detail">请选择一条通知</div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.state {
  color: var(--text-secondary);
  padding: var(--space4) 0;
}
.inbox {
  display: flex;
  gap: var(--space4);
  flex: 1;
  min-height: 0;
}
.ledger {
  width: 380px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.ledger-head {
  padding: var(--space3) var(--space4);
  border-bottom: 1px solid var(--border);
  font-weight: 600;
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.ledger-list {
  overflow-y: auto;
  flex: 1;
  min-height: 0;
  list-style: none;
}
.row {
  padding: var(--space3) var(--space4);
  border-bottom: 1px solid var(--border);
  cursor: pointer;
}
.row.selected {
  background: var(--fact-soft);
  border-left: 4px solid var(--fact);
}
.row.unread .title {
  font-weight: 700;
  border-left: 3px solid var(--action);
  padding-left: 8px;
}
.row.read .title {
  font-weight: 400;
  border-left: 3px solid transparent;
  padding-left: 8px;
}
.row .title {
  display: flex;
  gap: var(--space2);
  align-items: center;
  flex-wrap: wrap;
}
.row .sub {
  color: var(--text-secondary);
  font-size: 12px;
  margin-top: 2px;
}
.read-state {
  font-size: 11px;
  border-radius: 999px;
  padding: 0 6px;
  border: 1px solid;
}
.read-state.unread-tag {
  color: var(--action);
  border-color: var(--action);
  background: var(--fact-soft);
}
.read-state.read-tag {
  color: var(--text-secondary);
  border-color: var(--border);
}
.prio {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  border-radius: 999px;
  padding: 0 6px;
  border: 1px solid;
}
.prio.urgent {
  color: var(--danger);
  border-color: var(--danger);
  background: var(--danger-soft);
}
.prio.normal {
  color: var(--fact);
  border-color: var(--fact);
  background: var(--fact-soft);
}
.prio.low {
  color: var(--text-secondary);
  border-color: var(--border);
}
.detail {
  flex: 1;
  min-width: 0;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: var(--space4) var(--space5);
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: var(--space4);
}
.kv {
  display: grid;
  grid-template-columns: 110px 1fr;
  gap: var(--space2) var(--space4);
  font-size: 13px;
}
.kv dt {
  color: var(--text-secondary);
}
.kv dd {
  margin: 0;
  word-break: break-word;
}
.actions {
  display: flex;
  gap: var(--space3);
}
.btn-primary {
  text-decoration: none;
}
.empty-detail {
  color: var(--text-secondary);
}
@media (max-width: 1240px) {
  .ledger {
    width: 340px;
  }
}
</style>
