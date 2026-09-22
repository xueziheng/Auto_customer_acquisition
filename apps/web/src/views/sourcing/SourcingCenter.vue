<script setup lang="ts">
import { codeLabel } from "../../components/displayLabels";
import { computed, inject, onMounted, ref } from "vue";
import { RouterLink } from "vue-router";

import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";
import { displayZonedIsoTime, elapsedZonedSeconds } from "./zonedTime";

type ApiClient = ReturnType<typeof createApiClient>;
type Admission = components["schemas"]["SourcingAdmissionReadView"];
type AdmissionList = components["schemas"]["SourcingAdmissionListView"];
type AdmissionPolicy = components["schemas"]["SourcingAdmissionPolicyView"];
type AdmissionState = components["schemas"]["AdmissionState"];
type SourcingCase = components["schemas"]["SourcingCaseReadView"];

const client = inject<ApiClient>("tradeos-api-client", apiClient);
const waitingAdmissions = ref<Admission[]>([]);
const blockedAdmissions = ref<Admission[]>([]);
const activeAdmissions = ref<Admission[]>([]);
const cases = ref<SourcingCase[]>([]);
const policy = ref<AdmissionPolicy>({ status: "policy_status_unknown" });
const loading = ref(true);
const partitionReady = ref(false);
const error = ref<string | null>(null);
const notice = ref<string | null>(null);
const manualTarget = ref<Admission | null>(null);
const pendingAdmissionId = ref<string | null>(null);
const manualRequestKeys = new Map<string, string>();
let loadGeneration = 0;

const policyLabel = computed(() => ({
  automatic_admission_disabled: "自动准入已关闭",
  enabled: "自动准入已启用",
  policy_not_configured: "自动准入未配置",
  policy_status_unknown: "自动准入策略状态未知",
}[policy.value.status]));
function formatTime(value: string | null | undefined): string {
  return displayZonedIsoTime(value);
}

function formatWait(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} 分钟`;
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return remainder ? `${hours} 小时 ${remainder} 分钟` : `${hours} 小时`;
}

function admittedWait(item: Admission): string {
  const seconds = elapsedZonedSeconds(item.ready_at, item.admitted_at);
  return seconds === null ? "未知" : formatWait(seconds);
}

function activeTiming(item: Admission): string {
  return item.state === "admitted"
    ? `准入等待用时 ${admittedWait(item)}`
    : `自就绪起 ${formatWait(item.waiting_duration_seconds)}`;
}

function clusterLabel(item: Admission): string {
  return item.cluster_id
    ? `${item.cluster_member_count ?? "未知"} 条已验证需求`
    : "尚未归簇（按 1 条需求排序）";
}

function blockedLabel(item: Admission): string | null {
  if (item.blocked_reason === "case_state_mismatch") return "案例状态不匹配";
  if (item.blocked_reason === "priority_facts_invalid") return "排序事实无效";
  return null;
}

function listError(status: number): string {
  if (status === 403) return "当前身份无权读取寻源准入队列";
  if (status === 503) return "寻源准入队列暂不可用";
  return "寻源准入队列读取失败";
}

async function loadAdmissionState(state: AdmissionState, generation: number): Promise<AdmissionList | null> {
  try {
    const result = await client.GET("/sourcing-admissions", {
      params: { query: { limit: 50, state } },
    });
    if (result.response.status === 200 && result.data) {
      if (result.data.items.length < 50) return result.data;
      if (generation === loadGeneration) {
        error.value ??= "准入队列可能已截断，无法安全展示等待与处理中的准入记录";
      }
      return null;
    }
    if (generation === loadGeneration) error.value ??= listError(result.response.status);
  } catch {
    if (generation === loadGeneration) error.value ??= "无法连接寻源准入服务";
  }
  return null;
}

async function loadCases(): Promise<SourcingCase[]> {
  try {
    const result = await client.GET("/sourcing-cases", { params: { query: { limit: 50 } } });
    if (result.response.status === 200 && result.data) return result.data;
  } catch { /* Case 工作台与 admission 队列独立展示。 */ }
  return [];
}

function hasDuplicateAdmissionIdentity(groups: Admission[][]): boolean {
  const admissionIds = new Set<string>();
  const caseIds = new Set<string>();
  const needIds = new Set<string>();
  for (const item of groups.flat()) {
    if (
      admissionIds.has(item.admission_id)
      || caseIds.has(item.case_id)
      || needIds.has(item.need_id)
    ) return true;
    admissionIds.add(item.admission_id);
    caseIds.add(item.case_id);
    needIds.add(item.need_id);
  }
  return false;
}

async function loadAll(): Promise<void> {
  const generation = ++loadGeneration;
  loading.value = true;
  partitionReady.value = false;
  manualTarget.value = null;
  error.value = null;
  const [waiting, blocked, starting, admitted, loadedCases] = await Promise.all([
    loadAdmissionState("waiting", generation),
    loadAdmissionState("blocked", generation),
    loadAdmissionState("starting", generation),
    loadAdmissionState("admitted", generation),
    loadCases(),
  ]);
  if (generation !== loadGeneration) return;
  cases.value = loadedCases;
  if (!waiting || !blocked || !starting || !admitted) {
    waitingAdmissions.value = [];
    blockedAdmissions.value = [];
    activeAdmissions.value = [];
    policy.value = waiting?.policy ?? blocked?.policy ?? starting?.policy ?? admitted?.policy ?? { status: "policy_status_unknown" };
    loading.value = false;
    return;
  }
  if (hasDuplicateAdmissionIdentity([waiting.items, blocked.items, starting.items, admitted.items])) {
    waitingAdmissions.value = [];
    blockedAdmissions.value = [];
    activeAdmissions.value = [];
    policy.value = waiting.policy;
    error.value = "准入队列状态不一致，已停止展示，请刷新重试";
    loading.value = false;
    return;
  }
  waitingAdmissions.value = waiting?.items ?? [];
  blockedAdmissions.value = blocked?.items ?? [];
  activeAdmissions.value = [...(starting?.items ?? []), ...(admitted?.items ?? [])];
  policy.value = waiting?.policy ?? blocked?.policy ?? starting?.policy ?? admitted?.policy ?? { status: "policy_status_unknown" };
  partitionReady.value = true;
  loading.value = false;
}

function openManualDialog(item: Admission): void {
  if (!item.can_current_user_manual_start || pendingAdmissionId.value === item.admission_id) return;
  manualTarget.value = item;
  notice.value = null;
  error.value = null;
}

function closeManualDialog(): void {
  if (pendingAdmissionId.value) return;
  manualTarget.value = null;
}

function manualError(status: number): string {
  if (status === 409) return "准入状态已变化，请刷新队列后重试";
  if (status === 503) return "寻源准入服务暂不可用，请稍后重试";
  if (status === 403) return "当前身份无权人工准入此寻源案例";
  if (status === 404) return "准入记录不存在或不属于当前租户";
  return "人工准入未完成，请稍后重试";
}

async function confirmManualAdmission(): Promise<void> {
  const target = manualTarget.value;
  if (!target || pendingAdmissionId.value) return;
  pendingAdmissionId.value = target.admission_id;
  error.value = null;
  const requestKey = manualRequestKeys.get(target.admission_id) ?? globalThis.crypto.randomUUID();
  manualRequestKeys.set(target.admission_id, requestKey);
  try {
    const result = await client.POST("/sourcing-admissions/{admission_id}/admit", {
      params: {
        header: { "Idempotency-Key": requestKey },
        path: { admission_id: target.admission_id },
      },
    });
    if (result.response.status === 200 && result.data) {
      manualRequestKeys.delete(target.admission_id);
      manualTarget.value = null;
      notice.value = "已准入；这只代表该寻源案例获准启动，不代表寻源、询价或报价已完成。";
      await loadAll();
      return;
    }
    error.value = manualError(result.response.status);
    manualTarget.value = null;
  } catch {
    error.value = "无法连接寻源准入服务，请稍后按原请求重试";
    manualTarget.value = null;
  } finally {
    pendingAdmissionId.value = null;
  }
}

onMounted(() => void loadAll());
</script>

<template>
  <div class="shell sourcing-shell">
    <div class="page-head">
      <div>
        <p class="phase-eyebrow">
          寻源中心
        </p><h1>寻源中心</h1>
      </div>
      <button
        type="button"
        :disabled="loading"
        @click="loadAll"
      >
        {{ loading ? "加载中…" : "刷新" }}
      </button>
    </div>
    <div class="safe-banner">
      <span aria-hidden="true">i</span><div>一个需求对应一个寻源案例；需求簇不是合并订单。簇规模只决定尚未启动寻源案例的准入顺序。</div>
    </div>
    <div class="policy-strip">
      <strong>{{ policyLabel }}</strong>
      <span v-if="policy.directive_version">老板指令 v{{ policy.directive_version }} · 每轮上限 {{ policy.batch_limit }}</span>
    </div>
    <div
      v-if="error"
      class="safe-banner danger"
      role="alert"
    >
      {{ error }}
    </div>
    <div
      v-if="notice"
      class="safe-banner"
      role="status"
    >
      {{ notice }}
    </div>
    <div
      v-if="loading"
      class="empty"
    >
      正在读取准入队列…
    </div>
    <template v-if="!loading && partitionReady">
      <section
        class="queue-section"
        data-section="waiting-admission"
        aria-labelledby="waiting-title"
      >
        <header>
          <div>
            <p class="phase-eyebrow">
              待准入队列
            </p><h2 id="waiting-title">
              等待准入
            </h2>
          </div><span>{{ waitingAdmissions.length + blockedAdmissions.length }} 个</span>
        </header>
        <p
          v-if="!waitingAdmissions.length && !blockedAdmissions.length"
          class="empty"
        >
          当前没有等待准入的寻源案例
        </p>
        <article
          v-for="item in waitingAdmissions"
          :key="item.admission_id"
          class="admission-row"
          :data-admission-id="item.admission_id"
        >
          <div class="admission-main">
            <RouterLink :to="`/sourcing/${item.admission_id}`">
              <strong>{{ item.case_id }}</strong>
            </RouterLink>
            <small>需求 {{ item.need_id }}</small>
          </div>
          <dl>
            <div><dt>需求簇</dt><dd>{{ clusterLabel(item) }}</dd></div>
            <div><dt>就绪 / 等待</dt><dd>{{ formatTime(item.ready_at) }} · 当前已等待 {{ formatWait(item.waiting_duration_seconds) }}</dd></div>
            <div><dt>排序版本</dt><dd>{{ item.ranking_version ?? "无有效快照" }}</dd></div>
          </dl>
          <p class="explanation">
            {{ item.explanation ?? "排序事实待修复" }}
          </p>
          <button
            v-if="item.can_current_user_manual_start"
            data-manual-admit
            type="button"
            :disabled="pendingAdmissionId === item.admission_id"
            @click="openManualDialog(item)"
          >
            {{ pendingAdmissionId === item.admission_id ? "准入中…" : "人工准入" }}
          </button>
        </article>
        <article
          v-for="item in blockedAdmissions"
          :key="item.admission_id"
          class="admission-row blocked-row"
          :data-admission-id="item.admission_id"
        >
          <div class="admission-main">
            <RouterLink :to="`/sourcing/${item.admission_id}`">
              <strong>{{ item.case_id }}</strong>
            </RouterLink>
            <small>需求 {{ item.need_id }}</small>
          </div>
          <dl>
            <div><dt>状态</dt><dd>已阻断 · {{ blockedLabel(item) }}</dd></div>
            <div><dt>就绪 / 时长</dt><dd>{{ formatTime(item.ready_at) }} · 自就绪起 {{ formatWait(item.waiting_duration_seconds) }}</dd></div>
            <div><dt>排序版本</dt><dd>{{ item.ranking_version ?? "无有效快照" }}</dd></div>
          </dl>
          <p class="explanation">
            {{ item.explanation ?? "排序事实待修复" }}
          </p>
        </article>
      </section>

      <section
        class="queue-section"
        data-section="active-admissions"
        aria-labelledby="active-title"
      >
        <header>
          <div>
            <p class="phase-eyebrow">
              准入处理中
            </p><h2 id="active-title">
              处理中
            </h2>
          </div><span>{{ activeAdmissions.length }} 个</span>
        </header>
        <p
          v-if="!activeAdmissions.length"
          class="empty"
        >
          当前没有处理中的准入记录
        </p>
        <article
          v-for="item in activeAdmissions"
          :key="item.admission_id"
          class="case-row"
        >
          <span><strong>{{ item.case_id }}</strong><small>需求 {{ item.need_id }}</small></span>
          <span>{{ item.state === "starting" ? "启动绑定中" : "已准入" }}</span>
          <span>
            {{ formatTime(item.ready_at) }} · {{ activeTiming(item) }}
            <small>{{ item.state === "starting" ? item.explanation : `${formatTime(item.admitted_at)} · ${item.admitted_by ?? "未知"}` }}</small>
          </span>
          <nav
            class="case-actions"
            :aria-label="`${item.case_id} 入口`"
          >
            <RouterLink :to="`/sourcing/${item.admission_id}`">
              准入审计详情
            </RouterLink>
            <RouterLink :to="`/sourcing/${item.case_id}`">
              寻源案例工作台
            </RouterLink>
          </nav>
        </article>
      </section>
    </template>

    <section
      v-if="!loading"
      class="queue-section"
      data-section="case-workbench"
      aria-labelledby="case-workbench-title"
    >
      <header>
        <div>
          <p class="phase-eyebrow">
            寻源案例工作台
          </p><h2 id="case-workbench-title">
            寻源案例工作台
          </h2>
        </div><span>{{ cases.length }} 个</span>
      </header>
      <p class="case-workbench-note">
        此处独立展示寻源案例记录，不根据准入队列推断其准入状态。
      </p>
      <p
        v-if="!cases.length"
        class="empty"
      >
        当前没有可展示的寻源案例
      </p>
      <RouterLink
        v-for="item in cases"
        :key="item.case_id"
        class="case-row"
        :to="`/sourcing/${item.case_id}`"
      >
        <span><strong>{{ item.case_id }}</strong><small>需求 {{ item.need_id }}</small></span>
        <span>寻源案例状态：{{ codeLabel(item.state) }} · 梯子至 {{ item.ladder_checked_to ?? "未知" }}</span>
        <span v-if="item.stop">停止：{{ codeLabel(item.stop.code) }}</span>
      </RouterLink>
    </section>

    <div
      v-if="manualTarget"
      class="dialog-backdrop"
      role="presentation"
      @click.self="closeManualDialog"
    >
      <section
        role="dialog"
        aria-modal="true"
        aria-labelledby="manual-dialog-title"
        class="manual-dialog"
      >
        <h2 id="manual-dialog-title">
          确认人工准入
        </h2>
        <p>将允许寻源案例 {{ manualTarget.case_id }} 启动既有寻源工作流。此操作不代表已找到供应、已询价或已报价。</p>
        <div>
          <button
            type="button"
            :disabled="pendingAdmissionId !== null"
            @click="closeManualDialog"
          >
            取消
          </button>
          <button
            class="btn-primary"
            type="button"
            :disabled="pendingAdmissionId !== null"
            @click="confirmManualAdmission"
          >
            {{ pendingAdmissionId ? "正在准入…" : "确认准入" }}
          </button>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.sourcing-shell { overflow-y: auto; }
.policy-strip, .queue-section, .admission-row { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); }
.policy-strip { display: flex; justify-content: space-between; gap: var(--space3); padding: var(--space3) var(--space4); }
.policy-strip strong { color: var(--fact); }.policy-strip span { color: var(--text-secondary); }
.queue-section { display: grid; gap: var(--space2); padding: var(--space4); }
.queue-section > header { display: flex; justify-content: space-between; align-items: end; gap: var(--space3); padding-bottom: var(--space2); }
.queue-section > header > span { color: var(--text-secondary); }
.admission-row { display: grid; grid-template-columns: minmax(190px, .65fr) minmax(420px, 1.7fr) minmax(220px, .9fr) auto; gap: var(--space3); align-items: center; padding: var(--space3); }
.admission-main { display: grid; gap: 2px; min-width: 0; }.admission-main a { color: var(--text-primary); overflow-wrap: anywhere; }.admission-main small { color: var(--text-secondary); overflow-wrap: anywhere; }
.admission-row dl { display: grid; grid-template-columns: repeat(3, minmax(120px, 1fr)); gap: var(--space3); margin: 0; }
.admission-row dl > div { display: grid; gap: 2px; border: 0; padding: 0; }.admission-row dt { color: var(--text-secondary); font-size: 11px; }.admission-row dd { margin: 0; overflow-wrap: anywhere; }
.explanation { color: var(--text-secondary); }.blocked-row { border-color: var(--danger); background: var(--danger-soft); }
.case-row { display: flex; justify-content: space-between; gap: var(--space3); align-items: center; padding: var(--space3) var(--space4); border: 1px solid var(--border); border-radius: var(--radius); color: var(--text-primary); text-decoration: none; background: var(--surface); }
.case-row:hover { border-color: var(--action); }.case-row span { display: grid; gap: 2px; }.case-row small { color: var(--text-secondary); }
.case-actions { display: flex; flex-wrap: wrap; gap: var(--space2); }.case-actions a { color: var(--action); white-space: nowrap; }
.case-workbench-note { color: var(--text-secondary); }
.empty { padding: var(--space5); color: var(--text-secondary); text-align: center; }
.dialog-backdrop { position: fixed; inset: 0; z-index: 1000; display: grid; place-items: center; padding: var(--space4); background: rgba(16, 35, 35, .42); }
.manual-dialog { width: min(480px, 100%); display: grid; gap: var(--space3); padding: var(--space5); border: 1px solid var(--border); border-radius: 12px; background: var(--surface); box-shadow: 0 16px 48px rgba(16, 35, 35, .2); }
.manual-dialog > div { display: flex; justify-content: flex-end; gap: var(--space2); }
@media (max-width: 900px) {
  .admission-row { grid-template-columns: 1fr; }.policy-strip, .case-row { align-items: flex-start; flex-direction: column; }
  .admission-row dl { grid-template-columns: 1fr; }.admission-row button { width: 100%; }
}
</style>
