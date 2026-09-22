<script setup lang="ts">
/* global document, HTMLButtonElement, KeyboardEvent, Response */
import { computed, inject, onBeforeUnmount, onMounted, ref } from "vue";

import { apiClient, createApiClient } from "../api/client";
import type { components } from "../api/api";

type ApiClient = ReturnType<typeof createApiClient>;
type EnrollmentView = components["schemas"]["EnrollmentView"];

const client = inject<ApiClient>("tradeos-api-client", apiClient);

const enrollments = ref<EnrollmentView[]>([]);
const selectedId = ref<string | null>(null);
const listLoading = ref(true);
const listError = ref<string | null>(null);
const listRetryAfter = ref<string | null>(null);
const actionError = ref<string | null>(null);
const drawerOpen = ref(false);
const drawerBusy = ref(false);
const subject = ref("");
const body = ref("");
const prepareFocus = ref<HTMLButtonElement | null>(null);
let listVersion = 0;
let prepareVersion = 0;
let sendVersion = 0;

const stateLabels: Record<string, string> = {
  enrolled: "已入组",
  in_sequence: "序列中",
  replied: "已回复",
  completed: "已完成",
  stopped_suppressed: "已停止（抑制）",
  stopped_bounced: "已停止（退信）",
  stopped_manual: "已停止（人工）",
  stopped_identity_unavailable: "已停止（身份不可用）",
};

function manualRetryNotice(response: Response): string | null {
  const value = response.headers.get("retry-after");
  if (!value) return null;
  return /^\d+$/.test(value) ? `${value} 秒后可手工重试` : "请在服务建议时间后手工重试";
}

async function loadEnrollments(): Promise<void> {
  const version = ++listVersion;
  listLoading.value = true;
  listError.value = null;
  const { data, response } = await client.GET("/crm/enrollments", {
    params: { query: { limit: 200 } },
  });
  if (version !== listVersion) return;
  listLoading.value = false;
  if (response.status === 403) {
    listError.value = "当前没有可访问的触达任务";
    return;
  }
  if (response.status === 503) {
    listError.value = "触达服务暂不可用";
    listRetryAfter.value = manualRetryNotice(response);
    return;
  }
  if (response.status !== 200) {
    listError.value = "触达任务加载失败，请刷新后重试";
    return;
  }
  const items = data ?? [];
  enrollments.value = items;
  if (selectedId.value !== null && !items.some((item) => item.enrollment_id === selectedId.value)) {
    selectedId.value = null;
  }
}

const selectedEnrollment = computed(() =>
  enrollments.value.find((item) => item.enrollment_id === selectedId.value) ?? null,
);

function statusLabel(value: string): string {
  return stateLabels[value] ?? value;
}

function openDrawer(): void {
  actionError.value = null;
  drawerOpen.value = true;
  drawerBusy.value = false;
}

function closeDrawer(clearDraft: boolean): void {
  drawerOpen.value = false;
  if (clearDraft) {
    subject.value = "";
    body.value = "";
  }
  prepareFocus.value?.focus();
}

function onEscape(event: KeyboardEvent): void {
  if (event.key === "Escape" && drawerOpen.value) {
    closeDrawer(true);
  }
}

async function prepareAttempt(): Promise<void> {
  if (!selectedEnrollment.value || drawerBusy.value) return;
  drawerBusy.value = true; // 双击锁
  actionError.value = null;
  actionRetryAfter.value = null;
  const version = ++prepareVersion;
  const { data, response } = await client.POST(
    "/crm/enrollments/{enrollment_id}/attempts/prepare",
    {
      params: { path: { enrollment_id: selectedEnrollment.value.enrollment_id } },
    },
  );
  if (version !== prepareVersion) return; // stale response 忽略
  drawerBusy.value = false;
  if (response.status === 200) {
    if (data) selectedAttemptId.value = data.attempt_id;
    openDrawer();
    return;
  }
  if (response.status === 403) {
    actionError.value = "当前没有可访问的触达任务";
    return;
  }
  actionError.value = "无法准备发送，请稍后重试";
}

function sendResultCopy(status: number, bodyJson: unknown): string {
  if (status === 200) {
    const payload = (bodyJson ?? {}) as { duplicate?: boolean };
    return payload.duplicate ? "已识别为重复请求，未再次发送" : "发送成功";
  }
  if (status === 403) return "权限已变化，编辑器已关闭";
  if (status === 429) return "发送额度暂不可用";
  if (status === 503) return "发送服务暂不可用，当前事实可能已变化";
  const payload = (bodyJson ?? {}) as { code?: string };
  if (payload.code === "idempotency_conflict") return "请求标识与内容不一致，请重新准备";
  if (payload.code === "reconciliation_required") return "发送状态待人工核对，请勿重复发送";
  if (payload.code === "in_progress") return "发送请求正在处理中，请稍后刷新";
  return "发送失败，请刷新后重试";
}

async function sendAttempt(): Promise<void> {
  if (
    !selectedEnrollment.value ||
    drawerBusy.value ||
    !subject.value ||
    !body.value ||
    selectedAttemptId.value === null
  ) {
    return;
  }
  drawerBusy.value = true; // 双击锁
  const version = ++sendVersion;
  const { data, error, response } = await client.POST(
    "/crm/message-attempts/{attempt_id}/send",
    {
      params: { path: { attempt_id: selectedAttemptId.value } },
      body: { subject: subject.value, body: body.value },
    },
  );
  if (version !== sendVersion) return; // stale response 忽略
  drawerBusy.value = false;
  if (response.status === 200) {
    closeDrawer(true);
    actionError.value = data?.duplicate ? "已识别为重复请求，未再次发送" : "发送成功";
    await loadEnrollments();
    return;
  }
  if (response.status === 403) {
    closeDrawer(true);
    actionError.value = "权限已变化，编辑器已关闭";
    return;
  }
  actionError.value = sendResultCopy(response.status, error);
  if (response.status === 429) {
    actionRetryAfter.value = manualRetryNotice(response);
  }
}

const actionRetryAfter = ref<string | null>(null);
const selectedAttemptId = ref<string | null>(null);

onMounted(() => {
  void loadEnrollments();
  document.addEventListener("keydown", onEscape);
});

onBeforeUnmount(() => {
  document.removeEventListener("keydown", onEscape);
});
</script>

<template>
  <div class="shell">
    <div class="page-head">
      <h1>触达工作台</h1>
      <span class="meta">列表保持后端顺序</span>
    </div>
    <section
      class="workbench"
      aria-label="触达任务"
      :aria-busy="listLoading ? 'true' : undefined"
    >
      <div class="ledger">
        <div class="ledger-head">
          <span>入组记录</span>
          <button @click="loadEnrollments">刷新列表</button>
        </div>
        <div v-if="listLoading" class="ledger-state" role="status">正在加载触达任务…</div>
        <div v-else-if="listError" class="ledger-state" role="alert">
          {{ listError }}
          <span v-if="listRetryAfter">（{{ listRetryAfter }}）</span>
        </div>
        <div v-else-if="enrollments.length === 0" class="ledger-state">
          当前没有待处理的触达任务
        </div>
        <ol v-else class="ledger-list">
          <li
            v-for="item in enrollments"
            :key="item.enrollment_id"
            aria-label="入组记录"
            class="row"
            :class="{ selected: item.enrollment_id === selectedId }"
            tabindex="0"
            role="button"
            @click="selectedId = item.enrollment_id"
            @keydown.enter="selectedId = item.enrollment_id"
          >
            <div class="name">{{ item.account_id }}</div>
            <div class="sub">
              {{ item.enrollment_id }} · 步骤 {{ item.current_step }} · 下次发送
              {{ item.next_send_at ?? "—" }}
            </div>
            <span class="status" :class="item.state">{{ statusLabel(item.state) }}</span>
          </li>
        </ol>
      </div>
      <div class="detail" aria-label="选中入组记录详情">
        <template v-if="selectedEnrollment">
          <h2>入组记录详情</h2>
          <dl class="kv">
            <dt>入组记录</dt><dd>{{ selectedEnrollment.enrollment_id }}</dd>
            <dt>活动</dt><dd>{{ selectedEnrollment.campaign_id }}</dd>
            <dt>企业</dt><dd>{{ selectedEnrollment.account_id }}</dd>
            <dt>联系人</dt><dd>{{ selectedEnrollment.contact_point_id }}</dd>
            <dt>发件身份</dt><dd>{{ selectedEnrollment.sending_identity_id }}</dd>
            <dt>当前步骤</dt><dd>{{ selectedEnrollment.current_step }}</dd>
            <dt>下次发送</dt><dd>{{ selectedEnrollment.next_send_at ?? "—" }}</dd>
            <dt>状态</dt><dd>{{ statusLabel(selectedEnrollment.state) }}</dd>
          </dl>
          <div class="actions">
            <button
              ref="prepareFocus"
              class="btn-primary"
              :disabled="drawerBusy"
              @click="prepareAttempt"
            >
              准备发送
            </button>
          </div>
        </template>
        <div v-else-if="!listError && !listLoading" class="empty-detail">
          请选择一条入组记录
        </div>
        <div
          v-if="actionError"
          class="safe-banner"
          role="alert"
        >
          <span aria-hidden="true">ℹ</span>
          <div>
            {{ actionError }}
            <span v-if="actionRetryAfter">（{{ actionRetryAfter }}）</span>
          </div>
        </div>
      </div>
    </section>

    <div v-if="drawerOpen" class="drawer-mask" aria-hidden="true"></div>
    <section
      v-if="drawerOpen"
      class="drawer"
      role="dialog"
      aria-modal="true"
      aria-label="发送编辑"
    >
      <div class="drawer-head">
        <h2>发送编辑（邮件草稿）</h2>
        <button aria-label="关闭并清空草稿" @click="closeDrawer(true)">Esc ✕</button>
      </div>
      <div class="drawer-body">
        <div class="field">
          <label for="send-subject">邮件主题</label>
          <input id="send-subject" v-model="subject" type="text" maxlength="998" />
          <div class="hint">发给客户的内容请使用英文。</div>
        </div>
        <div class="field">
          <label for="send-body">邮件正文</label>
          <textarea id="send-body" v-model="body" maxlength="100000"></textarea>
          <div class="hint">发给客户的内容请使用英文。</div>
        </div>
      </div>
      <div class="drawer-foot">
        <button @click="closeDrawer(true)">取消</button>
        <button
          class="btn-primary"
          :disabled="drawerBusy || !subject || !body"
          @click="sendAttempt"
        >
          发送
        </button>
      </div>
    </section>
  </div>
</template>

<style scoped>
.workbench {
  display: flex;
  gap: var(--space4);
  flex: 1;
  min-height: 0;
}
.ledger {
  width: 320px;
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
  gap: var(--space3);
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
.row .name {
  font-weight: 600;
}
.row .sub {
  color: var(--text-secondary);
  font-size: 12px;
}
.ledger-state {
  padding: var(--space4);
  color: var(--text-secondary);
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
  grid-template-columns: 150px 1fr;
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
.empty-detail {
  color: var(--text-secondary);
}
.drawer-mask {
  position: fixed;
  inset: 0;
  background: rgba(23, 35, 35, 0.35);
  z-index: 10;
}
.drawer {
  position: fixed;
  top: 56px;
  right: 0;
  bottom: 0;
  width: 440px;
  background: var(--surface);
  border-left: 1px solid var(--border);
  box-shadow: -8px 0 24px rgba(23, 35, 35, 0.15);
  display: flex;
  flex-direction: column;
  z-index: 11;
}
.drawer-head {
  padding: var(--space4);
  border-bottom: 1px solid var(--border);
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.drawer-head h2 {
  font-size: 16px;
}
.drawer-body {
  flex: 1;
  overflow-y: auto;
  padding: var(--space4) var(--space5);
  display: flex;
  flex-direction: column;
  gap: var(--space4);
}
.field label {
  display: block;
  font-weight: 600;
  margin-bottom: var(--space2);
}
.field .hint {
  color: var(--text-secondary);
  font-size: 12px;
  margin-top: var(--space2);
}
input[type="text"],
textarea {
  width: 100%;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 8px 10px;
  color: var(--text-primary);
  background: var(--surface);
}
textarea {
  min-height: 120px;
  resize: vertical;
}
.drawer-foot {
  padding: var(--space4);
  border-top: 1px solid var(--border);
  display: flex;
  gap: var(--space3);
  justify-content: flex-end;
}
@media (max-width: 1240px) {
  .ledger {
    width: 300px;
  }
}
@media (max-width: 1180px) {
  .drawer {
    width: min(440px, 100%);
  }
}
</style>
