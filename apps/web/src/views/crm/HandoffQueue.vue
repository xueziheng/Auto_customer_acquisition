<script setup lang="ts">
/* global HTMLButtonElement, HTMLOListElement, Response */
import { computed, inject, nextTick, onMounted, ref } from "vue";

import { apiClient, createApiClient } from "../../api/client";
import type { components } from "../../api/api";
import HandoffPacketView from "./HandoffPacketView.vue";

type ApiClient = ReturnType<typeof createApiClient>;
type HandoffPacketViewDto = components["schemas"]["HandoffPacketView"];
type HandoffQueueItemView = components["schemas"]["HandoffQueueItemView"];
type OpportunityView = components["schemas"]["OpportunityView"];
type QueueUiState = "ready" | "loading" | "empty" | "forbidden" | "unavailable";

const client = inject<ApiClient>("tradeos-api-client", apiClient);
const queue = ref<HandoffQueueItemView[]>([]);
const removedHandoffIds = ref<ReadonlySet<string>>(new Set());
const selectedHandoffId = ref<string | null>(null);
const packet = ref<HandoffPacketViewDto | null>(null);
const opportunity = ref<OpportunityView | null>(null);
const queueState = ref<QueueUiState>("loading");
const packetLoading = ref(false);
const writePending = ref(false);
const stale = ref(false);
const safeErrorMessage = ref<string | null>(null);
const retryAfter = ref<string | null>(null);
const announcement = ref("正在加载接管队列…");
const announcementIsError = ref(false);
const queueList = ref<HTMLOListElement>();

let queueGeneration = 0;
let packetGeneration = 0;

const visibleQueue = computed(() => queue.value.filter((item) => !removedHandoffIds.value.has(item.handoff_id)));
const isReady = computed(() => queueState.value === "ready");
const canSelect = computed(() => isReady.value && !writePending.value);
const hasSelectedCombination = computed(() =>
  Boolean(
    selectedHandoffId.value
      && packet.value
      && opportunity.value
      && packet.value.handoff_id === selectedHandoffId.value
      && packet.value.opportunity_id === opportunity.value.opportunity_id,
  ),
);
const canAccept = computed(() => canSelect.value && !packetLoading.value && hasSelectedCombination.value);

function safeError(status: number): string {
  if (status === 400) return "请求参数无效";
  if (status === 403) return "没有权限";
  if (status === 409) return "已被接受";
  if (status === 503) return "服务暂时不可用，请稍后重试";
  return "请求未完成，请刷新后重试";
}

function manualRetryNotice(response: Response): string | null {
  const value = response.headers.get("retry-after");
  if (!value) return null;
  return /^\d+$/.test(value) ? `${value} 秒后可手工重试` : "请在服务建议时间后手工重试";
}

function announce(message: string, isError = false): void {
  announcement.value = message;
  announcementIsError.value = isError;
}

function clearCombination(): void {
  packet.value = null;
  opportunity.value = null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isString(value: unknown): value is string {
  return typeof value === "string";
}

function isNullableString(value: unknown): boolean {
  return value === undefined || value === null || isString(value);
}

function isOptionalStringArray(value: unknown): boolean {
  return value === undefined || Array.isArray(value) && value.every(isString);
}

function isOptionalNullableNumber(value: unknown): boolean {
  return value === undefined || value === null || typeof value === "number";
}

function isOptionalNullableBoolean(value: unknown): boolean {
  return value === undefined || value === null || typeof value === "boolean";
}

function isOptionalMoney(value: unknown): boolean {
  return value === undefined || value === null || isRecord(value) && isString(value.amount) && isString(value.currency);
}

function isProvenanceSummary(value: unknown): boolean {
  return isRecord(value)
    && isNullableString(value.confirmed_at)
    && value.confirmed_at !== undefined
    && isNullableString(value.confirmed_by)
    && value.confirmed_by !== undefined
    && isString(value.extracted_at)
    && isString(value.extracted_by)
    && isString(value.field_name)
    && isNullableString(value.page_hash)
    && value.page_hash !== undefined
    && isString(value.source_id)
    && isString(value.source_type)
    && isNullableString(value.source_url)
    && value.source_url !== undefined;
}

function isOptionalProvenanceArray(value: unknown): boolean {
  return value === undefined || Array.isArray(value) && value.every(isProvenanceSummary);
}

function isStringRecord(value: unknown): boolean {
  return isRecord(value) && Object.values(value).every(isString);
}

function isOptionalScoreExplanation(value: unknown): boolean {
  if (value === undefined || value === null) return true;
  if (!isRecord(value) || !isRecord(value.sort_key)) return false;
  return isOptionalStringArray(value.failed_gates)
    && value.failed_gates !== undefined
    && isStringRecord(value.gate_reasons)
    && isOptionalStringArray(value.passed_gates)
    && value.passed_gates !== undefined
    && isString(value.rank_bucket)
    && isString(value.scored_at)
    && isString(value.scorer_version)
    && typeof value.sort_key.evidence_rank === "number"
    && typeof value.sort_key.supply_rank === "number"
    && typeof value.sort_key.value_band === "number";
}

function isQueueItem(value: HandoffQueueItemView | undefined): value is HandoffQueueItemView {
  return Boolean(
    value
      && typeof value.handoff_id === "string"
      && value.handoff_id.length > 0
      && typeof value.opportunity_id === "string"
      && value.opportunity_id.length > 0
      && typeof value.account_name === "string"
      && typeof value.customer_verbatim === "string"
      && typeof value.wait_seconds === "number",
  );
}

function isPacket(value: unknown): value is HandoffPacketViewDto {
  return isRecord(value)
    && isString(value.account_name)
    && isOptionalStringArray(value.already_sent)
    && isNullableString(value.assigned_to_name)
    && isOptionalStringArray(value.commitments_made)
    && isNullableString(value.conversation_summary)
    && isString(value.country)
    && isString(value.customer_verbatim)
    && isOptionalStringArray(value.evidence_links)
    && isString(value.handoff_id)
    && isNullableString(value.how_we_found_them)
    && isOptionalStringArray(value.missing_information)
    && isString(value.opportunity_id)
    && isString(value.requested_at)
    && isString(value.state)
    && isNullableString(value.suggested_next_step)
    && isString(value.trigger)
    && isNullableString(value.validated_need_summary)
    && isOptionalNullableNumber(value.wait_seconds)
    && isString(value.why_valuable);
}

function isOpportunity(value: unknown): value is OpportunityView {
  return isRecord(value)
    && isString(value.account_id)
    && isString(value.account_name)
    && isOptionalNullableBoolean(value.can_source)
    && isString(value.country)
    && isString(value.created_at)
    && isNullableString(value.current_supply_problem)
    && isNullableString(value.destination)
    && isNullableString(value.died_at_state)
    && isOptionalMoney(value.estimated_cost)
    && isOptionalMoney(value.estimated_profit)
    && typeof value.has_pending_handoff === "boolean"
    && isNullableString(value.loss_reason)
    && isString(value.need_id)
    && isNullableString(value.next_action)
    && isNullableString(value.next_action_due)
    && isString(value.opportunity_id)
    && isNullableString(value.owner)
    && isNullableString(value.owner_name)
    && isString(value.product_category)
    && isOptionalProvenanceArray(value.provenance)
    && isOptionalNullableNumber(value.quantity)
    && isNullableString(value.required_by)
    && isOptionalScoreExplanation(value.score)
    && isNullableString(value.spec_summary)
    && isString(value.state)
    && isOptionalMoney(value.target_price);
}

function removeCapturedHandoff(handoffId: string): void {
  removedHandoffIds.value = new Set([...removedHandoffIds.value, handoffId]);
  if (selectedHandoffId.value === handoffId) selectedHandoffId.value = null;
  clearCombination();
}

function firstItemAtOrAfter(index: number): HandoffQueueItemView | undefined {
  return visibleQueue.value[Math.min(index, Math.max(visibleQueue.value.length - 1, 0))];
}

async function focusQueueItem(handoffId: string | null): Promise<void> {
  if (!handoffId) return;
  await nextTick();
  const button = [...(queueList.value?.querySelectorAll<HTMLButtonElement>("button[data-handoff-id]") ?? [])].find(
    (candidate) => candidate.dataset.handoffId === handoffId,
  );
  button?.focus();
}

function leaveReadFailure(
  status: number,
  response: Response | null,
  options: { clearProtected: boolean; queueFailure: boolean; refreshIncomplete?: boolean },
): void {
  safeErrorMessage.value = options.refreshIncomplete
    ? "刷新未完成，请手工重试"
    : safeError(status);
  retryAfter.value = response ? manualRetryNotice(response) : null;
  packetLoading.value = false;
  if (status === 403) {
    queueState.value = "forbidden";
    stale.value = false;
    if (options.queueFailure) queue.value = [];
    selectedHandoffId.value = null;
    clearCombination();
  } else {
    queueState.value = "unavailable";
    stale.value = queue.value.length > 0 || packet.value !== null;
    if (options.clearProtected) clearCombination();
  }
  announce(safeErrorMessage.value, true);
}

async function loadPacket(handoffId: string, allowDuringWrite = false): Promise<boolean> {
  if (!isReady.value || writePending.value && !allowDuringWrite) return false;
  const queueItem = visibleQueue.value.find((item) => item.handoff_id === handoffId);
  if (!queueItem) return false;
  const requestGeneration = ++packetGeneration;
  selectedHandoffId.value = handoffId;
  packetLoading.value = true;
  safeErrorMessage.value = null;
  retryAfter.value = null;
  announce("正在加载完整接管包…");
  try {
    const packetResult = await client.GET("/crm/handoffs/{handoff_id}", {
      params: { path: { handoff_id: handoffId } },
    });
    if (
      requestGeneration !== packetGeneration
      || !isReady.value
      || selectedHandoffId.value !== handoffId
    ) return false;
    if (
      packetResult.response.status !== 200
      || !isPacket(packetResult.data)
      || packetResult.data.handoff_id !== handoffId
      || packetResult.data.opportunity_id !== queueItem.opportunity_id
    ) {
      leaveReadFailure(packetResult.response.status, packetResult.response, {
        clearProtected: packetResult.response.status !== 503,
        queueFailure: false,
      });
      return false;
    }
    const packetData = packetResult.data;
    const opportunityResult = await client.GET("/crm/opportunities/{opportunity_id}", {
      params: { path: { opportunity_id: packetData.opportunity_id } },
    });
    if (
      requestGeneration !== packetGeneration
      || !isReady.value
      || selectedHandoffId.value !== handoffId
    ) return false;
    if (
      opportunityResult.response.status !== 200
      || !isOpportunity(opportunityResult.data)
      || opportunityResult.data.opportunity_id !== packetData.opportunity_id
    ) {
      leaveReadFailure(opportunityResult.response.status, opportunityResult.response, {
        clearProtected: opportunityResult.response.status !== 503,
        queueFailure: false,
      });
      return false;
    }
    packet.value = packetData;
    opportunity.value = opportunityResult.data;
    stale.value = false;
    packetLoading.value = false;
    announce("完整接管包已按后端响应加载。");
    return true;
  } catch {
    if (requestGeneration === packetGeneration && isReady.value && selectedHandoffId.value === handoffId) {
      leaveReadFailure(0, null, { clearProtected: true, queueFailure: false });
    }
    return false;
  }
}

async function loadQueue(options: {
  allowDuringWrite?: boolean;
  focusIndex?: number;
  successMessage?: string;
  successIsError?: boolean;
  silentLoading?: boolean;
} = {}): Promise<boolean> {
  const requestGeneration = ++queueGeneration;
  packetGeneration += 1;
  packetLoading.value = false;
  queueState.value = "loading";
  safeErrorMessage.value = null;
  retryAfter.value = null;
  if (!options.silentLoading) announce("正在加载接管队列…");
  try {
    const result = await client.GET("/crm/handoffs", {
      params: { query: { limit: 50 } },
    });
    if (requestGeneration !== queueGeneration) return false;
    if (
      result.response.status !== 200
      || !Array.isArray(result.data)
      || !result.data.every(isQueueItem)
    ) {
      leaveReadFailure(result.response.status, result.response, {
        clearProtected: false,
        queueFailure: true,
      });
      return false;
    }
    queue.value = result.data;
    stale.value = false;
    const firstAvailable = options.focusIndex === undefined
      ? visibleQueue.value.find((item) => item.handoff_id === selectedHandoffId.value) ?? visibleQueue.value[0]
      : firstItemAtOrAfter(options.focusIndex);
    if (!firstAvailable) {
      selectedHandoffId.value = null;
      clearCombination();
      queueState.value = "empty";
      announce(options.successMessage ?? "当前没有待接管事项", options.successIsError ?? false);
      return true;
    }
    queueState.value = "ready";
    selectedHandoffId.value = firstAvailable.handoff_id;
    const detailLoaded = await loadPacket(firstAvailable.handoff_id, options.allowDuringWrite);
    if (requestGeneration !== queueGeneration) return false;
    if (detailLoaded) {
      announce(options.successMessage ?? "已按后端等待顺序刷新接管队列。", options.successIsError ?? false);
    }
    return detailLoaded;
  } catch {
    if (requestGeneration === queueGeneration) {
      leaveReadFailure(0, null, { clearProtected: false, queueFailure: true });
    }
    return false;
  }
}

async function selectHandoff(handoffId: string): Promise<void> {
  if (!canSelect.value || handoffId === selectedHandoffId.value && hasSelectedCombination.value) return;
  await loadPacket(handoffId);
}

async function refreshQueue(): Promise<void> {
  if (!canSelect.value) return;
  await loadQueue();
}

async function manuallyRetry(): Promise<void> {
  if (writePending.value || isReady.value) return;
  await loadQueue({ successMessage: "已通过手工重试按后端等待顺序恢复。" });
}

async function acceptHandoff(): Promise<void> {
  if (!canAccept.value || !selectedHandoffId.value) return;
  const handoffId = selectedHandoffId.value;
  const capturedIndex = visibleQueue.value.findIndex((item) => item.handoff_id === handoffId);
  if (capturedIndex < 0) return;
  writePending.value = true;
  packetGeneration += 1;
  safeErrorMessage.value = null;
  retryAfter.value = null;
  announce("正在接受接管…");
  let focusAfterWrite: string | null = null;
  try {
    const result = await client.POST("/crm/handoffs/{handoff_id}/accept", {
      params: { path: { handoff_id: handoffId } },
    });
    if (result.response.status === 204 || result.response.status === 409) {
      const concurrent = result.response.status === 409;
      removeCapturedHandoff(handoffId);
      const outcome = concurrent ? "已被接受" : "已接受接管";
      announce(`${outcome}；正在按后端响应刷新队列。`, concurrent);
      const refreshed = await loadQueue({
        allowDuringWrite: true,
        focusIndex: capturedIndex,
        silentLoading: true,
        successIsError: concurrent,
        successMessage: `${outcome}；已按后端等待顺序刷新队列。`,
      });
      if (refreshed && isReady.value) focusAfterWrite = selectedHandoffId.value;
      if (!refreshed && queueState.value !== "forbidden") {
        const rootError = safeErrorMessage.value ?? "请求未完成，请刷新后重试";
        safeErrorMessage.value = `${rootError}；刷新未完成，请手工重试`;
        stale.value = true;
        queueState.value = "unavailable";
        announce(`${outcome}；${rootError}；刷新未完成，请手工重试`, true);
      }
      return;
    }
    safeErrorMessage.value = safeError(result.response.status);
    retryAfter.value = manualRetryNotice(result.response);
    if (result.response.status === 403) {
      queueState.value = "forbidden";
      stale.value = false;
      selectedHandoffId.value = null;
      clearCombination();
    } else if (result.response.status !== 400) {
      queueState.value = "unavailable";
      stale.value = true;
    }
    announce(safeErrorMessage.value, result.response.status !== 400);
  } catch {
    safeErrorMessage.value = "请求未完成，请刷新后重试";
    queueState.value = "unavailable";
    stale.value = true;
    announce(safeErrorMessage.value, true);
  } finally {
    writePending.value = false;
    if (focusAfterWrite && isReady.value && selectedHandoffId.value === focusAfterWrite) {
      await focusQueueItem(focusAfterWrite);
    }
  }
}

onMounted(() => {
  void loadQueue();
});
</script>

<template>
  <main
    class="handoff-shell"
    aria-label="TradeOS 人工接管证据账本"
  >
    <header class="topbar">
      <div class="brand">
        <span
          class="brand-mark"
          aria-hidden="true"
        >TO</span><span>TradeOS</span>
      </div>
      <nav
        class="topnav"
        aria-label="CRM 页面"
      >
        <RouterLink to="/crm/opportunities">
          机会看板
        </RouterLink>
        <span aria-current="page">人工接管队列</span>
        <button
          type="button"
          disabled
        >
          失败原因分析
        </button>
      </nav>
      <div class="identity">
        <strong>演示工作区</strong> · 当前员工
      </div>
    </header>

    <div class="handoff-workspace">
      <section
        class="pane queue-pane"
        aria-labelledby="handoff-queue-title"
        :aria-busy="queueState === 'loading'"
      >
        <header class="pane-header">
          <p class="eyebrow">
            EVIDENCE LEDGER
          </p>
          <div class="heading-row">
            <div>
              <h1 id="handoff-queue-title">
                人工接管队列
              </h1>
              <p class="subcopy">
                最久等待事项优先由真人接管
              </p>
            </div>
            <button
              type="button"
              aria-label="刷新队列"
              :disabled="!canSelect"
              @click="refreshQueue"
            >
              ↻
            </button>
          </div>
          <div class="fairness-rule">
            <strong>等待最久优先</strong>
            <span>requested_at 升序 / wait_seconds 降序；不按分数排序</span>
          </div>
          <p
            v-if="stale"
            class="stale-note"
          >
            数据可能已过期
          </p>
          <div
            v-if="safeErrorMessage"
            class="safe-error"
            role="alert"
          >
            <span aria-hidden="true">!</span><span>{{ safeErrorMessage }}</span>
            <small v-if="retryAfter">{{ retryAfter }}</small>
            <button
              type="button"
              :disabled="writePending || isReady"
              @click="manuallyRetry"
            >
              手工重试
            </button>
          </div>
        </header>

        <div
          v-if="queueState === 'loading' && visibleQueue.length === 0"
          class="loading-state"
          role="status"
        >
          正在加载接管队列…
        </div>
        <p
          v-else-if="queueState === 'empty'"
          class="empty-state"
        >
          当前没有待接管事项
        </p>
        <ol
          v-else
          ref="queueList"
          class="handoff-list"
          aria-label="最久等待接管队列"
        >
          <li
            v-for="(item, index) in visibleQueue"
            :key="item.handoff_id"
          >
            <button
              class="handoff-card"
              type="button"
              :data-handoff-id="item.handoff_id"
              :aria-current="item.handoff_id === selectedHandoffId ? 'true' : undefined"
              :disabled="!canSelect"
              @click="selectHandoff(item.handoff_id)"
            >
              <span class="card-top"><span class="demo-badge">演示数据</span><span class="trigger-tag">{{ item.trigger }}</span></span>
              <span class="wait-time"><small>已等待</small>{{ item.wait_seconds }} 秒</span>
              <span class="queue-order-row"><span class="queue-order">等待顺序第 {{ index + 1 }} 项</span><span class="requested-at">{{ item.requested_at }}</span></span>
              <strong class="account-name">{{ item.account_name }}</strong>
              <span class="card-meta">{{ item.country }} · 指派员工 {{ item.assigned_to ?? "未分配" }} · 状态 {{ item.state }}</span>
              <span class="handoff-inference">
                <strong>推断 / 价值与建议</strong>
                <span><b>价值说明：</b>{{ item.why_valuable }}</span>
                <span><b>建议下一步：</b>{{ item.suggested_next_step ?? "暂不可用" }}</span>
              </span>
              <span class="card-counts">
                <span>缺失信息 {{ item.missing_information?.length ?? 0 }} 项</span>
                <span>证据入口 {{ item.evidence_links?.length ?? 0 }} 项</span>
              </span>
              <span class="backend-rule">升级由后端规则决定</span>
            </button>
          </li>
        </ol>
      </section>

      <section
        class="pane packet-pane"
        aria-label="接管包详情"
        :aria-busy="packetLoading"
      >
        <p
          v-if="packetLoading && !hasSelectedCombination"
          class="packet-loading"
          role="status"
        >
          正在加载完整接管包…
        </p>
        <HandoffPacketView
          v-if="packet && opportunity && !packetLoading"
          :packet="packet"
          :opportunity="opportunity"
        />
        <p
          v-else-if="queueState === 'forbidden'"
          class="packet-placeholder"
        >
          没有权限；受保护的接管包已清空。
        </p>
        <p
          v-else-if="queueState === 'empty'"
          class="packet-placeholder"
        >
          当前没有待接管事项；受保护的接管包已清空。
        </p>
        <p
          v-else-if="!packetLoading"
          class="packet-placeholder"
        >
          请选择待接管事项以读取完整接管包。
        </p>
      </section>

      <aside
        class="pane status-pane"
        aria-labelledby="operation-title"
      >
        <header class="pane-header">
          <p class="eyebrow">
            HUMAN ACTION
          </p>
          <h2 id="operation-title">
            操作状态
          </h2>
          <p class="subcopy">
            API / 域服务判权为最终裁决
          </p>
        </header>
        <div class="status-content">
          <section class="accept-panel">
            <h3>接受当前接管</h3>
            <p>提交后以服务端刷新结果为准。</p>
            <button
              class="accept-button"
              type="button"
              :disabled="!canAccept"
              @click="acceptHandoff"
            >
              {{ writePending ? "正在接受…" : "接受接管" }}
            </button>
          </section>
          <div
            class="live-region"
            :role="announcementIsError ? 'alert' : 'status'"
            :aria-live="announcementIsError ? 'assertive' : 'polite'"
          >
            {{ announcement }}
          </div>
          <section class="state-note">
            <h3>安全状态</h3>
            <p><strong>403 · 没有权限</strong> 清空受保护详情并禁用操作。</p>
            <p><strong>503 · 服务暂时不可用</strong> 保留最近成功内容并标记可能过期，只允许手工恢复。</p>
            <p><strong>Empty</strong> 无事项时不能接受接管。</p>
          </section>
        </div>
      </aside>
    </div>
  </main>
</template>

<style scoped>
:global(html),
:global(body),
:global(#app) {
  width: 100%;
  height: 100%;
  overflow: hidden;
}

:global(body) {
  margin: 0;
  background: #f5f7f7;
  color: #172323;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
  font-size: 14px;
  line-height: 1.55;
}

.handoff-shell {
  display: grid;
  grid-template-rows: 68px minmax(0, 1fr);
  height: 100vh;
  overflow: hidden;
  background: #f5f7f7;
}

.topbar {
  display: flex;
  align-items: center;
  gap: 22px;
  padding: 0 22px;
  color: #edf7f5;
  border-bottom: 1px solid #0f302e;
  background: #173b39;
}

.brand,
.topnav,
.heading-row,
.card-top,
.card-counts {
  display: flex;
  align-items: center;
}

.queue-order-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 7px;
}

.brand {
  gap: 10px;
  min-width: 172px;
  font-size: 17px;
  font-weight: 780;
}

.brand-mark {
  display: grid;
  width: 34px;
  height: 34px;
  place-items: center;
  border: 1px solid #75a9a2;
  border-radius: 9px;
  background: #23524f;
  font-size: 13px;
}

.topnav {
  gap: 5px;
}

.topnav a,
.topnav span,
.topnav button {
  display: inline-flex;
  min-height: 44px;
  align-items: center;
  padding: 0 12px;
  color: #d9ebe8;
  border: 1px solid transparent;
  border-radius: 8px;
  background: transparent;
  font: inherit;
  font-weight: 680;
  text-decoration: none;
}

.topnav [aria-current="page"] {
  color: #fff;
  border-color: #4d807a;
  background: #2a5d59;
}

.identity {
  margin-left: auto;
  color: #b8d4cf;
  font-size: 12px;
}

.identity strong {
  color: #fff;
}

.handoff-workspace {
  display: grid;
  grid-template-columns: 330px minmax(500px, 1fr) 286px;
  gap: 14px;
  min-height: 0;
  padding: 14px;
  overflow: hidden;
}

.pane {
  min-width: 0;
  min-height: 0;
  overflow: hidden;
  border: 1px solid #cbd7d5;
  border-radius: 12px;
  background: #fff;
  box-shadow: 0 1px 2px rgba(20, 35, 35, 0.08);
}

.queue-pane,
.status-pane {
  display: flex;
  flex-direction: column;
}

.packet-pane {
  overflow: auto;
}

.pane-header {
  padding: 17px 18px 14px;
  border-bottom: 1px solid #cbd7d5;
}

.eyebrow {
  margin: 0 0 4px;
  color: #0f766e;
  font-size: 11px;
  font-weight: 780;
  letter-spacing: 0.1em;
}

h1,
h2,
h3,
p {
  margin-top: 0;
}

h1 {
  margin-bottom: 4px;
  font-size: 22px;
  line-height: 1.25;
}

h2 {
  margin-bottom: 5px;
  font-size: 17px;
  line-height: 1.35;
}

h3 {
  margin-bottom: 7px;
  font-size: 14px;
}

.subcopy {
  margin-bottom: 0;
  color: #526363;
  font-size: 12px;
}

.heading-row {
  justify-content: space-between;
  gap: 9px;
}

button {
  min-height: 44px;
  padding: 0 12px;
  color: #172323;
  border: 1px solid #9cb0ad;
  border-radius: 8px;
  background: #fff;
  font: inherit;
  font-weight: 680;
  cursor: pointer;
}

button:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}

button:focus-visible,
a:focus-visible {
  outline: 3px solid #7c3aed;
  outline-offset: 2px;
}

.fairness-rule {
  display: grid;
  gap: 2px;
  margin-top: 11px;
  padding: 10px;
  color: #174f4a;
  border: 1px solid #7dbab2;
  border-radius: 8px;
  background: #e7f5f2;
  font-size: 12px;
}

.stale-note {
  margin: 10px 0 0;
  padding: 7px 9px;
  color: #8a4b00;
  border: 1px solid #deb86c;
  border-radius: 7px;
  background: #fff4e8;
  font-size: 12px;
  font-weight: 680;
}

.safe-error {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 7px;
  margin-top: 10px;
  padding: 9px;
  color: #8b2117;
  border: 1px solid #e3a29b;
  border-radius: 8px;
  background: #fef0ec;
  font-size: 12px;
}

.safe-error small {
  grid-column: 2;
}

.safe-error button {
  grid-column: 1 / -1;
  width: 100%;
}

.handoff-list {
  flex: 1;
  min-height: 0;
  margin: 0;
  padding: 10px;
  overflow: auto;
  list-style: none;
}

.handoff-list li + li {
  margin-top: 9px;
}

.handoff-card {
  display: grid;
  width: 100%;
  min-height: 0;
  gap: 8px;
  padding: 13px;
  border-color: #cbd7d5;
  text-align: left;
  font-weight: 400;
  box-shadow: 0 1px 2px rgba(20, 35, 35, 0.08);
}

.handoff-card[aria-current="true"] {
  border-color: #0f766e;
  background: #f0faf8;
  box-shadow: inset 3px 0 0 #0f766e, 0 1px 2px rgba(20, 35, 35, 0.08);
}

.card-top,
.card-counts {
  justify-content: space-between;
  gap: 8px;
}

.demo-badge,
.trigger-tag,
.queue-order {
  display: inline-flex;
  width: fit-content;
  align-items: center;
  min-height: 23px;
  padding: 2px 7px;
  border-radius: 6px;
  font-size: 11px;
  font-weight: 750;
  line-height: 1.2;
}

.demo-badge {
  color: #445554;
  border: 1px solid #cbd7d5;
  background: #e9efee;
}

.trigger-tag {
  color: #405351;
  border: 1px solid #cbd7d5;
  background: #f0f4f3;
}

.queue-order {
  color: #5a4200;
  border: 1px solid #d9bd69;
  background: #fff8e5;
}

.wait-time {
  display: grid;
  color: #9a4f09;
  font-size: 18px;
  font-weight: 800;
  font-variant-numeric: tabular-nums;
}

.wait-time small,
.card-meta,
.requested-at,
.backend-rule,
.card-counts {
  color: #526363;
  font-size: 11px;
  font-weight: 500;
}

.account-name {
  font-size: 15px;
}

.handoff-inference {
  display: grid;
  gap: 5px;
  padding: 9px;
  color: #6e4c00;
  border: 1px dashed #b98722;
  border-radius: 8px;
  background: #fff5d6;
  font-size: 12px;
}

.handoff-inference strong {
  color: #8a5c00;
}

.card-counts {
  align-items: start;
  justify-content: start;
  gap: 9px;
  flex-wrap: wrap;
}

.backend-rule {
  font-style: italic;
}

.loading-state,
.empty-state,
.packet-placeholder,
.packet-loading {
  margin: 18px;
  padding: 16px;
  color: #526363;
  border: 1px dashed #9cb0ad;
  border-radius: 9px;
  background: #f9fbfa;
}

.packet-loading {
  margin-bottom: 0;
}

.status-content {
  display: grid;
  gap: 14px;
  padding: 15px;
  overflow: auto;
}

.accept-panel,
.state-note {
  padding: 12px;
  border: 1px solid #cbd7d5;
  border-radius: 9px;
  background: #f9fbfa;
}

.accept-panel p,
.state-note p {
  color: #526363;
  font-size: 12px;
}

.accept-panel p:last-child,
.state-note p:last-child {
  margin-bottom: 0;
}

.accept-button {
  width: 100%;
  color: #fff;
  border-color: #155eef;
  background: #155eef;
}

.live-region {
  min-height: 52px;
  padding: 11px;
  color: #174f4a;
  border: 1px solid #7dbab2;
  border-radius: 8px;
  background: #e7f5f2;
  font-size: 12px;
}

.live-region[role="alert"] {
  color: #8b2117;
  border-color: #e3a29b;
  background: #fef0ec;
}

@media (max-width: 1240px) {
  .handoff-workspace {
    grid-template-columns: 290px minmax(450px, 1fr) 250px;
    gap: 10px;
    padding: 10px;
  }

  .topbar {
    gap: 12px;
    padding: 0 14px;
  }

  .brand {
    min-width: auto;
  }

  .identity {
    display: none;
  }
}

@media (max-width: 760px) {
  :global(body) {
    min-width: 0;
  }

  .handoff-shell {
    grid-template-rows: auto auto;
    height: calc(100vh - 56px);
    overflow: auto;
  }

  .topbar {
    flex-wrap: wrap;
    gap: 8px;
    padding: 10px;
  }

  .topnav {
    width: 100%;
    overflow-x: auto;
  }

  .handoff-workspace {
    grid-template-columns: minmax(0, 1fr);
    overflow: visible;
  }

  .pane,
  .handoff-list,
  .status-content {
    overflow: visible;
  }

  .summary-grid,
  .fact-grid,
  .context-grid,
  .packet-lists {
    grid-template-columns: minmax(0, 1fr);
  }
}

@media (prefers-reduced-motion: reduce) {
  *,
  *::before,
  *::after {
    scroll-behavior: auto !important;
    transition-duration: 0.01ms !important;
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
  }
}
</style>
