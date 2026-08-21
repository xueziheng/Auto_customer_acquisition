<script setup lang="ts">
/* global Response */
import { computed, inject, ref } from "vue";

import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";

type ApiClient = ReturnType<typeof createApiClient>;
type Proposal = components["schemas"]["ProposalView"];
type Confirmation = components["schemas"]["DiscoveryConfirmationResponse"];

const client = inject<ApiClient>("tradeos-api-client", apiClient);
const message = ref("");
const proposal = ref<Proposal | null>(null);
const confirmation = ref<Confirmation | null>(null);
const submitting = ref(false);
const deciding = ref(false);
const error = ref<string | null>(null);
const statusMessage = ref("输入指令后只会生成待确认提案，不会直接启动工作流。");

const fieldLabels: Record<string, string> = {
  objective: "总目标",
  discovery_objective: "发现目标",
  queries: "检索式",
  target_countries: "目标国家",
  excluded_countries: "排除国家",
  target_categories: "目标品类",
  excluded_categories: "排除品类",
  max_search_queries: "最多检索式",
  max_pages_per_query: "每个检索式最多页数",
  max_signals: "最多需求信号",
  max_hypotheses: "最多需求假设",
  minimum_confidence_tier: "最低置信档位",
  strategy_group: "策略组",
  campaign_id: "Campaign",
  role_hints: "联系人角色线索",
  assessment_ref: "正当利益评估引用",
};

const stateLabel = computed(() => {
  const value = proposal.value?.state;
  return (
    {
      pending_confirmation: "待确认",
      confirmed: "已确认",
      rejected: "已拒绝",
      expired: "已过期",
    }[value ?? ""] ?? value ?? ""
  );
});

const capKeys = [
  "max_search_queries",
  "max_pages_per_query",
  "max_signals",
  "max_hypotheses",
] as const;

const capRows = computed(() =>
  capKeys.map((key) => ({
    key,
    label: fieldLabels[key],
    value: proposal.value?.parsed_fields[key] || "未设置",
  })),
);

const scopeRows = computed(() => {
  if (!proposal.value) return [];
  return Object.entries(proposal.value.parsed_fields)
    .filter(([key]) => !capKeys.includes(key as (typeof capKeys)[number]))
    .map(([key, value]) => ({ key, label: fieldLabels[key] ?? key, value: displayValue(key, value) }));
});

function retryNotice(response: Response): string {
  const value = response.headers.get("retry-after");
  return value && /^\d+$/.test(value)
    ? `服务暂不可用，请在 ${value} 秒后重试`
    : "服务暂不可用，请稍后重试";
}

function safeError(response: Response): string {
  if (response.status === 403) return "当前身份无权创建或决定老板指令";
  if (response.status === 409) return "提案状态已经变化，请重新读取后再决定";
  if (response.status === 503) return retryNotice(response);
  return "请求未完成，请检查输入后重试";
}

function displayValue(key: string, value: string): string {
  if (!value) return "未设置";
  if (key !== "queries") return value;
  try {
    const parsed: unknown = JSON.parse(value);
    return Array.isArray(parsed) ? parsed.filter((item) => typeof item === "string").join("；") : value;
  } catch {
    return value;
  }
}

function resetDraft(): void {
  proposal.value = null;
  confirmation.value = null;
  error.value = null;
  statusMessage.value = "已清空当前提案；尚未启动任何工作流。";
}

async function loadProposal(proposalId: string): Promise<void> {
  const result = await client.GET("/commands/discovery-proposals/{proposal_id}", {
    params: { path: { proposal_id: proposalId } },
  });
  if (result.response.status === 200 && result.data) proposal.value = result.data;
}

async function createProposal(): Promise<void> {
  const raw = message.value.trim();
  if (!raw || submitting.value) return;
  submitting.value = true;
  proposal.value = null;
  confirmation.value = null;
  error.value = null;
  statusMessage.value = "正在把指令解释为不可变提案…";
  try {
    const result = await client.POST("/commands/discovery-proposals", {
      body: { message: raw },
    });
    if (result.response.status === 200 && result.data) {
      proposal.value = result.data;
      statusMessage.value = "提案已生成。请逐项核对，确认前不会启动工作流。";
      return;
    }
    error.value = safeError(result.response);
    statusMessage.value = "提案未生成。";
  } catch {
    error.value = "无法连接服务，请稍后重试";
    statusMessage.value = "提案未生成。";
  } finally {
    submitting.value = false;
  }
}

async function decide(action: "confirm" | "reject"): Promise<void> {
  const current = proposal.value;
  if (!current || current.state !== "pending_confirmation" || deciding.value) return;
  deciding.value = true;
  error.value = null;
  statusMessage.value = action === "confirm" ? "正在确认并启动受限工作流…" : "正在拒绝提案…";
  try {
    if (action === "confirm") {
      const result = await client.POST(
        "/commands/discovery-proposals/{proposal_id}/confirm",
        {
          params: { path: { proposal_id: current.proposal_id } },
        },
      );
      if (result.response.status === 200 && result.data) {
        confirmation.value = result.data;
        await loadProposal(current.proposal_id);
        statusMessage.value = "提案已确认；系统只会在页面列明的范围和上限内执行。";
        return;
      }
      error.value = safeError(result.response);
    } else {
      const result = await client.POST(
        "/commands/discovery-proposals/{proposal_id}/reject",
        {
          params: { path: { proposal_id: current.proposal_id } },
        },
      );
      if (result.response.status === 200) {
        await loadProposal(current.proposal_id);
        statusMessage.value = "提案已拒绝；没有启动工作流。";
        return;
      }
      error.value = safeError(result.response);
    }
    statusMessage.value = "决定未提交。";
  } catch {
    error.value = "无法连接服务，请稍后重试";
    statusMessage.value = "决定未提交。";
  } finally {
    deciding.value = false;
  }
}
</script>

<template>
  <div class="shell command-shell">
    <div class="page-head command-head">
      <div>
        <p class="eyebrow">
          BOSS COMMAND CENTER
        </p>
        <h1>指挥中心</h1>
      </div>
      <p class="head-copy">
        自然语言先转为可审阅提案；只有老板明确确认后，受限工作流才会启动。
      </p>
    </div>

    <section
      class="composer"
      aria-labelledby="command-composer-title"
    >
      <div class="composer-copy">
        <span class="step">01</span>
        <div>
          <h2 id="command-composer-title">
            描述这次需求发现任务
          </h2>
          <p>请写清国家、品类、排除项与数量上限。系统不会把这段文字直接当执行命令。</p>
        </div>
      </div>
      <form @submit.prevent="createProposal">
        <label for="boss-command">老板原始指令</label>
        <textarea
          id="boss-command"
          v-model="message"
          rows="4"
          placeholder="例如：寻找德国和荷兰的储能安装商需求，排除消费电子；最多 6 个检索式、每个 3 页、收集 80 条信号并形成 20 个假设。"
          :disabled="submitting || deciding"
        />
        <div class="composer-actions">
          <span>{{ message.trim().length }} 字</span>
          <button
            v-if="proposal"
            type="button"
            @click="resetDraft"
          >
            新建指令
          </button>
          <button
            class="btn-primary"
            type="submit"
            :disabled="!message.trim() || submitting || deciding"
          >
            {{ submitting ? "正在生成…" : "生成待确认提案" }}
          </button>
        </div>
      </form>
    </section>

    <div
      class="control-note"
      :class="{ danger: error }"
      role="status"
    >
      <strong>{{ error ? "未执行" : "执行闸门" }}</strong>
      <span>{{ error ?? statusMessage }}</span>
    </div>

    <section
      v-if="proposal"
      class="proposal"
      aria-labelledby="proposal-title"
    >
      <header class="proposal-head">
        <div>
          <span class="step">02</span>
          <div>
            <h2 id="proposal-title">
              逐项核对提案
            </h2>
            <p class="proposal-id">
              {{ proposal.proposal_id }}
            </p>
          </div>
        </div>
        <span
          class="proposal-state"
          :class="`state-${proposal.state}`"
        >{{ stateLabel }}</span>
      </header>

      <div class="comparison-grid">
        <article class="comparison-card raw-card">
          <span class="card-label">老板原话 · 事实记录</span>
          <p>{{ proposal.raw_text }}</p>
        </article>
        <article class="comparison-card interpretation-card">
          <span class="card-label">系统理解 · 待确认推断</span>
          <p>{{ proposal.interpretation_summary }}</p>
        </article>
        <article class="comparison-card behavior-card">
          <span class="card-label">确认后的行为变化</span>
          <ul>
            <li
              v-for="change in proposal.expected_behavior_changes"
              :key="change"
            >
              {{ change }}
            </li>
          </ul>
        </article>
      </div>

      <div class="proposal-details">
        <article class="scope-card">
          <h3>执行范围</h3>
          <dl>
            <div
              v-for="row in scopeRows"
              :key="row.key"
            >
              <dt>{{ row.label }}</dt>
              <dd>{{ row.value }}</dd>
            </div>
          </dl>
        </article>
        <article class="caps-card">
          <div>
            <h3>硬上限</h3>
            <span>工作流不得越过</span>
          </div>
          <dl>
            <div
              v-for="row in capRows"
              :key="row.key"
            >
              <dt>{{ row.label }}</dt>
              <dd>{{ row.value }}</dd>
            </div>
          </dl>
        </article>
      </div>

      <footer class="decision-bar">
        <div>
          <strong>{{ proposal.state === "pending_confirmation" ? "确认后才会执行" : `提案${stateLabel}` }}</strong>
          <span v-if="proposal.decided_by_name">决定人：{{ proposal.decided_by_name }}</span>
          <span v-else>未确认提案不会触发任何发现任务</span>
        </div>
        <div
          v-if="proposal.state === 'pending_confirmation'"
          class="decision-actions"
        >
          <button
            type="button"
            :disabled="deciding"
            @click="decide('reject')"
          >
            拒绝，不执行
          </button>
          <button
            class="btn-primary"
            type="button"
            :disabled="deciding"
            @click="decide('confirm')"
          >
            {{ deciding ? "正在提交…" : "确认并启动" }}
          </button>
        </div>
      </footer>
    </section>

    <section
      v-if="confirmation"
      class="run-receipt"
      aria-label="工作流启动回执"
    >
      <div>
        <span class="receipt-mark">✓</span>
        <div>
          <h2>受限工作流已启动</h2>
          <p>Run {{ confirmation.run_id }} · Directive {{ confirmation.directive_id }}</p>
        </div>
      </div>
      <RouterLink to="/demand">
        查看需求雷达 →
      </RouterLink>
    </section>
  </div>
</template>

<style scoped>
.command-shell { overflow: auto; }
.command-head { justify-content: space-between; padding-top: var(--space3); }
.command-head > div { display: flex; align-items: baseline; gap: var(--space3); }
.eyebrow { color: var(--fact); font-size: 11px; font-weight: 800; letter-spacing: .16em; }
.head-copy { color: var(--text-secondary); max-width: 620px; }
.composer, .proposal, .run-receipt { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; }
.composer { display: grid; grid-template-columns: minmax(260px, .7fr) minmax(420px, 1.3fr); gap: var(--space5); padding: var(--space5); }
.composer-copy, .proposal-head > div { display: flex; gap: var(--space3); align-items: flex-start; }
.step { display: inline-grid; place-items: center; flex: 0 0 30px; height: 30px; border-radius: 50%; background: var(--fact-soft); color: var(--fact); font-size: 12px; font-weight: 800; }
h2 { font-size: 16px; }
.composer-copy p, .proposal-head p { color: var(--text-secondary); margin-top: var(--space1); }
form label { display: block; font-size: 12px; font-weight: 700; margin-bottom: var(--space2); }
textarea { width: 100%; resize: vertical; border: 1px solid var(--border); border-radius: var(--radius); padding: var(--space3); color: var(--text-primary); background: #fbfcfc; }
textarea:focus { border-color: var(--action); outline: 3px solid rgba(21, 94, 239, .14); }
.composer-actions { display: flex; justify-content: flex-end; align-items: center; gap: var(--space2); margin-top: var(--space3); }
.composer-actions span { color: var(--text-secondary); font-size: 12px; margin-right: auto; }
.control-note { display: flex; gap: var(--space3); align-items: center; border-left: 4px solid var(--fact); background: var(--fact-soft); color: var(--fact); padding: var(--space3) var(--space4); }
.control-note.danger { border-color: var(--danger); background: var(--danger-soft); color: var(--danger); }
.control-note strong { white-space: nowrap; }
.proposal { padding: var(--space5); }
.proposal-head { display: flex; justify-content: space-between; gap: var(--space4); align-items: flex-start; margin-bottom: var(--space4); }
.proposal-id { font-family: ui-monospace, monospace; font-size: 11px; }
.proposal-state { border: 1px solid var(--border); border-radius: 999px; padding: 4px 10px; font-size: 12px; font-weight: 700; }
.state-pending_confirmation { color: var(--warning); background: var(--warning-soft); border-color: var(--warning); }
.state-confirmed { color: var(--fact); background: var(--fact-soft); border-color: var(--fact); }
.state-rejected, .state-expired { color: var(--danger); background: var(--danger-soft); border-color: var(--danger); }
.comparison-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: var(--space3); }
.comparison-card { min-height: 150px; border: 1px solid var(--border); border-top-width: 4px; border-radius: var(--radius); padding: var(--space4); }
.raw-card { border-top-color: var(--fact); }
.interpretation-card { border-top-color: var(--inference); background: var(--inference-soft); }
.behavior-card { border-top-color: var(--action); }
.card-label { display: block; color: var(--text-secondary); font-size: 11px; font-weight: 800; letter-spacing: .06em; margin-bottom: var(--space3); }
.comparison-card p { white-space: pre-wrap; }
.comparison-card ul { padding-left: 18px; display: grid; gap: var(--space2); }
.proposal-details { display: grid; grid-template-columns: 1.4fr .6fr; gap: var(--space3); margin-top: var(--space3); }
.scope-card, .caps-card { border: 1px solid var(--border); border-radius: var(--radius); padding: var(--space4); }
.caps-card { border-color: var(--warning); background: #fffbeb; }
.caps-card > div { display: flex; align-items: baseline; justify-content: space-between; gap: var(--space2); }
.caps-card > div span { color: var(--warning); font-size: 11px; font-weight: 700; }
dl { display: grid; gap: var(--space2); margin-top: var(--space3); }
dl > div { display: grid; grid-template-columns: minmax(120px, .4fr) 1fr; gap: var(--space3); border-top: 1px solid rgba(82, 99, 99, .18); padding-top: var(--space2); }
dt { color: var(--text-secondary); font-size: 12px; }
dd { overflow-wrap: anywhere; }
.caps-card dd { font-size: 18px; font-weight: 800; color: var(--warning); text-align: right; }
.decision-bar { display: flex; justify-content: space-between; align-items: center; gap: var(--space4); border-top: 1px solid var(--border); margin-top: var(--space4); padding-top: var(--space4); }
.decision-bar > div:first-child { display: grid; }
.decision-bar span { color: var(--text-secondary); font-size: 12px; }
.decision-actions { display: flex; gap: var(--space2); }
.run-receipt { display: flex; justify-content: space-between; align-items: center; padding: var(--space4) var(--space5); border-color: var(--fact); }
.run-receipt > div { display: flex; gap: var(--space3); align-items: center; }
.receipt-mark { display: grid; place-items: center; width: 32px; height: 32px; border-radius: 50%; background: var(--fact); color: white; font-weight: 800; }
.run-receipt p { color: var(--text-secondary); font-family: ui-monospace, monospace; font-size: 11px; }
.run-receipt a { color: var(--action); font-weight: 700; text-decoration: none; }
@media (max-width: 900px) {
  .composer, .comparison-grid, .proposal-details { grid-template-columns: 1fr; }
  .decision-bar, .run-receipt { align-items: flex-start; flex-direction: column; }
}
</style>
