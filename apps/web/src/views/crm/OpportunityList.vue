<script setup lang="ts">
/* global Event, HTMLSelectElement, Response */
import { computed, inject, onMounted, ref } from "vue";

import { apiClient, createApiClient } from "../../api/client";
import type { components } from "../../api/api";
import OpportunityDetail from "./OpportunityDetail.vue";

type ApiClient = ReturnType<typeof createApiClient>;
type OpportunityMarkLostBody = components["schemas"]["OpportunityMarkLostBody"];
type OpportunityState = components["schemas"]["OpportunityState"];
type OpportunityView = components["schemas"]["OpportunityView"];
type RefreshChannels = { detail: boolean; list: boolean };

const client = inject<ApiClient>("tradeos-api-client", apiClient);
const opportunities = ref<OpportunityView[]>([]);
const selectedId = ref<string | null>(null);
const selectedOpportunity = ref<OpportunityView | null>(null);
const listLoading = ref(true);
const detailLoading = ref(false);
const listError = ref<string | null>(null);
const detailError = ref<string | null>(null);
const actionError = ref<string | null>(null);
const listRetryAfter = ref<string | null>(null);
const detailRetryAfter = ref<string | null>(null);
const actionRetryAfter = ref<string | null>(null);
const stale = ref(false);
const writePending = ref(false);
const actionStatus = ref("等待人工操作；前端不会预先修改状态。");
const outstandingPostWriteReads = ref<RefreshChannels | null>(null);
const writeRefreshWarning = computed(() =>
  outstandingPostWriteReads.value ? "写入成功，但刷新未完成，请手工重试" : null,
);
const stateFilter = ref<OpportunityState | "">("");
const pageLimit = ref<25 | 50 | 100>(50);
let detailRequestVersion = 0;
let listRequestVersion = 0;

const stateLabels: Record<OpportunityState, string> = {
  qualified: "已合格",
  assigned: "已分配",
  contacted: "已联系",
  sourcing: "寻源中",
  quoted: "已报价",
  negotiating: "洽谈中",
  won: "已成交",
  lost: "已流失",
};

function requestHeaders(): Record<string, string> {
  const headers: Record<string, string> = {};
  const tenantId = import.meta.env.VITE_TENANT_ID;
  const employeeId = import.meta.env.VITE_EMPLOYEE_ID;
  if (tenantId) headers["X-Tenant-Id"] = tenantId;
  if (employeeId) headers["X-Employee-Id"] = employeeId;
  return headers;
}

function safeError(status: number): string {
  if (status === 400) return "请求参数无效";
  if (status === 403) return "没有权限";
  if (status === 409) return "当前状态不允许此操作";
  if (status === 503) return "服务暂时不可用";
  return "请求未完成，请刷新后重试";
}

function manualRetryNotice(response: Response): string | null {
  const value = response.headers.get("retry-after");
  if (!value) return null;
  return /^\d+$/.test(value) ? `${value} 秒后可手工重试` : "请在服务建议时间后手工重试";
}

function isOpportunityState(value: string): value is OpportunityState {
  return Object.hasOwn(stateLabels, value);
}

function statusLabel(value: string): string {
  return isOpportunityState(value) ? stateLabels[value] : value;
}

function rankBucketLabel(opportunity: OpportunityView): string {
  const bucket = opportunity.score?.rank_bucket;
  if (bucket === "high") return "排序桶：高";
  if (bucket === "mid") return "排序桶：中";
  if (bucket === "low") return "排序桶：低";
  return "排序桶：未知";
}

function changeLimit(event: Event): void {
  const value = (event.target as HTMLSelectElement).value;
  if (value === "25") pageLimit.value = 25;
  if (value === "50") pageLimit.value = 50;
  if (value === "100") pageLimit.value = 100;
}

async function loadDetail(opportunityId: string): Promise<boolean> {
  const requestVersion = ++detailRequestVersion;
  detailLoading.value = true;
  detailError.value = null;
  selectedOpportunity.value = null;
  actionError.value = null;
  detailRetryAfter.value = null;
  try {
    const result = await client.GET("/crm/opportunities/{opportunity_id}", {
      params: { path: { opportunity_id: opportunityId } },
      headers: requestHeaders(),
    });
    if (requestVersion !== detailRequestVersion || selectedId.value !== opportunityId) return false;
    if (result.response.status === 200 && result.data) {
      selectedOpportunity.value = result.data;
      return true;
    }
    detailError.value = safeError(result.response.status);
    detailRetryAfter.value = manualRetryNotice(result.response);
    if (result.response.status === 403) selectedOpportunity.value = null;
    return false;
  } catch {
    if (requestVersion === detailRequestVersion && selectedId.value === opportunityId) {
      detailError.value = "请求未完成，请刷新后重试";
    }
    return false;
  } finally {
    if (requestVersion === detailRequestVersion) detailLoading.value = false;
  }
}

async function selectOpportunity(opportunityId: string): Promise<void> {
  if (writePending.value || opportunityId === selectedId.value && selectedOpportunity.value) return;
  selectedId.value = opportunityId;
  await loadDetail(opportunityId);
}

async function loadList(loadSelectedDetail = true): Promise<boolean> {
  const requestVersion = ++listRequestVersion;
  listLoading.value = opportunities.value.length === 0;
  listError.value = null;
  listRetryAfter.value = null;
  try {
    const result = await client.GET("/crm/opportunities", {
      params: {
        query: {
          states: stateFilter.value ? [stateFilter.value] : undefined,
          limit: pageLimit.value,
        },
      },
      headers: requestHeaders(),
    });
    if (requestVersion !== listRequestVersion) return false;
    if (result.response.status === 200 && result.data) {
      opportunities.value = result.data;
      stale.value = false;
      const retained = selectedId.value && result.data.some((item) => item.opportunity_id === selectedId.value)
        ? selectedId.value
        : result.data[0]?.opportunity_id ?? null;
      selectedId.value = retained;
      if (!retained) {
        selectedOpportunity.value = null;
        detailError.value = null;
      } else if (loadSelectedDetail) {
        const detailLoaded = await loadDetail(retained);
        return detailLoaded && requestVersion === listRequestVersion;
      }
      return true;
    }

    listError.value = safeError(result.response.status);
    listRetryAfter.value = manualRetryNotice(result.response);
    if (result.response.status === 403) {
      opportunities.value = [];
      selectedId.value = null;
      selectedOpportunity.value = null;
      detailError.value = "没有权限";
      stale.value = false;
    } else if (opportunities.value.length > 0) {
      stale.value = true;
    }
  } catch {
    if (requestVersion !== listRequestVersion) return false;
    listError.value = "请求未完成，请刷新后重试";
    if (opportunities.value.length > 0) stale.value = true;
  } finally {
    if (requestVersion === listRequestVersion) listLoading.value = false;
  }
  return false;
}

function completeManualRecovery(attempted: Partial<RefreshChannels>): void {
  const outstanding = outstandingPostWriteReads.value;
  if (!outstanding) return;
  const remaining = {
    detail: attempted.detail === undefined ? outstanding.detail : !attempted.detail,
    list: attempted.list === undefined ? outstanding.list : !attempted.list,
  };
  if (remaining.detail || remaining.list) {
    outstandingPostWriteReads.value = remaining;
    return;
  }
  outstandingPostWriteReads.value = null;
  actionStatus.value = "已通过手工重试获取后端最新结果。";
}

async function manuallyRetryList(): Promise<void> {
  const list = await loadList(false);
  const attempted: Partial<RefreshChannels> = { list };
  if (list && selectedId.value) attempted.detail = await loadDetail(selectedId.value);
  completeManualRecovery(attempted);
}

async function manuallyRetryDetail(): Promise<void> {
  const detail = Boolean(selectedId.value && await loadDetail(selectedId.value));
  completeManualRecovery({ detail });
}

async function refreshAfterWrite(opportunityId: string): Promise<{ detail: boolean; list: boolean }> {
  const list = await loadList(false);
  let detail = false;
  if (selectedId.value === opportunityId || opportunities.value.some((item) => item.opportunity_id === opportunityId)) {
    selectedId.value = opportunityId;
    detail = await loadDetail(opportunityId);
  } else {
    selectedOpportunity.value = null;
  }
  return { detail, list };
}

async function transition(target: OpportunityState): Promise<void> {
  if (writePending.value || !selectedId.value) return;
  const opportunityId = selectedId.value;
  writePending.value = true;
  actionError.value = null;
  actionRetryAfter.value = null;
  outstandingPostWriteReads.value = null;
  actionStatus.value = "正在推进状态…";
  try {
    const result = await client.POST("/crm/opportunities/{opportunity_id}/transition", {
      params: { path: { opportunity_id: opportunityId } },
      body: { target },
      headers: requestHeaders(),
    });
    if (result.response.status === 200) {
      actionStatus.value = "状态推进请求成功；正在按后端响应刷新详情与列表。";
      const refreshed = await refreshAfterWrite(opportunityId);
      if (refreshed.list && refreshed.detail) {
        outstandingPostWriteReads.value = null;
        actionStatus.value = "状态已按后端最新结果刷新。";
      } else {
        outstandingPostWriteReads.value = { detail: !refreshed.detail, list: !refreshed.list };
        actionStatus.value = "写入成功，但刷新未完成，请手工重试。";
      }
      return;
    }
    actionError.value = safeError(result.response.status);
    actionRetryAfter.value = manualRetryNotice(result.response);
    if (result.response.status === 403) selectedOpportunity.value = null;
    actionStatus.value = "操作未完成。";
  } catch {
    actionError.value = "请求未完成，请刷新后重试";
    actionStatus.value = "操作未完成。";
  } finally {
    writePending.value = false;
  }
}

async function markLost(payload: OpportunityMarkLostBody): Promise<void> {
  if (writePending.value || !selectedId.value) return;
  const opportunityId = selectedId.value;
  writePending.value = true;
  actionError.value = null;
  actionRetryAfter.value = null;
  outstandingPostWriteReads.value = null;
  actionStatus.value = "正在标记流失…";
  try {
    const result = await client.POST("/crm/opportunities/{opportunity_id}/mark-lost", {
      params: { path: { opportunity_id: opportunityId } },
      body: payload,
      headers: requestHeaders(),
    });
    if (result.response.status === 200) {
      actionStatus.value = "流失结果已提交；正在按后端响应刷新详情与列表。";
      const refreshed = await refreshAfterWrite(opportunityId);
      if (refreshed.list && refreshed.detail) {
        outstandingPostWriteReads.value = null;
        actionStatus.value = "失败闭环已按后端最新结果刷新。";
      } else {
        outstandingPostWriteReads.value = { detail: !refreshed.detail, list: !refreshed.list };
        actionStatus.value = "写入成功，但刷新未完成，请手工重试。";
      }
      return;
    }
    actionError.value = safeError(result.response.status);
    actionRetryAfter.value = manualRetryNotice(result.response);
    if (result.response.status === 403) selectedOpportunity.value = null;
    actionStatus.value = "操作未完成。";
  } catch {
    actionError.value = "请求未完成，请刷新后重试";
    actionStatus.value = "操作未完成。";
  } finally {
    writePending.value = false;
  }
}

const listBusy = computed(() => listLoading.value || writePending.value);

onMounted(() => void loadList());
</script>

<template>
  <main
    class="board-shell"
    aria-label="TradeOS 机会证据账本"
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
        <button
          type="button"
          aria-current="page"
        >
          机会看板
        </button>
        <button
          type="button"
          disabled
        >
          人工接管队列
        </button>
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

    <div class="workspace">
      <section
        class="list-pane"
        aria-labelledby="board-title"
        :aria-busy="listBusy"
      >
        <header class="pane-header">
          <p class="eyebrow">
            EVIDENCE LEDGER
          </p>
          <h1 id="board-title">
            机会看板
          </h1>
          <p class="subcopy">
            需求证据、归属与下一步行动
          </p>
          <div class="tools">
            <select
              v-model="stateFilter"
              aria-label="按机会状态筛选"
            >
              <option value="">
                全部状态
              </option>
              <option
                v-for="(label, state) in stateLabels"
                :key="state"
                :value="state"
              >
                {{ label }}
              </option>
            </select>
            <select
              :value="pageLimit"
              aria-label="每页条数"
              @change="changeLimit"
            >
              <option value="25">
                25 条
              </option><option value="50">
                50 条
              </option><option value="100">
                100 条
              </option>
            </select>
            <button
              type="button"
              aria-label="刷新机会列表"
              :disabled="writePending"
              @click="manuallyRetryList"
            >
              <span aria-hidden="true">↻</span><span class="visually-hidden">刷新机会列表</span>
            </button>
          </div>
          <p class="order-note">
            <span aria-hidden="true">≡</span> 按后端返回顺序显示 · 前端不按分数重排
          </p>
          <p
            v-if="stale"
            class="stale-note"
          >
            数据可能已过期
          </p>
          <div
            v-if="listError"
            class="safe-error"
            role="alert"
          >
            <span aria-hidden="true">!</span><span>{{ listError }}</span>
            <small v-if="listRetryAfter">{{ listRetryAfter }}</small>
            <button
              type="button"
              @click="manuallyRetryList"
            >
              重试
            </button>
          </div>
        </header>

        <div
          v-if="listLoading"
          class="loading-state"
          role="status"
        >
          正在加载机会…
        </div>
        <p
          v-else-if="opportunities.length === 0 && !listError"
          class="empty-state"
        >
          暂无符合条件的机会
        </p>
        <ol
          v-else
          class="opportunity-list"
          aria-label="机会列表"
        >
          <li
            v-for="opportunity in opportunities"
            :key="opportunity.opportunity_id"
          >
            <button
              class="opportunity-card"
              type="button"
              :aria-current="selectedId === opportunity.opportunity_id"
              :disabled="writePending"
              @click="selectOpportunity(opportunity.opportunity_id)"
            >
              <span class="card-top">
                <span class="demo-badge">演示数据</span>
                <span class="status-tag"><span aria-hidden="true">●</span> {{ statusLabel(opportunity.state) }}</span>
              </span>
              <span class="account-name">{{ opportunity.account_name }}</span>
              <span class="metadata">
                {{ opportunity.country }} · {{ opportunity.product_category }} · 负责人
                {{ opportunity.owner_name ?? opportunity.owner ?? "未分配" }}
              </span>
              <span class="card-row">
                <span class="rank-tag">{{ rankBucketLabel(opportunity) }}</span>
                <span class="metadata">{{ opportunity.next_action_due ?? "未设置" }} 到期</span>
              </span>
              <span class="next-action"><strong>下一步：</strong>{{ opportunity.next_action ?? "待确认" }}</span>
            </button>
          </li>
        </ol>
      </section>

      <section
        class="detail-pane"
        aria-label="机会详情"
        :aria-busy="detailLoading"
      >
        <div
          v-if="writeRefreshWarning"
          class="write-refresh-warning"
          role="status"
        >
          {{ writeRefreshWarning }}
        </div>
        <div
          v-if="actionError"
          class="action-safe-error safe-error"
          role="alert"
        >
          <span aria-hidden="true">!</span><span>{{ actionError }}</span>
          <small v-if="actionRetryAfter">{{ actionRetryAfter }}</small>
        </div>
        <div
          v-if="detailLoading"
          class="detail-placeholder"
          role="status"
        >
          正在加载机会详情…
        </div>
        <div
          v-else-if="detailError"
          class="detail-placeholder safe-error"
          role="alert"
        >
          <span aria-hidden="true">!</span><span>{{ detailError }}</span>
          <small v-if="detailRetryAfter">{{ detailRetryAfter }}</small>
          <button
            v-if="selectedId"
            type="button"
            @click="manuallyRetryDetail"
          >
            重试
          </button>
        </div>
        <p
          v-else-if="!selectedOpportunity"
          class="detail-placeholder"
        >
          请选择一条机会查看详情
        </p>
        <div
          v-else
          class="detail-layout"
        >
          <OpportunityDetail
            :key="selectedOpportunity.opportunity_id"
            :opportunity="selectedOpportunity"
            :write-pending="writePending"
            :action-status="actionStatus"
            @transition="transition"
            @mark-lost="markLost"
          />
        </div>
      </section>
    </div>
  </main>
</template>

<style scoped>
:global(*) {
  box-sizing: border-box;
}

:global(html),
:global(body),
:global(#app),
:global(#app > main) {
  height: 100%;
  margin: 0;
  overflow: hidden;
}

:global(body) {
  min-width: 1080px;
  color: #172323;
  background: #f5f7f7;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
  font-size: 14px;
  line-height: 1.55;
}

.board-shell {
  display: grid;
  grid-template-rows: 68px minmax(0, 1fr);
  height: 100vh;
  overflow: hidden;
  color: #172323;
  background: #f5f7f7;
}

button,
select {
  min-height: 44px;
  border: 1px solid #9cb0ad;
  border-radius: 8px;
  color: #172323;
  background: #fff;
  font: inherit;
  font-weight: 680;
}

button {
  padding: 0 12px;
  cursor: pointer;
}

button:disabled {
  cursor: not-allowed;
  opacity: 0.62;
}

button:focus-visible,
select:focus-visible {
  outline: 3px solid #7c3aed;
  outline-offset: 2px;
}

h1,
p {
  margin-top: 0;
}

h1 {
  margin-bottom: 4px;
  font-size: 22px;
  line-height: 1.25;
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

.brand {
  display: flex;
  min-width: 172px;
  align-items: center;
  gap: 10px;
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
  display: flex;
  gap: 5px;
}

.topnav button {
  color: #d9ebe8;
  border-color: transparent;
  background: transparent;
}

.topnav button[aria-current="page"] {
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

.workspace {
  display: grid;
  grid-template-columns: 360px minmax(0, 1fr);
  gap: 14px;
  min-height: 0;
  padding: 14px;
  overflow: hidden;
}

.list-pane,
.detail-pane {
  min-width: 0;
  min-height: 0;
  overflow: hidden;
  border: 1px solid #cbd7d5;
  border-radius: 12px;
  background: #fff;
  box-shadow: 0 1px 2px rgba(20, 35, 35, 0.08);
}

.list-pane {
  display: flex;
  flex-direction: column;
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

.subcopy,
.order-note {
  margin: 0;
  color: #526363;
  font-size: 12px;
}

.tools {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 76px 44px;
  gap: 8px;
  margin-top: 14px;
}

.tools select {
  min-width: 0;
  padding: 0 8px;
}

.tools button {
  padding: 0;
}

.order-note {
  margin-top: 10px;
}

.stale-note {
  margin: 8px 0 0;
  color: #9a4f09;
  font-size: 12px;
  font-weight: 700;
}

.opportunity-list {
  margin: 0;
  padding: 10px;
  overflow: auto;
  list-style: none;
}

.opportunity-list li + li {
  margin-top: 9px;
}

.opportunity-card {
  width: 100%;
  min-height: 0;
  padding: 13px;
  border-color: #cbd7d5;
  border-radius: 10px;
  text-align: left;
  font-weight: 400;
  box-shadow: 0 1px 2px rgba(20, 35, 35, 0.08);
}

.opportunity-card[aria-current="true"] {
  border-color: #0f766e;
  background: #f0faf8;
  box-shadow: inset 3px 0 0 #0f766e, 0 1px 2px rgba(20, 35, 35, 0.08);
}

.card-top,
.card-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 9px;
}

.demo-badge,
.status-tag,
.rank-tag {
  display: inline-flex;
  min-height: 23px;
  align-items: center;
  gap: 5px;
  padding: 2px 7px;
  border-radius: 6px;
  font-size: 11px;
  font-weight: 750;
  line-height: 1.2;
  white-space: nowrap;
}

.demo-badge {
  color: #445554;
  border: 1px solid #cbd7d5;
  background: #e9efee;
}

.status-tag {
  color: #135f59;
  border: 1px solid #88c9c0;
  background: #e7f5f2;
}

.rank-tag {
  color: #5a4200;
  border: 1px solid #d9bd69;
  background: #fff8e5;
}

.account-name {
  display: block;
  margin: 10px 0 3px;
  font-size: 15px;
  font-weight: 760;
}

.metadata {
  display: block;
  color: #526363;
  font-size: 12px;
}

.card-row {
  margin-top: 8px;
}

.next-action {
  display: block;
  margin-top: 10px;
  padding-top: 9px;
  border-top: 1px solid #dfe7e5;
  color: #324443;
  font-size: 12px;
}

.next-action strong {
  color: #172323;
}

.detail-pane {
  overflow: auto;
}

.detail-layout {
  min-height: 100%;
  padding: 14px;
}

.loading-state,
.empty-state,
.detail-placeholder {
  margin: 0;
  padding: 24px;
  color: #526363;
}

.safe-error {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 8px;
  margin-top: 10px;
  padding: 8px 9px;
  color: #74251d;
  border: 1px solid #d9aaa4;
  border-radius: 8px;
  background: #fef0ec;
  font-size: 12px;
}

.safe-error small {
  grid-column: 2;
}

.safe-error button {
  grid-row: 1 / span 2;
  grid-column: 3;
}

.detail-placeholder.safe-error {
  margin: 14px;
}

.action-safe-error,
.write-refresh-warning {
  margin-right: 14px;
  margin-left: 14px;
}

.write-refresh-warning {
  padding: 9px 10px;
  color: #6f4a00;
  border: 1px solid #d9bd69;
  border-radius: 8px;
  background: #fff8e5;
  font-size: 12px;
  font-weight: 700;
}

.visually-hidden {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}

@media (max-width: 1240px) {
  .topbar {
    gap: 10px;
    padding: 0 14px;
  }

  .brand {
    min-width: 142px;
    font-size: 15px;
  }

  .topnav button {
    padding: 0 8px;
  }

  .workspace {
    grid-template-columns: 318px minmax(0, 1fr);
    gap: 10px;
    padding: 10px;
  }

  .tools {
    grid-template-columns: minmax(0, 1fr) 68px 44px;
  }

  .detail-layout {
    padding: 10px;
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
