<script setup lang="ts">
import { codeLabel } from "../../components/displayLabels";
/* global Response */
import { computed, inject, onMounted, ref, watch } from "vue";

import { useRoute } from "vue-router";
import { useQuoteRequestScope } from "../costing-quotes/quote-request-scope";
import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";
import AgentConversation from "./AgentConversation.vue";
import ResearchAccessCard from "../../components/ResearchAccessCard.vue";
import { laneLabel, sourceChannelLabel, stopLabel } from "../../components/researchLabels";

type ApiClient = ReturnType<typeof createApiClient>;
type Proposal = components["schemas"]["DiscoveryProposalView"];
type Confirmation = components["schemas"]["DiscoveryConfirmationResponse"];
type AdmissionPolicy = components["schemas"]["SourcingAdmissionPolicyView"];
type AdmissionProposal = components["schemas"]["ProposalView"];
type AdmissionConfirmation = components["schemas"]["SourcingAdmissionConfirmationResponse"];

const client = inject<ApiClient>("tradeos-api-client", apiClient);
const route=useRoute();
const assistantAvailable = ref(false);
const message = ref("");
const proposal = ref<Proposal | null>(null);
const confirmation = ref<Confirmation | null>(null);
const execution = ref<components["schemas"]["DiscoveryExecutionView"] | null>(null);
const receiptRunId = computed(() => confirmation.value?.run_id ?? execution.value?.run_id);
const submitting = ref(false);
const deciding = ref(false);
const decisionUncertain = ref(false);
const error = ref<string | null>(null);
const statusMessage = ref("输入指令后只会生成待确认提案，不会直接启动工作流。");
const admissionPolicy = ref<AdmissionPolicy>({ status: "policy_status_unknown" });
const admissionPolicyVisible = ref(true);
const admissionMessage = ref("按需求簇排序，每轮最多启动 3 个寻源案例");
const admissionEnabled = ref(true);
const admissionBatchLimit = ref(3);
const admissionProposal = ref<AdmissionProposal | null>(null);
const admissionConfirmation = ref<AdmissionConfirmation | null>(null);
const admissionSubmitting = ref(false);
const admissionConfirming = ref(false);
const admissionError = ref<string | null>(null);
const admissionConfirmKey = ref<string | null>(null);

function clearPage(): void {
 message.value=""; proposal.value=null;confirmation.value=null;execution.value=null;
 submitting.value=false;deciding.value=false;decisionUncertain.value=false;error.value=null;
 admissionProposal.value=null;admissionConfirmation.value=null;admissionConfirmKey.value=null;
 admissionSubmitting.value=false;admissionConfirming.value=false;admissionError.value=null;
 admissionPolicy.value={status:"policy_status_unknown"};statusMessage.value="请以当前身份读取或创建提案";
}
const gate=useQuoteRequestScope(client,()=>[route.fullPath],()=>{clearPage();globalThis.queueMicrotask(()=>void loadPage());});
async function loadPage():Promise<void>{
 const op=gate.begin("page");if(!op?.valid())return;
 await loadAdmissionPolicy();
 if(!op.valid())return;
 const id=typeof route.query.proposal_id==="string"?route.query.proposal_id:null;
 if(id)await loadProposal(id);
}
watch(()=>route.fullPath,()=>{clearPage();void loadPage();},{flush:"sync"});

const admissionPolicyLabel = computed(() => ({
  automatic_admission_disabled: "已关闭",
  enabled: "已启用",
  policy_not_configured: "未配置",
  policy_status_unknown: "状态未知",
}[admissionPolicy.value.status]));

function admissionSafeError(status: number): string {
  if (status === 403) return "当前身份无权配置寻源准入策略";
  if (status === 409) return "提案基于的老板指令已变化，请重新创建提案";
  if (status === 503) return "寻源准入策略服务暂不可用，请稍后重试";
  return "寻源准入策略请求未完成，请检查配置后重试";
}

async function loadAdmissionPolicy(): Promise<void> {
  const op=gate.begin("loadAdmissionPolicy");if(!op?.valid())return ;
  try {
    const result = await client.GET("/sourcing-admissions", {
      params: { query: { limit: 1, state: "waiting" } },
    });
    if(!op.valid())return ;
    if (result.response.status === 200 && result.data) {
      admissionPolicy.value = result.data.policy;
      admissionPolicyVisible.value = true;
      return;
    }
    if(!op.valid())return ;
    if (result.response.status === 403) {
      admissionPolicyVisible.value = false;
      return;
    }
  } catch {
    if(!op.valid())return ; /* 策略读取失败与“未配置”不同。 */ }
  if(op.valid()) admissionPolicy.value = { status: "policy_status_unknown" };
}

async function createAdmissionProposal(): Promise<void> {
  const raw = admissionMessage.value.trim();
  if (!raw || admissionSubmitting.value || !Number.isInteger(admissionBatchLimit.value)) return;
  const op=gate.begin("createAdmissionProposal");if(!op?.valid())return ;
  admissionSubmitting.value = true;
  admissionError.value = null;
  admissionProposal.value = null;
  admissionConfirmation.value = null;
  admissionConfirmKey.value = null;
  try {
    const result = await client.POST("/commands/sourcing-admission-proposals", {
      body: {
        automatic_admission_enabled: admissionEnabled.value,
        batch_limit: admissionBatchLimit.value,
        message: raw,
        mode: "cluster_ranked",
      },
    });
    if(!op.valid())return ;
    if (result.response.status === 200 && result.data) {
      admissionProposal.value = result.data;
      return;
    }
    if(!op.valid())return ;
    if (result.response.status === 403) admissionPolicyVisible.value = false;
    admissionError.value = admissionSafeError(result.response.status);
  } catch {
    if(!op.valid())return ;
    admissionError.value = "无法连接寻源准入策略服务";
  } finally {
    if(op.valid()) admissionSubmitting.value = false;
  }
}

async function confirmAdmissionProposal(): Promise<void> {
  const current = admissionProposal.value;
  if (!current || current.state !== "pending_confirmation" || admissionConfirming.value) return;
  const op=gate.begin("confirmAdmissionProposal");if(!op?.valid())return ;
  admissionConfirming.value = true;
  admissionError.value = null;
  admissionConfirmKey.value ??= globalThis.crypto.randomUUID();
  try {
    const result = await client.POST(
      "/commands/sourcing-admission-proposals/{proposal_id}/confirm",
      {
        params: {
          header: { "Idempotency-Key": admissionConfirmKey.value },
          path: { proposal_id: current.proposal_id },
        },
      },
    );
    if(!op.valid())return ;
    if (result.response.status === 200 && result.data) {
      admissionConfirmation.value = result.data;
      admissionProposal.value = { ...current, state: "confirmed" };
      admissionPolicy.value = {
        automatic_admission_enabled: result.data.automatic_admission_enabled,
        batch_limit: result.data.batch_limit,
        directive_id: result.data.directive_id,
        directive_version: result.data.directive_version,
        status: result.data.automatic_admission_enabled ? "enabled" : "automatic_admission_disabled",
      };
      admissionConfirmKey.value = null;
      return;
    }
    admissionError.value = admissionSafeError(result.response.status);
  } catch {
    if(!op.valid())return ;
    admissionError.value = "无法连接寻源准入策略服务";
  } finally {
    if(op.valid()) admissionConfirming.value = false;
  }
}

const fieldLabels: Record<string, string> = {
  objective: "总目标",
  discovery_objective: "发现目标",
  queries: "检索式",
  target_countries: "目标国家",
  excluded_countries: "排除国家",
  target_categories: "目标品类",
  excluded_categories: "排除品类",
  max_search_queries: "最多检索式",
  max_pages_read: "总页面读取上限",
  execution_mode: "执行模式",
  max_signals: "最多需求信号",
  max_hypotheses: "最多需求假设",
  minimum_confidence_tier: "最低置信档位",
  strategy_group: "策略组",
  campaign_id: "活动",
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
  "max_pages_read",
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
    .filter(([key]) => proposal.value?.execution_mode !== "research_only" || !["campaign_id", "role_hints", "assessment_ref"].includes(key))
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
  if (key === "execution_mode") return value === "research_only" ? "只研究" : "触达准备（原流程）";
  if (key === "minimum_confidence_tier") return codeLabel(value);
  if (key !== "queries") return value;
  try {
    const parsed: unknown = JSON.parse(value);
    return Array.isArray(parsed) ? parsed.map((item: unknown) => {
      if (typeof item === "string") return item;
      if (item && typeof item === "object" && "query" in item && typeof item.query === "string") {
        return `${"discovery_lane" in item && typeof item.discovery_lane === "string" ? laneLabel(item.discovery_lane) : "历史查询"}：${item.query}`;
      }
      return "查询待核验";
    }).join("；") : value;
  } catch {
    return value;
  }
}

function resetDraft(): void {
  gate.invalidate();
  submitting.value=false;deciding.value=false;
  proposal.value = null;
  confirmation.value = null;
  execution.value = null;
  error.value = null;
  decisionUncertain.value = false;
  statusMessage.value = "已清空当前提案；新指令仍需确认后执行。";
}

async function loadProposal(proposalId: string): Promise<boolean> {
  const op=gate.begin("loadProposal");if(!op?.valid())return false;
  try {
    const result = await client.GET("/commands/discovery-proposals/{proposal_id}", {
      params: { path: { proposal_id: proposalId } },
    });
    if(!op.valid())return false;
    if (result.response.status === 200 && result.data) {
      proposal.value = result.data;
      decisionUncertain.value = false;
      if (result.data.state === "confirmed") await loadExecution(proposalId);
      return op.valid();
    }
    if ([403,404].includes(result.response.status)) { proposal.value=null; confirmation.value=null; execution.value=null; error.value="提案不存在或当前身份无权读取"; }
  } catch {
    if(!op.valid())return false; /* 读取失败不撤销已经收到的 POST 成功回执。 */ }
  return false;
}

async function loadExecution(proposalId: string): Promise<void> {
  const op=gate.begin("loadExecution");if(!op?.valid())return ;
  try {
    const result = await client.GET("/commands/discovery-proposals/{proposal_id}/execution", {
      params: { path: { proposal_id: proposalId } },
    });
    if(!op.valid())return;
    execution.value = result.response.status === 200 && result.data
      ? result.data : { state: "unknown", can_resume: false };
  } catch {
    if(!op.valid())return ;
    execution.value = { state: "unknown", can_resume: false };
  }
}

async function refreshProposal(): Promise<void> {
  if (!proposal.value || deciding.value) return;
  const op=gate.begin("refreshProposal");if(!op?.valid())return ;
  deciding.value = true;
  error.value = null;
  const refreshed = await loadProposal(proposal.value.proposal_id);
  if(!op.valid())return;
  statusMessage.value = refreshed ? "已重新读取提案状态。" : "状态刷新未完成；保留最近已知回执，请稍后只读刷新。";
  deciding.value = false;
}

function markDecisionUncertain(): void {
  decisionUncertain.value = true;
  error.value = "提交结果待核实；请只读刷新提案状态，不要重复提交。";
  statusMessage.value = "提交结果待核实。";
}

async function createProposal(): Promise<void> {
  const raw = message.value.trim();
  if (!raw || submitting.value) return;
  const op=gate.begin("createProposal");if(!op?.valid())return ;
  submitting.value = true;
  proposal.value = null;
  confirmation.value = null;
  execution.value = null;
  decisionUncertain.value = false;
  error.value = null;
  statusMessage.value = "正在把指令解释为不可变提案…";
  try {
    const result = await client.POST("/commands/discovery-proposals", {
      body: { message: raw },
    });
    if(!op.valid())return ;
    if (result.response.status === 200 && result.data) {
      proposal.value = result.data;
      statusMessage.value = "提案已生成。请逐项核对，确认前不会启动工作流。";
      return;
    }
    error.value = safeError(result.response);
    statusMessage.value = "提案未生成。";
  } catch {
    if(!op.valid())return ;
    error.value = "无法连接服务，请稍后重试";
    statusMessage.value = "提案未生成。";
  } finally {
    if(op.valid()) submitting.value = false;
  }
}

async function decide(action: "confirm" | "reject"): Promise<void> {
  const current = proposal.value;
  if (!current || deciding.value || decisionUncertain.value) return;
  if (current.state !== "pending_confirmation" && !(current.state === "confirmed" && action === "confirm" && execution.value?.can_resume && !receiptRunId.value)) return;
  if (action === "confirm" && current.can_confirm !== true) return;
  const op=gate.begin("decide");if(!op?.valid())return ;
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
      if(!op.valid())return ;
    if (result.response.status === 200 && result.data) {
        confirmation.value = result.data;
        execution.value = { state: "started", run_id: result.data.run_id, can_resume: false };
        proposal.value = { ...current, state: "confirmed", can_confirm: false };
        const refreshed = await loadProposal(current.proposal_id);
        if(!op.valid())return;
  statusMessage.value = refreshed
          ? "提案已确认；系统只会在页面列明的范围和上限内执行。"
          : "确认已成功，运行记录回执已保留；详情刷新未完成，可只读刷新提案状态。";
        return;
      }
      if(!op.valid())return ;
    if (result.response.status >= 500 || result.response.status === 200) {
        markDecisionUncertain();
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
      if(!op.valid())return ;
    if (result.response.status === 200) {
        proposal.value = { ...current, state: "rejected", can_confirm: false };
        const refreshed = await loadProposal(current.proposal_id);
        if(!op.valid())return;
  statusMessage.value = refreshed
          ? "提案已拒绝；没有启动工作流。"
          : "拒绝已成功；详情刷新未完成，可只读刷新提案状态。";
        return;
      }
      if(!op.valid())return ;
    if (result.response.status >= 500) {
        markDecisionUncertain();
        return;
      }
      error.value = safeError(result.response);
    }
    statusMessage.value = "决定未提交。";
  } catch {
    if(!op.valid())return ;
    markDecisionUncertain();
  } finally {
    if(op.valid()) deciding.value = false;
  }
}

onMounted(() => void loadPage());
</script>

<template>
  <div class="shell command-shell">
    <div class="page-head command-head">
      <div>
        <p class="eyebrow">
          老板指令
        </p>
        <h1>指挥中心</h1>
      </div>
      <p class="head-copy">
        自然语言先转为可审阅提案；只有老板明确确认后，受限工作流才会启动。
      </p>
    </div>

    <AgentConversation @available="value=>assistantAvailable=value" />
    <section
      v-if="!assistantAvailable"
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
          placeholder="例如：只研究美国铰链的进口商、分销商与电商候选，排除消费电子；最多 3 个检索式、总计读取 6 页、收集 6 条信号并形成 3 个假设。"
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
      <strong>{{ error ? "状态提示" : "执行闸门" }}</strong>
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

      <div
        v-if="proposal.execution_mode === 'research_only'"
        class="research-plan"
      >
        <strong>只研究 · 计划线路：{{ proposal.planned_discovery_lanes?.map(laneLabel).join(" / ") }}</strong>
        <p aria-label="计划来源方向">
          计划来源方向：{{ proposal.planned_source_channels?.map(sourceChannelLabel).join(" / ") || "尚未记录来源计划" }}
        </p>
        <p>各来源独立检索，按本轮预算执行；免费额度耗尽即停止。</p>
        <p>领英与贸易记录仅检索公开页面，不代表已接入商业数据库；联系方式仍需核验。</p>
        <p>进口商候选不代表运输记录或客户采购确认；无需活动，不执行联系人、邮箱验证、发送和报价。</p>
        <ResearchAccessCard :status="proposal.research_access ?? null" />
        <p
          v-if="proposal.confirmation_blocked_reason"
          role="alert"
        >
          {{ stopLabel(proposal.confirmation_blocked_reason) }}
        </p>
      </div>

      <section
        v-if="proposal.state === 'confirmed' && !receiptRunId"
        class="research-plan"
        aria-label="工作流启动状态"
      >
        <p v-if="execution?.state === 'not_started'">
          提案已确认，尚未创建运行记录；提案决定不等于工作流已启动。
        </p>
        <p v-else>
          提案已确认，启动状态待核实；请只读刷新，不要重复提交。
        </p>
        <button
          v-if="execution?.can_resume"
          type="button"
          :disabled="deciding || decisionUncertain"
          @click="decide('confirm')"
        >
          {{ proposal.research_access?.confirmation_requires_recheck ? "重新核验后恢复启动" : "恢复启动（沿用原提案）" }}
        </button>
      </section>

      <footer class="decision-bar">
        <button
          type="button"
          :disabled="deciding"
          @click="refreshProposal"
        >
          刷新提案状态
        </button>
        <div>
          <strong>{{ proposal.state === "pending_confirmation" ? "确认后才会执行" : `提案${stateLabel}` }}</strong>
          <span v-if="proposal.decided_by_name">决定人：{{ proposal.decided_by_name }}</span>
          <span v-else-if="proposal.state === 'pending_confirmation'">未确认提案不会触发任何发现任务</span>
        </div>
        <div
          v-if="proposal.state === 'pending_confirmation'"
          class="decision-actions"
        >
          <button
            type="button"
            :disabled="deciding || decisionUncertain"
            @click="decide('reject')"
          >
            拒绝，不执行
          </button>
          <button
            class="btn-primary"
            type="button"
            :disabled="deciding || decisionUncertain || proposal.can_confirm !== true"
            @click="decide('confirm')"
          >
            {{ deciding ? "正在提交…" : proposal.research_access?.confirmation_requires_recheck ? "确认重新核验后研究" : "确认并启动" }}
          </button>
        </div>
      </footer>
    </section>

    <section
      v-if="receiptRunId"
      class="run-receipt"
      aria-label="工作流启动回执"
    >
      <div>
        <span class="receipt-mark">✓</span>
        <div>
          <h2>受限工作流已启动</h2>
          <p>运行记录 {{ receiptRunId }} <span v-if="confirmation">· 老板指令 {{ confirmation.directive_id }}</span></p>
        </div>
      </div>
      <RouterLink to="/demand">
        查看需求雷达 →
      </RouterLink>
      <RouterLink :to="{ path: '/runs', query: { run: receiptRunId } }">
        查看本次运行记录 →
      </RouterLink>
    </section>

    <section
      v-if="admissionPolicyVisible"
      class="admission-policy"
      aria-labelledby="admission-policy-title"
    >
      <header>
        <div>
          <p class="eyebrow">
            寻源准入
          </p>
          <h2 id="admission-policy-title">
            寻源准入策略
          </h2>
        </div>
        <strong>当前策略：{{ admissionPolicyLabel }}</strong>
      </header>
      <p class="admission-boundary">
        按需求簇规模决定尚未启动寻源案例的顺序。一个需求仍对应一个寻源案例，需求簇不是合并订单。
      </p>
      <dl
        v-if="admissionPolicy.directive_version"
        class="policy-facts"
      >
        <div><dt>生效版本</dt><dd>老板指令 v{{ admissionPolicy.directive_version }}</dd></div>
        <div><dt>自动准入</dt><dd>{{ admissionPolicy.automatic_admission_enabled ? "启用" : "关闭" }}</dd></div>
        <div><dt>每轮上限</dt><dd>{{ admissionPolicy.batch_limit }} 个寻源案例</dd></div>
      </dl>
      <form
        aria-label="寻源准入策略提案"
        @submit.prevent="createAdmissionProposal"
      >
        <label for="admission-message">老板原始指令</label>
        <textarea
          id="admission-message"
          v-model="admissionMessage"
          name="admission_message"
          rows="2"
          :disabled="admissionSubmitting || admissionConfirming"
        />
        <div class="admission-fields">
          <label class="switch-field">
            <input
              v-model="admissionEnabled"
              name="automatic_admission_enabled"
              type="checkbox"
              :disabled="admissionSubmitting || admissionConfirming"
            >
            自动准入
          </label>
          <label for="admission-limit">每轮上限</label>
          <input
            id="admission-limit"
            v-model.number="admissionBatchLimit"
            name="batch_limit"
            type="number"
            min="1"
            max="50"
            :disabled="admissionSubmitting || admissionConfirming"
          >
          <button
            class="btn-primary"
            type="submit"
            :disabled="!admissionMessage.trim() || admissionBatchLimit < 1 || admissionBatchLimit > 50 || admissionSubmitting || admissionConfirming"
          >
            {{ admissionSubmitting ? "正在生成…" : "生成准入提案" }}
          </button>
        </div>
      </form>
      <div
        v-if="admissionError"
        class="control-note danger"
        role="alert"
      >
        {{ admissionError }}
      </div>
      <article
        v-if="admissionProposal"
        class="admission-proposal"
      >
        <div class="comparison-grid">
          <section class="comparison-card raw-card">
            <span class="card-label">老板原话 · 事实记录</span><p>{{ admissionProposal.raw_text }}</p>
          </section>
          <section class="comparison-card interpretation-card">
            <span class="card-label">系统理解 · 待确认推断</span><p>{{ admissionProposal.interpretation_summary }}</p>
          </section>
          <section class="comparison-card behavior-card">
            <span class="card-label">确认后的行为变化</span><ul>
              <li
                v-for="change in admissionProposal.expected_behavior_changes"
                :key="change"
              >
                {{ change }}
              </li>
            </ul>
          </section>
        </div>
        <dl class="policy-facts proposal-policy-facts">
          <div><dt>排序模式</dt><dd>{{ codeLabel(admissionProposal.sourcing_admission_mode) }}</dd></div>
          <div><dt>自动准入</dt><dd>{{ admissionProposal.automatic_sourcing_admission_enabled ? "启用" : "关闭" }}</dd></div>
          <div><dt>每轮上限</dt><dd>{{ admissionProposal.sourcing_admission_batch_limit }} 个寻源案例</dd></div>
        </dl>
        <footer class="admission-decision">
          <span>{{ admissionProposal.state === "confirmed" ? "提案已确认" : "提案不会启动寻源流程" }}</span>
          <button
            v-if="admissionProposal.state === 'pending_confirmation'"
            class="btn-primary"
            type="button"
            :disabled="admissionConfirming"
            @click="confirmAdmissionProposal"
          >
            {{ admissionConfirming ? "正在确认…" : "确认提案" }}
          </button>
        </footer>
        <div
          v-if="admissionConfirmation"
          class="admission-confirmed"
          role="status"
        >
          <strong>生效老板指令 v{{ admissionConfirmation.directive_version }}</strong>
          <span>确认只更新准入策略，不代表寻源已启动。</span>
        </div>
      </article>
    </section>
  </div>
</template>

<style scoped>
.command-shell { overflow: auto; }
.research-plan { display: grid; gap: 10px; margin-top: 16px; }
.command-head { justify-content: space-between; padding-top: var(--space3); }
.command-head > div { display: flex; align-items: baseline; gap: var(--space3); }
.eyebrow { color: var(--fact); font-size: 11px; font-weight: 800; letter-spacing: .16em; }
.head-copy { color: var(--text-secondary); max-width: 620px; }
.composer, .proposal, .run-receipt, .admission-policy { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; }
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
.admission-policy { display: grid; gap: var(--space3); padding: var(--space5); }
.admission-policy > header, .admission-decision, .admission-confirmed { display: flex; justify-content: space-between; align-items: center; gap: var(--space3); }
.admission-policy > header strong { color: var(--fact); }
.admission-boundary { color: var(--text-secondary); }
.policy-facts { grid-template-columns: repeat(3, 1fr); margin-top: 0; }
.policy-facts > div { display: grid; grid-template-columns: 1fr; gap: 2px; }
.admission-fields { display: flex; align-items: center; gap: var(--space3); margin-top: var(--space3); }
.admission-fields input[type="number"] { width: 90px; }
.switch-field { display: flex; align-items: center; gap: var(--space2); margin: 0; }
.admission-fields button { margin-left: auto; }
.admission-proposal { display: grid; gap: var(--space3); border-top: 1px solid var(--border); padding-top: var(--space4); }
.proposal-policy-facts { padding: var(--space3); border: 1px solid var(--border); border-radius: var(--radius); }
.admission-decision { color: var(--text-secondary); }
.admission-confirmed { border-left: 4px solid var(--fact); background: var(--fact-soft); color: var(--fact); padding: var(--space3) var(--space4); }
@media (max-width: 900px) {
  .composer, .comparison-grid, .proposal-details, .policy-facts { grid-template-columns: 1fr; }
  .decision-bar, .run-receipt, .admission-policy > header, .admission-decision, .admission-confirmed { align-items: flex-start; flex-direction: column; }
  .admission-fields { align-items: stretch; flex-direction: column; }
  .admission-fields input[type="number"] { width: 100%; }
  .admission-fields button { margin-left: 0; }
}
</style>
