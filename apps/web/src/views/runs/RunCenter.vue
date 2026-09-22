<script setup lang="ts">
import { codeLabel, sourcingStageLabel } from "../../components/displayLabels";
import { computed, inject, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { RouterLink, useRoute } from "vue-router";

import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";
import WebCoreObservationPanel from "../../components/WebCoreObservationPanel.vue";
import ResearchRunSummary from "../../components/ResearchRunSummary.vue";
import { stopLabel } from "../../components/researchLabels";
import { useQuoteRequestScope } from "../costing-quotes/quote-request-scope";

type ApiClient = ReturnType<typeof createApiClient>;
type RunDetail = components["schemas"]["RunDetailView"];
type RunSummary = components["schemas"]["RunSummaryView"];

const client = inject<ApiClient>("tradeos-api-client", apiClient);
const route = useRoute();
const runs = ref<RunSummary[]>([]);
const detail = ref<RunDetail | null>(null);
const observation = ref<components["schemas"]["WebCoreObservation"] | null>(null);
const observationError = ref<string | null>(null);
const observationLoading = ref(false);
const selectedRunId = ref<string | null>(null);
const statusFilter = ref("");
const workflowFilter = ref("");
const listLoading = ref(true);
const detailLoading = ref(false);
const listError = ref<string | null>(null);
const detailError = ref<string | null>(null);
const error = computed(() => detailError.value ?? listError.value);
let listRequestVersion = 0;
let detailRequestVersion = 0;
const identityGate = useQuoteRequestScope(client, () => [], () => {
  listRequestVersion += 1; detailRequestVersion += 1;
  runs.value = []; detail.value = null; selectedRunId.value = null;
  observation.value = null; observationError.value = null; observationLoading.value = false;
  listLoading.value = false; detailLoading.value = false; listError.value = null; detailError.value = null;
  statusFilter.value = ""; workflowFilter.value = "";
});

const filteredRuns = computed(() => {
  const workflow = workflowFilter.value.trim().toLowerCase();
  return runs.value.filter((run) =>
    (!statusFilter.value || run.status === statusFilter.value)
    && (!workflow || `${codeLabel(run.workflow_type)} ${run.workflow_type}`.toLowerCase().includes(workflow)),
  );
});
const activeCount = computed(() => runs.value.filter((run) =>
  ["pending", "running", "waiting_human", "waiting_event"].includes(run.status),
).length);
const failedCount = computed(() => runs.value.filter((run) =>
  ["failed", "timed_out"].includes(run.status),
).length);
const completedCount = computed(() => runs.value.filter((run) =>
  run.status === "completed",
).length);

const statusLabels: Readonly<Record<string, string>> = Object.freeze({
  cancelled: "已取消",
  completed: "已完成",
  failed: "失败",
  pending: "待执行",
  running: "运行中",
  timed_out: "已超时",
  waiting_event: "等待事件",
  waiting_human: "等待人工",
});

function safeError(status: number): string {
  if (status === 401) return "登录身份已失效，请重新选择有效身份";
  if (status === 403) return "只有老板可以查看运行记录审计记录";
  if (status === 404) return "该运行记录已不存在或不属于当前租户";
  if (status === 503) return "运行记录审计服务暂不可用";
  return "运行记录审计记录读取失败，请稍后重试";
}

function revokeRead(status: number): void {
  identityGate.invalidate(); listRequestVersion++; detailRequestVersion++;
  runs.value = []; detail.value = null; selectedRunId.value = null;
  observation.value = null; observationLoading.value = false;
  listLoading.value = false; detailLoading.value = false;
  observationError.value = safeError(status); listError.value = safeError(status); detailError.value = safeError(status);
}

function formatTime(value: string | null): string {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? value
    : parsed.toLocaleString("zh-CN", { hour12: false });
}

function statusLabel(status: string): string {
  return statusLabels[status] ?? status;
}

async function loadDetail(runId: string): Promise<void> {
  const op = identityGate.begin("detail"); if (!op?.valid()) return;
  const requestVersion = ++detailRequestVersion;
  selectedRunId.value = runId;
  detail.value = null;
  detailLoading.value = true;
  detailError.value = null;
  try {
    const result = await client.GET("/runs/{run_id}", {
      params: { path: { run_id: runId } },
      signal: op.signal,
    });
    if (!op.valid() || requestVersion !== detailRequestVersion) return;
    if (result.response.status !== 200 || !result.data) {
      detail.value = null;
      if ([401,403].includes(result.response.status)) revokeRead(result.response.status);
      detailError.value = safeError(result.response.status);
      return;
    }
    detail.value = result.data;
  } catch {
    if (!op.valid() || requestVersion !== detailRequestVersion) return;
    detail.value = null;
    detailError.value = "无法连接运行记录审计服务";
  } finally {
    if (op.valid() && requestVersion === detailRequestVersion) detailLoading.value = false;
  }
}

async function loadRuns(): Promise<void> {
  const op = identityGate.begin("list"); if (!op?.valid()) return;
  const requestVersion = ++listRequestVersion;
  listLoading.value = true;
  listError.value = null;
  try {
    const result = await client.GET("/runs", {
      params: { query: { limit: 50 } },
      signal: op.signal,
    });
    if (!op.valid() || requestVersion !== listRequestVersion) return;
    if (result.response.status !== 200 || !result.data) {
      runs.value = [];
      if ([401,403,404].includes(result.response.status)) revokeRead(result.response.status);
      listError.value = safeError(result.response.status);
      return;
    }
    runs.value = result.data;
    if (selectedRunId.value === null && route.query.run === undefined && runs.value[0]) {
      await loadDetail(runs.value[0].run_id);
    }
  } catch {
    if (!op.valid() || requestVersion !== listRequestVersion) return;
    runs.value = [];
    listError.value = "无法连接运行记录审计服务";
  } finally {
    if (op.valid() && requestVersion === listRequestVersion) listLoading.value = false;
  }
}

async function loadObservation(): Promise<void> {
  const op = identityGate.begin("observation"); if (!op?.valid()) return;
  observation.value = null; observationError.value = null; observationLoading.value = true;
  try {
    const result = await client.GET("/runs/observability", { signal: op.signal });
    if (!op.valid()) return;
    if (result.response.status !== 200 || !result.data || !Array.isArray(result.data.stages) || !result.data.handoffs) {
      if ([401, 403].includes(result.response.status)) revokeRead(result.response.status);
      observationError.value = safeError(result.response.status);
      return;
    }
    observation.value = result.data;
  } catch {
    if (op.valid()) observationError.value = "无法连接观测服务";
  } finally {
    if (op.valid()) observationLoading.value = false;
  }
}

async function refreshRuns(): Promise<void> {
  await Promise.all([
    loadRuns(),
    loadObservation(),
    selectedRunId.value ? loadDetail(selectedRunId.value) : Promise.resolve(),
  ]);
}

watch(() => route.query.run, (runId) => {
  detailRequestVersion += 1;
  selectedRunId.value = null;
  detail.value = null;
  detailLoading.value = false;
  detailError.value = null;
  if (typeof runId === "string" && runId.trim()) void loadDetail(runId);
  else if (runId !== undefined) detailError.value = "运行记录链接无效，请使用单个非空运行记录编号";
  else if (runs.value[0]) void loadDetail(runs.value[0].run_id);
}, { immediate: true });

onMounted(() => { void loadRuns(); void loadObservation(); });
onBeforeUnmount(() => {
  listRequestVersion += 1;
  detailRequestVersion += 1;
});
</script>

<template>
  <div class="shell run-shell">
    <div class="page-head run-head">
      <div>
        <p class="phase-eyebrow">
          运行审计
        </p>
        <h1>运行记录</h1>
      </div>
      <button
        type="button"
        :disabled="listLoading"
        @click="refreshRuns"
      >
        {{ listLoading ? "加载中…" : "刷新记录" }}
      </button>
    </div>

    <div class="safe-banner">
      <span aria-hidden="true">i</span>
      <div>本页展示工作流、步骤、工具、资料和审批的安全摘要；不展示内部执行数据、客户正文、个人信息或凭证。报价运行记录中的审批完成不代表已发送。</div>
    </div>
    <div
      v-if="error"
      class="safe-banner danger"
      role="alert"
    >
      {{ error }}
    </div>

    <section
      class="run-metrics"
      aria-label="运行记录审计概览"
    >
      <article><span>最近记录</span><strong>{{ listLoading || listError ? "—" : runs.length }}</strong><p>按创建时间倒序，最多 50 条</p></article>
      <article><span>进行中 / 等待</span><strong>{{ listLoading || listError ? "—" : activeCount }}</strong><p>含等待人工与外部事件</p></article>
      <article><span>失败 / 超时</span><strong>{{ listLoading || listError ? "—" : failedCount }}</strong><p>错误只展示脱敏类别</p></article>
      <article><span>已完成</span><strong>{{ listLoading || listError ? "—" : completedCount }}</strong><p>状态机正常到达终态</p></article>
    </section>

    <WebCoreObservationPanel
      :observation="observation"
      :error="observationError"
      :loading="observationLoading"
    />

    <section class="run-layout">
      <article class="run-panel run-index">
        <header>
          <div>
            <p class="card-kicker">
              运行记录列表
            </p>
            <h2>执行记录</h2>
          </div>
          <span>{{ filteredRuns.length }} / {{ runs.length }}</span>
        </header>
        <div class="run-filters">
          <label>
            <span>工作流</span>
            <input
              v-model="workflowFilter"
              type="search"
              placeholder="按工作流类型筛选"
            >
          </label>
          <label>
            <span>状态</span>
            <select v-model="statusFilter">
              <option value="">全部状态</option>
              <option
                v-for="(label, value) in statusLabels"
                :key="value"
                :value="value"
              >
                {{ label }}
              </option>
            </select>
          </label>
        </div>
        <div
          v-if="listLoading"
          class="empty"
        >
          正在读取运行记录…
        </div>
        <div
          v-else-if="listError"
          class="empty"
        >
          记录读取失败，请刷新核对
        </div>
        <div
          v-else-if="!filteredRuns.length"
          class="empty"
        >
          {{ runs.length ? "没有符合筛选条件的运行记录" : "当前没有可审计的运行记录" }}
        </div>
        <div
          v-else
          class="run-list"
        >
          <button
            v-for="run in filteredRuns"
            :key="run.run_id"
            type="button"
            class="run-row"
            :class="{ selected: run.run_id === selectedRunId }"
            @click="loadDetail(run.run_id)"
          >
            <span class="run-row-top">
              <strong>{{ codeLabel(run.workflow_type) }}</strong>
              <small :class="`state-${run.status}`">{{ statusLabel(run.status) }}</small>
            </span>
            <span>{{ run.subject_ref }}</span>
            <span v-if="run.research">只研究 · {{ stopLabel(run.research.stop_reason) }}</span>
            <span class="run-row-meta">{{ codeLabel(run.current_step) }} · {{ formatTime(run.last_activity_at) }}</span>
          </button>
        </div>
      </article>

      <article class="run-panel run-detail">
        <header>
          <div>
            <p class="card-kicker">
              证据链
            </p>
            <h2>审计证据链</h2>
          </div>
          <span v-if="detail">{{ detail.summary.run_id }}</span>
        </header>
        <div
          v-if="detailLoading"
          class="empty"
        >
          正在读取证据链…
        </div>
        <div
          v-else-if="detailError"
          class="empty"
        >
          {{ detailError }}
        </div>
        <div
          v-else-if="!detail"
          class="empty"
        >
          选择一条运行记录查看步骤、工具、产物与审批
        </div>
        <div
          v-else
          class="detail-content"
        >
          <section class="summary-grid">
            <div><span>工作流</span><strong>{{ codeLabel(detail.summary.workflow_type) }} v{{ detail.summary.workflow_version }}</strong></div>
            <div><span>业务主体</span><strong>{{ detail.summary.subject_ref }}</strong></div>
            <div><span>当前步骤</span><strong>{{ codeLabel(detail.summary.current_step) }}</strong></div>
            <div><span>状态</span><strong>{{ statusLabel(detail.summary.status) }}</strong></div>
            <div><span>创建时间</span><strong>{{ formatTime(detail.summary.created_at) }}</strong></div>
            <div><span>下次调度</span><strong>{{ formatTime(detail.summary.next_poll_at) }}</strong></div>
            <div><span>重试次数</span><strong>{{ detail.summary.retry_count }}</strong></div>
            <div><span>脱敏错误</span><strong>{{ codeLabel(detail.summary.last_error ?? "无") }}</strong></div>
          </section>

          <section
            v-if="detail.observation"
            class="audit-section run-observation"
          >
            <h3>停留位置与可追溯输入</h3>
            <p>当前步骤：{{ codeLabel(detail.summary.current_step) }} · 处理责任：{{ detail.observation.responsible_employee_id ?? '未知，需老板核对分工' }}</p>
            <p>记录时间跨度：{{ detail.observation.recorded_span_seconds === null ? '未知' : `${detail.observation.recorded_span_seconds} 秒` }} · 时间异常 {{ detail.observation.invalid_time_count }}。这是记录时钟跨度，不是执行或人工工作耗时。</p>
            <p>工具调用 {{ detail.observation.call_count }} · 尝试 {{ detail.observation.attempt_count }} · 重放回执 {{ detail.observation.duplicate_receipt_count }}；模型计量单位、人工工时和费用未知，缺少实际用量、计时和费率。</p>
            <p>关联机会：{{ detail.observation.opportunity_id ?? '未知' }}</p>
            <p v-if="detail.observation.handoff_id">
              <RouterLink :to="`/crm/handoffs/${detail.observation.handoff_id}`">
                打开关联接管与机会
              </RouterLink>
            </p>
            <p v-if="detail.observation.need_id">
              <RouterLink :to="`/demand/needs/${detail.observation.need_id}`">
                打开关联已验证需求
              </RouterLink>
            </p>
            <p v-if="!detail.observation.handoff_id && !detail.observation.need_id">
              没有可核实的业务对象绑定；使用下方已有审批引用核对。
            </p>
          </section>

          <ResearchRunSummary
            v-if="detail.summary.research"
            :research="detail.summary.research"
          />

          <section
            v-if="detail.summary.sourcing"
            class="audit-section sourcing-run-summary"
          >
            <header><h3>寻源运行摘要</h3><span>{{ detail.summary.sourcing.case_id }}</span></header>
            <p>计划：{{ codeLabel(detail.summary.sourcing.plan_status ?? "未确认") }} · 候选 {{ detail.summary.sourcing.candidate_count }} · 搜索尝试 {{ detail.summary.sourcing.search_attempt_count }} · 页面尝试 {{ detail.summary.sourcing.page_attempt_count }}</p>
            <p>免费额度：已预留 {{ detail.summary.sourcing.reserved_credits }} / 已消耗 {{ detail.summary.sourcing.consumed_credits }} / 不确定 {{ detail.summary.sourcing.uncertain_credits }}</p>
            <p v-if="detail.summary.sourcing.stop_reason">
              停止：{{ codeLabel(detail.summary.sourcing.stop_reason.code) }} · {{ sourcingStageLabel(detail.summary.sourcing.stop_reason.stage) }}
            </p>
            <ul
              v-if="detail.summary.sourcing.ladder.length"
              class="sourcing-ladder"
            >
              <li
                v-for="item in detail.summary.sourcing.ladder"
                :key="`${item.rung}-${item.outcome}`"
              >
                第 {{ item.rung }} 级：{{ codeLabel(item.outcome) }}
              </li>
            </ul>
            <p class="meta">
              该摘要不包含搜索词、页面正文、供应商联系人、成本或客户报价。
            </p>
          </section>

          <section class="audit-section">
            <header><h3>步骤时间线</h3><span>{{ detail.steps.length }} 步</span></header>
            <div
              v-if="!detail.steps.length"
              class="audit-empty"
            >
              没有持久化步骤记录
            </div>
            <ol
              v-else
              class="step-list"
            >
              <li
                v-for="step in detail.steps"
                :key="step.step_id"
              >
                <span
                  class="timeline-dot"
                  :class="`state-${step.status}`"
                />
                <div><strong>{{ codeLabel(step.step_name) }}</strong><small>{{ statusLabel(step.status) }} · 尝试 {{ step.attempt }} 次</small><p>{{ formatTime(step.created_at) }} → {{ formatTime(step.updated_at) }}</p><em v-if="step.error">{{ codeLabel(step.error) }}</em></div>
              </li>
            </ol>
          </section>

          <section class="audit-section">
            <header><h3>工具调用</h3><span>{{ detail.tool_calls.length }} 次</span></header>
            <div
              v-if="!detail.tool_calls.length"
              class="audit-empty"
            >
              该运行记录没有工具调用元数据
            </div>
            <div
              v-else
              class="audit-cards"
            >
              <article
                v-for="call in detail.tool_calls"
                :key="call.tool_call_id"
              >
                <header><strong>{{ codeLabel(call.tool_id) }}</strong><span>{{ codeLabel(call.status) }}</span></header>
                <p>版本 {{ call.tool_version }} · 风险 {{ codeLabel(call.risk_level) }} · 成本级别 {{ codeLabel(call.cost_class) }}</p>
                <small>尝试 {{ call.attempt_count }} 次 · {{ codeLabel(call.error_category ?? "无错误类别") }}</small>
              </article>
            </div>
          </section>

          <div class="evidence-grid">
            <section class="audit-section">
              <header><h3>系统产物</h3><span>{{ detail.artifacts.length }} 件</span></header>
              <div
                v-if="!detail.artifacts.length"
                class="audit-empty compact"
              >
                没有系统产物引用
              </div>
              <div
                v-else
                class="audit-cards compact-cards"
              >
                <article
                  v-for="artifact in detail.artifacts"
                  :key="artifact.artifact_id"
                >
                  <strong>{{ codeLabel(artifact.kind) }}</strong><p>{{ artifact.subject_ref }}</p><small>{{ artifact.generated_by }} · {{ formatTime(artifact.generated_at) }}</small>
                </article>
              </div>
            </section>
            <section class="audit-section">
              <header><h3>人工审批</h3><span>{{ detail.approvals.length }} 项</span></header>
              <div
                v-if="!detail.approvals.length"
                class="audit-empty compact"
              >
                没有由该运行记录发起的审批
              </div>
              <div
                v-else
                class="audit-cards compact-cards"
              >
                <article
                  v-for="approval in detail.approvals"
                  :key="approval.approval_id"
                >
                  <header><strong>{{ codeLabel(approval.approval_type) }}</strong><span>{{ codeLabel(approval.state) }}</span></header><p>
                    <RouterLink :to="{ path: '/approvals', query: { approval_id: approval.approval_id } }">
                      {{ approval.approval_id }}
                    </RouterLink>
                  </p><small>创建于 {{ formatTime(approval.created_at) }}</small>
                </article>
              </div>
            </section>
          </div>
        </div>
      </article>
    </section>
  </div>
</template>

<style scoped>
.run-shell { overflow: auto; gap: var(--space4); }
.run-head { justify-content: space-between; padding-top: var(--space3); }
.run-metrics { display: grid; grid-template-columns: repeat(4, 1fr); gap: var(--space3); }
.run-metrics article, .run-panel { border: 1px solid var(--border); border-radius: 12px; background: var(--surface); }
.run-metrics article { padding: var(--space4); }
.run-metrics span, .summary-grid span { color: var(--text-secondary); font-size: 10px; font-weight: 700; }
.run-metrics strong { display: block; margin-top: var(--space1); font-size: 28px; }
.run-metrics p { color: var(--text-secondary); font-size: 10px; }
.run-layout { display: grid; grid-template-columns: minmax(300px, .66fr) minmax(560px, 1.34fr); gap: var(--space4); min-height: 610px; }
.run-panel { min-width: 0; padding: var(--space4); }
.run-panel > header, .audit-section > header { display: flex; justify-content: space-between; align-items: flex-end; gap: var(--space3); }
.run-panel > header { padding-bottom: var(--space3); }
.run-panel > header span, .audit-section > header span { color: var(--text-secondary); font-size: 10px; overflow-wrap: anywhere; }
.run-panel h2 { font-size: 18px; }
.run-filters { display: grid; grid-template-columns: 1fr 120px; gap: var(--space2); padding-bottom: var(--space3); }
.run-filters label { display: grid; gap: 4px; color: var(--text-secondary); font-size: 9px; font-weight: 700; }
.run-filters input, .run-filters select { width: 100%; border: 1px solid var(--border); border-radius: 8px; background: var(--surface); padding: 8px 9px; color: var(--text-primary); font: inherit; }
.run-list { display: grid; gap: var(--space2); max-height: 545px; overflow: auto; }
.run-row { display: grid; gap: 5px; width: 100%; border: 1px solid var(--border); border-radius: 10px; background: var(--surface); padding: var(--space3); color: var(--text-primary); text-align: left; }
.run-row:hover, .run-row.selected { border-color: var(--action); background: #f7f9ff; }
.run-row.selected { box-shadow: inset 3px 0 var(--action); }
.run-row-top { display: flex; justify-content: space-between; align-items: center; gap: var(--space2); }
.run-row-top strong { font-size: 12px; overflow-wrap: anywhere; }
.run-row > span:not(.run-row-top) { color: var(--text-secondary); font-size: 10px; overflow-wrap: anywhere; }
.run-row-meta { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.run-row small, .audit-cards header span { border-radius: 999px; background: #edf0f4; padding: 2px 7px; color: var(--text-secondary); font-size: 9px; white-space: nowrap; }
.state-running, .state-pending, .state-waiting_human, .state-waiting_event { background: #eaf0ff !important; color: var(--action) !important; }
.state-completed, .state-succeeded, .state-approved { background: var(--fact-soft) !important; color: var(--fact) !important; }
.state-failed, .state-timed_out, .state-rejected { background: var(--danger-soft) !important; color: var(--danger) !important; }
.detail-content { display: grid; gap: var(--space3); max-height: 590px; overflow: auto; padding-right: 3px; }
.summary-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 1px; overflow: hidden; border: 1px solid var(--border); border-radius: 10px; background: var(--border); }
.summary-grid div { min-width: 0; background: var(--surface); padding: var(--space3); }
.summary-grid strong { display: block; margin-top: 5px; font-size: 11px; overflow-wrap: anywhere; }
.audit-section { border: 1px solid var(--border); border-radius: 10px; padding: var(--space3); }
.audit-section h3 { font-size: 13px; }
.step-list { display: grid; gap: var(--space2); margin: var(--space3) 0 0; padding: 0; list-style: none; }
.step-list li { display: grid; grid-template-columns: 12px 1fr; gap: var(--space2); }
.timeline-dot { width: 10px; height: 10px; margin-top: 4px; border-radius: 50%; background: var(--border); }
.step-list div { display: grid; gap: 3px; }
.step-list strong { font-size: 11px; }
.step-list small, .step-list p, .step-list em, .audit-cards p, .audit-cards small { color: var(--text-secondary); font-size: 9px; font-style: normal; }
.step-list em { color: var(--danger); }
.audit-cards { display: grid; grid-template-columns: repeat(2, 1fr); gap: var(--space2); margin-top: var(--space3); }
.audit-cards article { min-width: 0; border: 1px solid var(--border); border-radius: 8px; padding: var(--space2); }
.audit-cards article > header { display: flex; justify-content: space-between; gap: var(--space2); }
.audit-cards strong, .audit-cards p { overflow-wrap: anywhere; }
.audit-cards strong { font-size: 10px; }
.evidence-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: var(--space3); }
.compact-cards { grid-template-columns: 1fr; }
.audit-empty { display: grid; place-items: center; min-height: 90px; color: var(--text-secondary); font-size: 10px; }
.audit-empty.compact { min-height: 70px; }
.empty { display: grid; place-items: center; min-height: 260px; color: var(--text-secondary); }
@media (max-width: 1100px) { .run-layout { grid-template-columns: 1fr; } .run-list, .detail-content { max-height: none; } }
@media (max-width: 760px) { .run-metrics, .summary-grid, .evidence-grid { grid-template-columns: repeat(2, 1fr); } .run-filters { grid-template-columns: 1fr; } }
@media (max-width: 520px) { .run-metrics, .summary-grid, .evidence-grid, .audit-cards { grid-template-columns: 1fr; } .run-head { align-items: flex-start; } }
</style>
