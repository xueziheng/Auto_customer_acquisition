<script setup lang="ts">
import { codeLabel } from "../../components/displayLabels";
/* global URL, window */
import { computed, inject, onMounted, ref, watch } from "vue";
import { useRoute } from "vue-router";

import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";
import { useQuoteRequestScope } from "../costing-quotes/quote-request-scope";

type ApiClient = ReturnType<typeof createApiClient>;
type Approval = components["schemas"]["ApprovalView"];

const client = inject<ApiClient>("tradeos-api-client", apiClient);
const route = useRoute();
const approvals = ref<Approval[]>([]);
const selected = ref<Approval | null>(null);
const loading = ref(true);
const detailLoading = ref(false);
const deciding = ref(false);
const error = ref<string | null>(null);
const rejectionReason = ref("");
const selectedId = ref("");

function queryApprovalId(): string | null {
  const value = route.query.approval_id;
  return typeof value === "string" && value.length > 0 ? value : null;
}

const pageGate = useQuoteRequestScope(client, () => [route.query.approval_id], () => {
  approvals.value = []; selected.value = null; selectedId.value = ""; rejectionReason.value = "";
  loading.value = false; detailLoading.value = false; deciding.value = false; error.value = null;
});
const detailGate = useQuoteRequestScope(client, () => [selectedId.value], () => {});
const actionGate = useQuoteRequestScope(client, () => [selectedId.value, rejectionReason.value], () => {});

const expiryLabel = computed(() => {
  const seconds = selected.value?.seconds_until_expiry;
  if (seconds === null || seconds === undefined) return "";
  if (seconds <= 0) return "已过期";
  if (seconds < 3600) return `${Math.ceil(seconds / 60)} 分钟后过期`;
  if (seconds < 86400) return `${Math.ceil(seconds / 3600)} 小时后过期`;
  return `${Math.ceil(seconds / 86400)} 天后过期`;
});

function safeSourceUrl(value: string): string | null {
  try {
    const url = new URL(value, window.location.origin);
    if (url.origin === window.location.origin && value.startsWith("/")) return value;
    return url.protocol === "https:" || url.protocol === "http:" ? url.toString() : null;
  } catch {
    return null;
  }
}

function formatDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

function safeError(status: number): string {
  if (status === 403) return "当前身份没有审批权限";
  if (status === 409) return "审批已被他人决定，请刷新";
  if (status === 503) return "审批服务暂不可用";
  return "请求未完成，请稍后重试";
}

async function loadApprovals(): Promise<void> {
  const op = pageGate.begin("list"); if (!op?.valid()) return;
  const requestedApprovalId = queryApprovalId();
  loading.value = true;
  error.value = null;
  try {
    const result = await client.GET("/approvals/pending", {
      params: { query: { limit: 100 } },
      signal: op.signal,
    });
    if (!op.valid()) return;
    if (result.response.status !== 200 || !result.data) {
      error.value = safeError(result.response.status);
      if (requestedApprovalId) await loadDetail(requestedApprovalId);
      return;
    }
    approvals.value = result.data;
    if (requestedApprovalId) {
      await loadDetail(requestedApprovalId);
      return;
    }
    const current = selectedId.value;
    const retained = current && result.data.some((item) => item.approval_id === current)
      ? current
      : result.data[0]?.approval_id;
    if (retained) await loadDetail(retained);
    else selected.value = null;
  } catch {
    if (op.valid()) error.value = "无法连接服务，请稍后重试";
  } finally {
    if (op.valid()) loading.value = false;
  }
}

async function loadDetail(approvalId: string): Promise<void> {
  selectedId.value = approvalId;
  selected.value = null;
  deciding.value = false;
  const op = detailGate.begin("detail"); if (!op?.valid()) return;
  detailLoading.value = true;
  error.value = null;
  rejectionReason.value = "";
  try {
    const result = await client.GET("/approvals/{approval_id}", {
      params: { path: { approval_id: approvalId } },
      signal: op.signal,
    });
    if (!op.valid()) return;
    if (result.response.status === 200 && result.data) selected.value = result.data;
    else error.value = safeError(result.response.status);
  } catch {
    if (op.valid()) error.value = "审批详情加载失败";
  } finally {
    if (op.valid()) detailLoading.value = false;
  }
}

async function decide(decision: "approve" | "reject"): Promise<void> {
  if (!selected.value || deciding.value || !selected.value.can_current_user_decide) return;
  if (decision === "reject" && !rejectionReason.value.trim()) {
    error.value = "否决必须填写具体原因，便于提议人修正后重提";
    return;
  }
  deciding.value = true;
  const op = actionGate.begin("decide"); if (!op?.valid()) { deciding.value = false; return; }
  error.value = null;
  try {
    const result = await client.POST("/approvals/{approval_id}/decide", {
      params: { path: { approval_id: selected.value.approval_id } },
      body: {
        decision,
        reason: decision === "reject" ? rejectionReason.value.trim() : undefined,
      },
      signal: op.signal,
    });
    if (!op.valid()) return;
    if (result.response.status !== 200) {
      error.value = safeError(result.response.status);
      return;
    }
    await loadApprovals();
  } catch {
    if (op.valid()) error.value = "决定结果未知，请先刷新核对审批状态";
  } finally {
    if (op.valid()) deciding.value = false;
  }
}

watch(() => route.query.approval_id, () => {
  selected.value = null;
  selectedId.value = "";
  rejectionReason.value = "";
  detailLoading.value = false;
  deciding.value = false;
  // 深链使旧列表请求失效；由新请求接管 loading，并重新读取精确审批。
  void loadApprovals();
}, { flush: "sync" });
onMounted(() => void loadApprovals());
</script>

<template>
  <div class="shell approval-shell">
    <div class="page-head approval-head">
      <div>
        <p class="eyebrow">
          人工审批
        </p><h1>审批中心</h1>
      </div>
      <button
        type="button"
        :disabled="loading"
        @click="loadApprovals"
      >
        {{ loading ? "加载中…" : "刷新待办" }}
      </button>
    </div>

    <div class="approval-rule">
      <strong>一分钟内完成决定</strong><span>审批包必须自带完整变更、理由、证据和影响；系统不提供默认批准、强制覆盖或过期补批。报价批准不发送；金额、比例、前版与汇率口径均为服务端投影，页面不重算。</span>
    </div>
    <div
      v-if="error"
      class="safe-banner danger"
      role="alert"
    >
      {{ error }}
    </div>

    <section class="approval-layout">
      <aside class="approval-list">
        <header><h2>我的待审批</h2><span>{{ approvals.length }}</span></header>
        <div
          v-if="loading"
          class="empty"
        >
          正在按过期时间排序…
        </div>
        <button
          v-for="approval in approvals"
          v-else
          :key="approval.approval_id"
          type="button"
          :class="{ active: selected?.approval_id === approval.approval_id }"
          @click="loadDetail(approval.approval_id)"
        >
          <span class="type">{{ approval.type_label }}</span><strong>{{ approval.title }}</strong><small>{{ formatDate(approval.expires_at) }} 过期</small>
        </button>
        <div
          v-if="!loading && !approvals.length"
          class="empty"
        >
          当前没有可由你决定的审批
        </div>
      </aside>

      <main
        v-if="selected"
        class="approval-packet"
        :aria-busy="detailLoading ? 'true' : undefined"
      >
        <header>
          <div><span class="packet-type">{{ selected.type_label }}</span><h2>{{ selected.title }}</h2><p>{{ selected.approval_id }}</p></div><span
            class="expiry"
            :class="{ urgent: (selected.seconds_until_expiry ?? 0) < 86400 }"
          >{{ expiryLabel }}</span>
        </header>

        <section class="why">
          <span>为什么提议</span><p>{{ selected.reason }}</p>
        </section>

        <section class="change-section">
          <h3>提议的精确变更</h3><dl>
            <div
              v-for="(value, key) in selected.proposed_change_display"
              :key="key"
            >
              <dt>{{ codeLabel(key) }}</dt><dd>{{ value }}</dd>
            </div>
          </dl>
        </section>

        <section class="blast-section">
          <h3>影响范围</h3><ul>
            <li
              v-for="entity in selected.affected_entities"
              :key="entity"
            >
              {{ entity }}
            </li>
          </ul><div class="outcomes">
            <article class="approved-outcome">
              <span>如果批准</span><p>{{ selected.if_approved }}</p>
            </article><article class="rejected-outcome">
              <span>如果否决</span><p>{{ selected.if_rejected }}</p>
            </article>
          </div><p class="reversible">
            {{ selected.reversible ? "该变更可通过后续受控操作撤销" : "该变更不可逆，请谨慎决定" }}
          </p>
        </section>

        <section class="evidence-section">
          <h3>证据与归属</h3><dl><div><dt>提议人</dt><dd>{{ selected.proposed_by ?? "智能助手运行记录" }}</dd></div><div><dt>业务负责人</dt><dd>{{ selected.owner_name ?? "未指定" }}</dd></div><div><dt>提交时间</dt><dd>{{ formatDate(selected.created_at) }}</dd></div></dl><ul v-if="selected.evidence_links?.length">
            <li
              v-for="link in selected.evidence_links ?? []"
              :key="link"
            >
              <a
                v-if="safeSourceUrl(link)"
                :href="safeSourceUrl(link) ?? undefined"
                target="_blank"
                rel="noopener noreferrer"
              >查看证据 ↗</a><span v-else>证据记录：{{ link }}</span>
            </li>
          </ul><p v-else>
            此审批包没有附加外部证据；决定依据为上方不可变边界快照。
          </p>
        </section>

        <footer class="decision-panel">
          <div v-if="selected.can_current_user_decide">
            <label>否决原因<textarea
              v-model="rejectionReason"
              :disabled="deciding"
              rows="2"
              placeholder="仅否决时必填；请写清需要怎样修改"
            /></label><div>
              <button
                class="reject-button"
                type="button"
                :disabled="deciding"
                @click="decide('reject')"
              >
                否决并退回
              </button><button
                class="btn-primary"
                type="button"
                :disabled="deciding"
                @click="decide('approve')"
              >
                {{ deciding ? "提交中…" : "批准此精确变更" }}
              </button>
            </div>
          </div><div
            v-else
            class="cannot-decide"
          >
            <strong>当前身份不能决定此审批</strong><span>可能原因：你是提议人/负责人、审批已过期或状态已经改变。后端会做最终裁决。</span>
          </div>
        </footer>
      </main>
      <div
        v-else
        class="approval-packet empty"
      >
        选择一个审批包查看完整内容
      </div>
    </section>
  </div>
</template>

<style scoped>
.approval-shell { overflow: auto; }
.approval-head { justify-content: space-between; padding-top: var(--space3); }
.approval-head > div { display: flex; align-items: baseline; gap: var(--space3); }
.eyebrow { color: #6941c6; font-size: 11px; font-weight: 800; letter-spacing: .16em; }
.approval-rule { display: flex; gap: var(--space3); border-left: 4px solid #6941c6; background: #f4f0ff; padding: var(--space3) var(--space4); }
.approval-rule span { color: var(--text-secondary); }
.approval-layout { display: grid; grid-template-columns: 310px 1fr; gap: var(--space3); min-height: 560px; }
.approval-list, .approval-packet { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; }
.approval-list { padding: var(--space3); }
.approval-list header { display: flex; justify-content: space-between; align-items: center; padding: var(--space2); }
.approval-list header span { display: grid; place-items: center; width: 26px; height: 26px; border-radius: 50%; background: #6941c6; color: white; font-weight: 800; }
.approval-list button { width: 100%; display: grid; text-align: left; gap: var(--space1); margin-top: var(--space2); padding: var(--space3); border-left: 3px solid transparent; }
.approval-list button.active { border-left-color: #6941c6; background: #f4f0ff; }
.approval-list .type { color: #6941c6; font-size: 10px; font-weight: 800; }
.approval-list small { color: var(--text-secondary); }
.approval-packet { padding: var(--space5); }
.approval-packet > header { display: flex; justify-content: space-between; gap: var(--space3); }
.packet-type { color: #6941c6; font-size: 11px; font-weight: 800; letter-spacing: .08em; }
.approval-packet > header p { color: var(--text-secondary); font-family: ui-monospace, monospace; font-size: 11px; }
.expiry { align-self: flex-start; border-radius: 999px; background: var(--fact-soft); color: var(--fact); padding: 4px 9px; font-size: 11px; font-weight: 800; }
.expiry.urgent { background: var(--warning-soft); color: var(--warning); }
.why, .change-section, .blast-section, .evidence-section { border-top: 1px solid var(--border); margin-top: var(--space4); padding-top: var(--space4); }
.why > span { color: var(--text-secondary); font-size: 11px; font-weight: 800; }
.why p { font-size: 16px; margin-top: var(--space2); }
h3 { font-size: 14px; }
dl { display: grid; gap: var(--space2); margin-top: var(--space3); }
dl div { display: grid; grid-template-columns: 170px 1fr; gap: var(--space3); border-top: 1px solid #edf0ef; padding-top: var(--space2); }
dt { color: var(--text-secondary); font-size: 12px; }
dd { overflow-wrap: anywhere; white-space: pre-wrap; }
.blast-section > ul, .evidence-section ul { padding-left: 20px; margin-top: var(--space2); }
.outcomes { display: grid; grid-template-columns: repeat(2, 1fr); gap: var(--space3); margin-top: var(--space3); }
.outcomes article { border-radius: var(--radius); padding: var(--space4); }
.outcomes span { display: block; font-size: 11px; font-weight: 800; margin-bottom: var(--space2); }
.approved-outcome { background: var(--fact-soft); border: 1px solid var(--fact); }
.approved-outcome span { color: var(--fact); }
.rejected-outcome { background: var(--danger-soft); border: 1px solid var(--danger); }
.rejected-outcome span { color: var(--danger); }
.reversible { color: var(--text-secondary); font-size: 11px; margin-top: var(--space2); }
.evidence-section > p { color: var(--text-secondary); margin-top: var(--space2); }
a { color: var(--action); font-weight: 700; text-decoration: none; }
.decision-panel { border-top: 2px solid #6941c6; margin-top: var(--space5); padding-top: var(--space4); }
.decision-panel > div:first-child { display: grid; grid-template-columns: 1fr auto; gap: var(--space3); align-items: end; }
.decision-panel label { display: grid; gap: var(--space1); font-size: 12px; font-weight: 700; }
textarea { resize: vertical; border: 1px solid var(--border); border-radius: var(--radius-sm); padding: var(--space2); }
.decision-panel label + div { display: flex; gap: var(--space2); }
.reject-button { color: var(--danger); border-color: var(--danger); }
.cannot-decide { display: grid; color: var(--warning); background: var(--warning-soft); padding: var(--space3); }
.cannot-decide span { font-size: 12px; }
.empty { display: grid; place-items: center; min-height: 120px; color: var(--text-secondary); }
@media (max-width: 900px) {
  .approval-shell > * { flex-shrink: 0; }
  .approval-layout {
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: auto auto;
    min-height: 0;
    align-content: start;
  }
  .approval-list { max-height: 300px; overflow: auto; }
  .outcomes, .decision-panel > div:first-child { grid-template-columns: 1fr; }
}
</style>
