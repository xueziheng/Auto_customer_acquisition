<script setup lang="ts">
/* global Event, HTMLInputElement */
import { computed, inject, onMounted, ref } from "vue";

import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";

type ApiClient = ReturnType<typeof createApiClient>;
type Commitment = components["schemas"]["CommitmentView"];

const client = inject<ApiClient>("tradeos-api-client", apiClient);
const commitments = ref<Commitment[]>([]);
const loading = ref(true);
const actingId = ref<string | null>(null);
const error = ref<string | null>(null);
const includeFulfilled = ref(false);
const corrections = ref<Record<string, string>>({});

const pendingCount = computed(
  () => commitments.value.filter((item) => !item.confirmed_by).length,
);
const overdueCount = computed(
  () => commitments.value.filter((item) => item.status === "overdue").length,
);
const dueSoonCount = computed(() => {
  const horizon = Date.now() + 24 * 60 * 60 * 1000;
  return commitments.value.filter((item) => {
    if (!item.confirmed_by || item.due_at_uncertain) return false;
    if (!["pending", "waiting_customer"].includes(item.status)) return false;
    const due = new Date(item.due_at).getTime();
    return Number.isFinite(due) && due <= horizon;
  }).length;
});

function formatDate(value: string | null): string {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? value
    : parsed.toLocaleString("zh-CN", { hour12: false });
}

function statusLabel(item: Commitment): string {
  if (!item.confirmed_by) return "待确认";
  const labels: Record<Commitment["status"], string> = {
    cancelled: "已取消",
    fulfilled: "已完成",
    overdue: "已逾期",
    pending: "待履约",
    waiting_customer: "等待客户",
  };
  return labels[item.status];
}

function safeError(status: number): string {
  if (status === 403) return "只能操作本人负责的承诺";
  if (status === 409) return "承诺状态已经变化，请刷新";
  if (status === 503) return "承诺服务暂不可用";
  return "请求未完成，请稍后重试";
}

function localAbsoluteTime(value: string): string | null {
  if (!value) return null;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return null;
  const offsetMinutes = -parsed.getTimezoneOffset();
  const sign = offsetMinutes >= 0 ? "+" : "-";
  const hours = String(Math.floor(Math.abs(offsetMinutes) / 60)).padStart(2, "0");
  const minutes = String(Math.abs(offsetMinutes) % 60).padStart(2, "0");
  return `${value}:00${sign}${hours}:${minutes}`;
}

function updateCorrection(commitmentId: string, event: Event): void {
  corrections.value[commitmentId] = (event.target as HTMLInputElement).value;
}

async function loadCommitments(): Promise<void> {
  loading.value = true;
  error.value = null;
  try {
    const result = await client.GET("/commitments", {
      params: { query: { include_fulfilled: includeFulfilled.value } },
    });
    if (result.response.status !== 200 || !result.data) {
      error.value = safeError(result.response.status);
      return;
    }
    commitments.value = result.data;
  } catch {
    error.value = "无法连接服务，请稍后重试";
  } finally {
    loading.value = false;
  }
}

async function confirm(item: Commitment): Promise<void> {
  if (actingId.value) return;
  const rawCorrection = corrections.value[item.commitment_id] ?? "";
  const correctedDueAt = localAbsoluteTime(rawCorrection);
  if (item.due_at_uncertain && correctedDueAt === null) {
    error.value = "时间不确定的承诺必须先核对到期时间";
    return;
  }
  actingId.value = item.commitment_id;
  error.value = null;
  try {
    const result = await client.POST("/commitments/{commitment_id}/confirm", {
      params: { path: { commitment_id: item.commitment_id } },
      body: { corrected_due_at: correctedDueAt },
    });
    if (result.response.status !== 204) {
      error.value = safeError(result.response.status);
      return;
    }
    await loadCommitments();
  } catch {
    error.value = "承诺确认未提交，请稍后重试";
  } finally {
    actingId.value = null;
  }
}

async function fulfill(item: Commitment): Promise<void> {
  if (actingId.value || !item.confirmed_by) return;
  actingId.value = item.commitment_id;
  error.value = null;
  try {
    const result = await client.POST("/commitments/{commitment_id}/fulfill", {
      params: { path: { commitment_id: item.commitment_id } },
    });
    if (result.response.status !== 204) {
      error.value = safeError(result.response.status);
      return;
    }
    await loadCommitments();
  } catch {
    error.value = "履约状态未更新，请稍后重试";
  } finally {
    actingId.value = null;
  }
}

onMounted(() => void loadCommitments());
</script>

<template>
  <div class="shell commitment-shell">
    <div class="page-head commitment-head">
      <div>
        <p class="phase-eyebrow">
          承诺管理
        </p><h1>承诺中心</h1>
      </div>
      <div class="head-actions">
        <label><input
          v-model="includeFulfilled"
          type="checkbox"
          @change="loadCommitments"
        >显示已完成</label>
        <button
          type="button"
          :disabled="loading"
          @click="loadCommitments"
        >
          {{ loading ? "加载中…" : "刷新" }}
        </button>
      </div>
    </div>

    <div class="safe-banner danger">
      <span aria-hidden="true">!</span><div>价格、折扣、交期、库存、认证、付款条件和合同条款永远不能由智能助手自动承诺。</div>
    </div>
    <div
      v-if="error"
      class="safe-banner danger"
      role="alert"
    >
      {{ error }}
    </div>

    <section
      class="metric-grid"
      aria-label="承诺概览"
    >
      <article><span>待确认</span><strong>{{ pendingCount }}</strong><p>确认前不进入提醒管道</p></article>
      <article><span>24 小时内到期</span><strong>{{ dueSoonCount }}</strong><p>仅统计时间明确且已确认</p></article>
      <article><span>已逾期</span><strong>{{ overdueCount }}</strong><p>员工承诺会按规则升级</p></article>
    </section>

    <section class="ledger">
      <header>
        <div>
          <p class="card-kicker">
            我的记录
          </p><h2>我负责的承诺</h2>
        </div><span>{{ commitments.length }} 条</span>
      </header>
      <div
        v-if="loading"
        class="empty"
      >
        正在读取承诺账本…
      </div>
      <div
        v-else-if="!commitments.length"
        class="empty"
      >
        当前没有需要处理的承诺
      </div>
      <article
        v-for="item in commitments"
        v-else
        :key="item.commitment_id"
        class="commitment-card"
        :class="{ overdue: item.status === 'overdue', uncertain: item.due_at_uncertain }"
        :data-commitment-id="item.commitment_id"
      >
        <header>
          <div class="status-line">
            <span class="type-pill">{{ item.commitment_type === "employee" ? "员工承诺" : "客户承诺" }}</span>
            <span class="state-pill">{{ statusLabel(item) }}</span>
            <span
              v-if="item.due_at_uncertain"
              class="uncertain-pill"
            >时间待核对</span>
          </div>
          <small>{{ item.commitment_id }}</small>
        </header>
        <h3>{{ item.action }}</h3>
        <blockquote>{{ item.verbatim }}</blockquote>
        <dl>
          <div><dt>到期时间</dt><dd>{{ formatDate(item.due_at) }}</dd></div>
          <div><dt>原始消息</dt><dd>{{ item.source_message_id }}</dd></div>
          <div><dt>提取版本</dt><dd>{{ item.extracted_by ?? "人工录入" }}</dd></div>
          <div><dt>确认记录</dt><dd>{{ item.confirmed_by ? `${item.confirmed_by} · ${formatDate(item.confirmed_at)}` : "尚未确认" }}</dd></div>
        </dl>
        <footer>
          <div
            v-if="!item.confirmed_by"
            class="confirm-panel"
          >
            <label>修正到期时间（{{ item.due_at_uncertain ? "必填" : "可选" }}）
              <input
                type="datetime-local"
                :value="corrections[item.commitment_id] ?? ''"
                @input="updateCorrection(item.commitment_id, $event)"
              >
            </label>
            <button
              class="btn-primary"
              type="button"
              :disabled="actingId !== null"
              @click="confirm(item)"
            >
              确认承诺
            </button>
          </div>
          <button
            v-else-if="['pending', 'waiting_customer', 'overdue'].includes(item.status)"
            class="btn-primary"
            type="button"
            :disabled="actingId !== null"
            @click="fulfill(item)"
          >
            标记已完成
          </button>
          <span
            v-else
            class="terminal-state"
          >该承诺已进入终态</span>
        </footer>
      </article>
    </section>
  </div>
</template>

<style scoped>
.commitment-shell { overflow: auto; }
.commitment-head { justify-content: space-between; padding-top: var(--space3); }
.head-actions { display: flex; align-items: center; gap: var(--space3); }
.head-actions label { display: flex; align-items: center; gap: var(--space1); color: var(--text-secondary); font-size: 12px; }
.metric-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: var(--space3); }
.metric-grid article, .ledger { border: 1px solid var(--border); border-radius: 12px; background: var(--surface); }
.metric-grid article { padding: var(--space4); }
.metric-grid span { color: var(--text-secondary); font-size: 12px; font-weight: 700; }
.metric-grid strong { display: block; margin-top: var(--space2); font-size: 30px; }
.metric-grid p { margin-top: var(--space1); color: var(--text-secondary); font-size: 11px; }
.ledger { padding: var(--space4); }
.ledger > header { display: flex; justify-content: space-between; align-items: end; padding-bottom: var(--space3); }
.ledger > header > span { color: var(--text-secondary); }
.commitment-card { margin-top: var(--space3); border: 1px solid var(--border); border-left: 4px solid var(--action); border-radius: var(--radius); padding: var(--space4); }
.commitment-card.overdue { border-left-color: var(--danger); }
.commitment-card.uncertain { border-right: 2px dashed var(--warning); }
.commitment-card > header { display: flex; justify-content: space-between; gap: var(--space3); }
.commitment-card small, dd { overflow-wrap: anywhere; color: var(--text-secondary); }
.status-line { display: flex; flex-wrap: wrap; gap: var(--space1); }
.type-pill, .state-pill, .uncertain-pill { border-radius: 999px; padding: 3px 8px; font-size: 10px; font-weight: 800; }
.type-pill { background: var(--fact-soft); color: var(--fact); }
.state-pill { background: #edf0ef; color: var(--text-secondary); }
.uncertain-pill { background: var(--warning-soft); color: var(--warning); }
h3 { margin-top: var(--space3); font-size: 18px; }
blockquote { margin: var(--space3) 0 0; border-left: 3px solid var(--border); padding-left: var(--space3); color: var(--text-secondary); font-style: italic; }
dl { display: grid; grid-template-columns: repeat(2, 1fr); gap: var(--space2) var(--space4); margin-top: var(--space4); }
dl div { display: grid; grid-template-columns: 90px 1fr; gap: var(--space2); }
dt { color: var(--text-secondary); font-size: 11px; font-weight: 700; }
dd { margin: 0; font-size: 12px; }
.commitment-card footer { display: flex; justify-content: flex-end; margin-top: var(--space4); border-top: 1px solid var(--border); padding-top: var(--space3); }
.confirm-panel { width: 100%; display: flex; justify-content: flex-end; align-items: end; gap: var(--space3); }
.confirm-panel label { display: grid; gap: var(--space1); color: var(--text-secondary); font-size: 11px; }
.confirm-panel input { border: 1px solid var(--border); border-radius: var(--radius-sm); padding: var(--space2); }
.terminal-state { color: var(--text-secondary); font-size: 12px; }
.empty { display: grid; place-items: center; min-height: 180px; color: var(--text-secondary); }
@media (max-width: 900px) { .metric-grid, dl { grid-template-columns: 1fr; } .commitment-head, .confirm-panel { align-items: stretch; flex-direction: column; } }
</style>
