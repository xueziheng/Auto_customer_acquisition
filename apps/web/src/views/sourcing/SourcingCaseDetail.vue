<script setup lang="ts">
import { computed, inject, onMounted, ref } from "vue";
import { RouterLink, useRoute } from "vue-router";

import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";
import SourcingPlanForm from "./SourcingPlanForm.vue";
import SourcingRecoveryForm from "./SourcingRecoveryForm.vue";
import SourcingReviewForm from "./SourcingReviewForm.vue";

type ApiClient = ReturnType<typeof createApiClient>;
type PublicPlanCommand = components["schemas"]["PublicSourcingPlanCommand"];
type PublicPlan = components["schemas"]["PublicSourcingPlanReadView"];
type SourcingCandidate = components["schemas"]["SourcingCandidateReadView"];
type SourcingCase = components["schemas"]["SourcingCaseReadView"];
type SourcingLadderCheck = components["schemas"]["SourcingLadderCheckReadView"];
type SourcingReview = components["schemas"]["SourcingReviewReadView"];
type CurrentQuota = components["schemas"]["SourcingCurrentQuotaReadView"];
type ReconciliationCommand = components["schemas"]["SourcingUncertainReconciliationCommand"];
type UncertainExecution = components["schemas"]["SourcingUncertainExecutionReadView"];
type PlanReference = components["schemas"]["PlanReferenceBody"];
type ReviewCommand = components["schemas"]["SourcingReviewCommand"];
type SecondaryProjection = "candidates" | "plan" | "quota" | "review" | "uncertain";
type ProjectionState = "forbidden" | "loading" | "success" | "unavailable";
type RunAvailability = "available" | "forbidden" | "unavailable" | "paid" | "unknown" | "exhausted";

const client = inject<ApiClient>("tradeos-api-client", apiClient);
const route = useRoute();
const caseId = computed(() => String(route.params.caseId ?? ""));
const sourcingCase = ref<SourcingCase | null>(null);
const candidates = ref<SourcingCandidate[]>([]);
const checks = ref<SourcingLadderCheck[]>([]);
const plan = ref<PublicPlan | null>(null);
const review = ref<SourcingReview | null>(null);
const currentQuota = ref<CurrentQuota | null>(null);
const uncertainExecutions = ref<UncertainExecution[]>([]);
const loading = ref(true);
const mutating = ref(false);
const error = ref<string | null>(null);
const notice = ref<string | null>(null);
const hasLoadedCase = ref(false);
const projectionState = ref<Record<SecondaryProjection, ProjectionState>>({
  candidates: "loading",
  plan: "loading",
  quota: "loading",
  review: "loading",
  uncertain: "loading",
});
const canDraftPlan = computed(() => (
  hasLoadedCase.value
  && projectionState.value.plan === "success"
  && !mutating.value
));
const canConfirmPlan = computed(() => (
  canDraftPlan.value
  && plan.value?.status === "pending_confirmation"
));
const canReview = computed(() => (
  projectionState.value.candidates === "success"
  && projectionState.value.review === "success"
));
const canRetryCostHandoff = computed(() => (
  canReview.value
  && review.value?.confirmed_by != null
  && sourcingCase.value?.stop?.code === "opportunity_required"
  && !mutating.value
));
const canReconcile = computed(() => projectionState.value.uncertain === "success");
const runAvailability = computed<RunAvailability>(() => {
  if (projectionState.value.quota === "forbidden") return "forbidden";
  if (projectionState.value.quota !== "success") return "unavailable";
  const quota = currentQuota.value;
  const currentPlan = plan.value;
  if (!quota || !currentPlan) return "unknown";
  if (quota.cost_status === "paid" || quota.paygo_enabled === true) return "paid";
  if (
    quota.cost_status !== "free"
    || quota.paygo_enabled !== false
    || !quota.checked_at
    || !Number.isInteger(quota.remaining)
    || !Number.isInteger(currentPlan.worst_case_credits)
  ) return "unknown";
  if ((quota.remaining ?? 0) < currentPlan.worst_case_credits) return "exhausted";
  return "available";
});
const canRunPlan = computed(() => (
  hasLoadedCase.value
  && projectionState.value.plan === "success"
  && plan.value?.status === "authorized"
  && runAvailability.value === "available"
  && !mutating.value
));

const projectionMessage: Record<SecondaryProjection, {
  forbidden: string;
  loading: string;
  retry: string;
  unavailable: string;
}> = {
  candidates: {
    forbidden: "当前身份无权读取候选",
    loading: "正在读取候选…",
    retry: "重试读取候选",
    unavailable: "候选数据暂不可用",
  },
  plan: {
    forbidden: "当前身份无权读取寻源计划",
    loading: "正在读取寻源计划…",
    retry: "重试读取寻源计划",
    unavailable: "寻源计划暂不可用",
  },
  quota: {
    forbidden: "当前身份无权读取当前额度",
    loading: "正在读取当前额度…",
    retry: "重试读取当前额度",
    unavailable: "当前额度暂不可用",
  },
  review: {
    forbidden: "当前身份无权读取审核",
    loading: "正在读取审核…",
    retry: "重试读取审核",
    unavailable: "审核数据暂不可用",
  },
  uncertain: {
    forbidden: "当前身份无权读取不确定请求",
    loading: "正在读取不确定请求…",
    retry: "重试读取不确定请求",
    unavailable: "不确定请求暂不可用",
  },
};

function idempotencyKey(prefix: string): string {
  return `${prefix}-${globalThis.crypto.randomUUID()}`;
}

function safeError(status: number): string {
  if (status === 403) return "当前身份无权执行此寻源操作";
  if (status === 404) return "案例不存在或不属于当前租户";
  if (status === 503) return "寻源服务暂不可用";
  return "寻源操作未完成，请核对范围与版本后重试";
}

function setProjectionState(
  projection: SecondaryProjection,
  status: number | null,
): void {
  projectionState.value[projection] = status === 403 ? "forbidden" : "unavailable";
}

function beginProjection(projection: SecondaryProjection): void {
  projectionState.value[projection] = "loading";
}

async function loadCandidates(): Promise<boolean> {
  beginProjection("candidates");
  candidates.value = [];
  try {
    const result = await client.GET("/sourcing-cases/{case_id}/candidates", {
      params: { path: { case_id: caseId.value } },
    });
    if (result.response.status !== 200) {
      setProjectionState("candidates", result.response.status);
      return false;
    }
    candidates.value = result.data ?? [];
    projectionState.value.candidates = "success";
    return true;
  } catch {
    setProjectionState("candidates", null);
    return false;
  }
}

async function loadPlan(): Promise<boolean> {
  beginProjection("plan");
  plan.value = null;
  try {
    const result = await client.GET("/sourcing-cases/{case_id}/public-search-plan", {
      params: { path: { case_id: caseId.value } },
    });
    if (result.response.status !== 200) {
      setProjectionState("plan", result.response.status);
      return false;
    }
    plan.value = result.data ?? null;
    projectionState.value.plan = "success";
    return true;
  } catch {
    setProjectionState("plan", null);
    return false;
  }
}

async function loadReview(): Promise<boolean> {
  beginProjection("review");
  review.value = null;
  try {
    const result = await client.GET("/sourcing-cases/{case_id}/review", {
      params: { path: { case_id: caseId.value } },
    });
    if (result.response.status !== 200) {
      setProjectionState("review", result.response.status);
      return false;
    }
    review.value = result.data ?? null;
    projectionState.value.review = "success";
    return true;
  } catch {
    setProjectionState("review", null);
    return false;
  }
}

async function loadQuota(): Promise<boolean> {
  beginProjection("quota");
  currentQuota.value = null;
  try {
    const result = await client.GET("/sourcing-cases/{case_id}/current-quota", {
      params: { path: { case_id: caseId.value } },
    });
    if (result.response.status !== 200) {
      setProjectionState("quota", result.response.status);
      return false;
    }
    currentQuota.value = result.data ?? null;
    projectionState.value.quota = "success";
    return true;
  } catch {
    setProjectionState("quota", null);
    return false;
  }
}

async function loadUncertainExecutions(): Promise<boolean> {
  beginProjection("uncertain");
  uncertainExecutions.value = [];
  try {
    const result = await client.GET("/sourcing-cases/{case_id}/uncertain-reconciliations", {
      params: { path: { case_id: caseId.value } },
    });
    if (result.response.status !== 200) {
      setProjectionState("uncertain", result.response.status);
      return false;
    }
    uncertainExecutions.value = result.data ?? [];
    projectionState.value.uncertain = "success";
    return true;
  } catch {
    setProjectionState("uncertain", null);
    return false;
  }
}

async function loadLadderChecks(): Promise<void> {
  checks.value = [];
  try {
    const result = await client.GET("/sourcing-cases/{case_id}/ladder-checks", {
      params: { path: { case_id: caseId.value } },
    });
    if (result.response.status === 200) checks.value = result.data ?? [];
  } catch {
    checks.value = [];
  }
}

async function retryProjection(projection: SecondaryProjection): Promise<void> {
  if (projection === "candidates") await loadCandidates();
  if (projection === "plan") await loadPlan();
  if (projection === "quota") await loadQuota();
  if (projection === "review") await loadReview();
  if (projection === "uncertain") await loadUncertainExecutions();
}

async function loadCase(preserveStatus = false, includeQuota = true): Promise<boolean> {
  loading.value = true;
  hasLoadedCase.value = false;
  error.value = null;
  if (!preserveStatus) notice.value = null;
  try {
    const caseResult = await client.GET("/sourcing-cases/{case_id}", {
      params: { path: { case_id: caseId.value } },
    });
    if (caseResult.response.status !== 200 || !caseResult.data) {
      error.value = safeError(caseResult.response.status);
      return false;
    }
    sourcingCase.value = caseResult.data;
    hasLoadedCase.value = true;
    loading.value = false;
    const [candidatesLoaded, , planLoaded, reviewLoaded, quotaLoaded, uncertainLoaded] = await Promise.all([
      loadCandidates(),
      loadLadderChecks(),
      loadPlan(),
      loadReview(),
      includeQuota ? loadQuota() : Promise.resolve(true),
      loadUncertainExecutions(),
    ]);
    return candidatesLoaded && planLoaded && reviewLoaded && quotaLoaded && uncertainLoaded;
  } catch {
    hasLoadedCase.value = false;
    error.value = "无法连接寻源服务";
    return false;
  } finally {
    loading.value = false;
  }
}

async function draftPlan(command: PublicPlanCommand): Promise<void> {
  if (!canDraftPlan.value) return;
  await mutate(async () => client.POST("/sourcing-cases/{case_id}/public-search-plan", {
    body: command,
    params: { path: { case_id: caseId.value } },
  }), "计划草稿已保存；尚未确认、未预留额度、未执行搜索。", false);
}

async function confirmPlan(reference: PlanReference): Promise<void> {
  if (!canConfirmPlan.value) return;
  await mutate(async () => client.POST("/sourcing-cases/{case_id}/public-search-plan/confirm", {
    body: reference,
    params: {
      header: { "Idempotency-Key": idempotencyKey("sourcing-confirm") },
      path: { case_id: caseId.value },
    },
  }), "计划已确认；仍未执行公开搜索。", false);
}

async function runPlan(reference: PlanReference): Promise<void> {
  if (!canRunPlan.value) return;
  await mutate(async () => client.POST("/sourcing-cases/{case_id}/run", {
    body: reference,
    params: {
      header: { "Idempotency-Key": idempotencyKey("sourcing-run") },
      path: { case_id: caseId.value },
    },
  }), "已请求运行。系统会重新核对已确认计划、精确哈希与免费额度；不会走付费回退。");
}

async function submitReview(command: ReviewCommand): Promise<void> {
  if (!canReview.value) return;
  await mutate(async () => client.POST("/sourcing-cases/{case_id}/review", {
    body: command,
    params: {
      header: { "Idempotency-Key": idempotencyKey("sourcing-review") },
      path: { case_id: caseId.value },
    },
  }), "人工选择已提交；这不是报价，也没有创建客户可见价格。");
}

async function reconcileUncertain(command: ReconciliationCommand): Promise<void> {
  if (!canReconcile.value) return;
  await mutate(async () => client.POST("/sourcing-cases/{case_id}/reconcile-uncertain-request", {
    body: command,
    params: {
      header: { "Idempotency-Key": idempotencyKey("sourcing-reconcile") },
      path: { case_id: caseId.value },
    },
  }), "已保存人工核对事实；系统只会按精确不确定请求恢复，未显示任何 Provider 原文。");
}

async function mutate(
  operation: () => ReturnType<ApiClient["POST"]>,
  success: string,
  includeQuota = true,
): Promise<void> {
  mutating.value = true;
  error.value = null;
  notice.value = null;
  try {
    const result = await operation();
    if (result.response.status !== 200) {
      error.value = safeError(result.response.status);
      return;
    }
    const refreshed = await loadCase(true, includeQuota);
    if (!refreshed) {
      error.value = "操作已被服务器接受，但最新安全投影暂不可用；请重试受影响区块。";
      return;
    }
    notice.value = success;
  } catch {
    error.value = "无法连接寻源服务";
  } finally {
    mutating.value = false;
  }
}

onMounted(() => void loadCase());
</script>

<template>
  <div class="shell sourcing-detail-shell">
    <div class="page-head">
      <div>
        <RouterLink to="/sourcing">
          ← 返回寻源中心
        </RouterLink><p class="phase-eyebrow">
          SOURCING CASE
        </p><h1>寻源 Case</h1>
      </div>
      <button
        type="button"
        :disabled="loading || mutating"
        @click="() => loadCase()"
      >
        {{ loading ? "加载中…" : "刷新" }}
      </button>
    </div>
    <div class="safe-banner">
      <span aria-hidden="true">i</span><div>公开页面参考价（indicative）不可用于客户报价。事实、供应商自述、匹配推断和未知项必须分别阅读。</div>
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
      正在读取寻源证据…
    </div>
    <template v-else-if="sourcingCase">
      <section class="case-summary detail-panel">
        <div><span>Case</span><strong>{{ sourcingCase.case_id }}</strong></div>
        <div>
          <span>Need</span><RouterLink :to="`/demand/needs/${sourcingCase.need_id}`">
            {{ sourcingCase.need_id }}
          </RouterLink>
        </div>
        <div><span>状态</span><strong>{{ sourcingCase.state }}</strong></div>
        <div><span>梯子</span><strong>{{ sourcingCase.ladder_checked_to ?? "未知" }}</strong></div>
        <div v-if="sourcingCase.stop">
          <span>停止原因</span><strong>{{ sourcingCase.stop.code }}</strong>
        </div>
      </section>

      <section class="detail-panel">
        <header>
          <div>
            <p class="card-kicker">
              MATCH LADDER
            </p><h2>匹配梯子检查</h2>
          </div><span>{{ checks.length }} 项</span>
        </header>
        <p
          v-if="!checks.length"
          class="muted"
        >
          尚无可展示的梯子检查事实。
        </p>
        <ol
          v-else
          class="ladder-list"
        >
          <li
            v-for="check in checks"
            :key="check.check_id"
          >
            第 {{ check.rung }} 级 · {{ check.outcome }} · {{ check.checked_by }}<small v-if="check.evidence_refs.length">证据：{{ check.evidence_refs.join("、") }}</small>
          </li>
        </ol>
      </section>

      <section
        v-if="projectionState.candidates === 'success'"
        class="detail-panel"
      >
        <header>
          <div>
            <p class="card-kicker">
              CANDIDATE EVIDENCE
            </p><h2>候选与证据</h2>
          </div><span>{{ candidates.length }} 个</span>
        </header>
        <div
          v-if="!candidates.length"
          class="muted"
        >
          没有候选；未知不是“合格”。
        </div>
        <article
          v-for="candidate in candidates"
          :key="candidate.candidate_id"
          class="candidate-card"
        >
          <header><div><h3>{{ candidate.supplier_name }}</h3><p>{{ candidate.product_title }} · {{ candidate.verification_status }}</p></div><span class="status indicative">indicative</span></header>
          <div class="candidate-columns">
            <section>
              <h4>网页观察事实</h4><dl>
                <template
                  v-for="(fact, name) in candidate.observed_facts"
                  :key="name"
                >
                  <dt>{{ name }}</dt><dd>{{ fact.value }} · {{ fact.evidence_ref }}</dd>
                </template>
              </dl>
            </section>
            <section>
              <h4>供应商自述</h4><dl>
                <template
                  v-for="(claim, name) in candidate.supplier_claims"
                  :key="name"
                >
                  <dt>{{ name }}</dt><dd>{{ claim.value }} · {{ claim.evidence_ref }}</dd>
                </template>
              </dl>
            </section>
            <section>
              <h4>匹配推断</h4><dl>
                <template
                  v-for="(inference, name) in candidate.match_inferences"
                  :key="name"
                >
                  <dt>{{ name }}</dt><dd>{{ inference.value }} · 基于 {{ inference.based_on.join("、") }}</dd>
                </template>
              </dl>
            </section>
            <section><h4>未知 / 待核验</h4><p>{{ candidate.verification_missing.join("、") || "无" }}</p></section>
          </div>
          <section class="candidate-audit">
            <h4>规格逐项比较</h4><p
              v-if="!candidate.spec_comparisons.length"
              class="muted"
            >
              没有可展示的规格比较。
            </p><ul v-else>
              <li
                v-for="comparison in candidate.spec_comparisons"
                :key="comparison.spec_name"
              >
                {{ comparison.spec_name }}：需求 {{ comparison.required }} · 提供 {{ comparison.offered ?? "未知" }} · {{ comparison.level }}<span v-if="comparison.substitutable !== null"> · 可替代：{{ comparison.substitutable ? "是" : "否" }}</span><span v-if="comparison.substitution_impact"> · 影响：{{ comparison.substitution_impact }}</span><span v-if="comparison.needs_customer_confirmation"> · 需客户确认</span>
              </li>
            </ul>
          </section>
          <section class="candidate-audit">
            <h4>八项核验</h4><p>产品类型、材质、尺寸、型号、数量档、MOQ、计价单位、币种</p><p>状态：{{ candidate.verification_status }} · 缺项：{{ candidate.verification_missing.join("、") || "无" }}</p><p v-if="candidate.rejection_reasons.length">
              拒绝原因：{{ candidate.rejection_reasons.join("、") }}
            </p>
          </section>
          <div class="safe-banner source-warning">
            <strong>不可用于客户报价</strong><span>公开页面参考价（indicative）</span>
          </div>
          <ul class="indicative-prices">
            <li
              v-for="tier in candidate.indicative_price_tiers"
              :key="tier.minimum_quantity"
            >
              {{ tier.minimum_quantity }} 起：{{ tier.amount }} {{ tier.currency }} / {{ tier.unit }}
            </li>
          </ul>
          <p class="meta">
            Artifact：{{ candidate.evidence.map((item) => item.artifact_id).join("、") }}
          </p>
        </article>
      </section>

      <section
        v-else
        class="detail-panel projection-state"
        role="alert"
      >
        <h2>候选与证据</h2>
        <p>
          {{ projectionState.candidates === "loading" ? projectionMessage.candidates.loading : projectionState.candidates === "forbidden" ? projectionMessage.candidates.forbidden : projectionMessage.candidates.unavailable }}
        </p>
        <button
          v-if="projectionState.candidates === 'unavailable'"
          type="button"
          :disabled="mutating"
          @click="retryProjection('candidates')"
        >
          {{ projectionMessage.candidates.retry }}
        </button>
      </section>

      <section
        v-if="projectionState.quota !== 'success'"
        class="detail-panel projection-state"
        role="alert"
      >
        <h2>当前安全额度</h2>
        <p>
          {{ projectionState.quota === "loading" ? projectionMessage.quota.loading : projectionState.quota === "forbidden" ? projectionMessage.quota.forbidden : projectionMessage.quota.unavailable }}
        </p>
        <button
          v-if="projectionState.quota === 'unavailable'"
          type="button"
          :disabled="mutating"
          @click="retryProjection('quota')"
        >
          {{ projectionMessage.quota.retry }}
        </button>
      </section>

      <SourcingPlanForm
        v-if="projectionState.plan === 'success'"
        :can-confirm="canConfirmPlan"
        :can-draft="canDraftPlan"
        :can-run="canRunPlan"
        :case-id="caseId"
        :case-version="sourcingCase.version"
        :current-quota="currentQuota"
        :plan="plan"
        :run-availability="runAvailability"
        @draft="draftPlan"
        @confirm="confirmPlan"
        @retry-quota="retryProjection('quota')"
        @run="runPlan"
      />
      <section
        v-else
        class="detail-panel projection-state"
        role="alert"
      >
        <h2>公开寻源计划</h2>
        <p>
          {{ projectionState.plan === "loading" ? projectionMessage.plan.loading : projectionState.plan === "forbidden" ? projectionMessage.plan.forbidden : projectionMessage.plan.unavailable }}
        </p>
        <button
          v-if="projectionState.plan === 'unavailable'"
          type="button"
          :disabled="mutating"
          @click="retryProjection('plan')"
        >
          {{ projectionMessage.plan.retry }}
        </button>
      </section>

      <SourcingReviewForm
        v-if="projectionState.candidates === 'success' && projectionState.review === 'success'"
        :candidates="candidates"
        :can-retry-cost-handoff="canRetryCostHandoff"
        :case-version="sourcingCase.version"
        :disabled="mutating || !canReview"
        :review="review"
        @submit="submitReview"
      />
      <section
        v-else-if="projectionState.review !== 'success'"
        class="detail-panel projection-state"
        role="alert"
      >
        <h2>人工审核</h2>
        <p>
          {{ projectionState.review === "loading" ? projectionMessage.review.loading : projectionState.review === "forbidden" ? projectionMessage.review.forbidden : projectionMessage.review.unavailable }}
        </p>
        <button
          v-if="projectionState.review === 'unavailable'"
          type="button"
          :disabled="mutating"
          @click="retryProjection('review')"
        >
          {{ projectionMessage.review.retry }}
        </button>
      </section>

      <SourcingRecoveryForm
        v-if="projectionState.uncertain === 'success'"
        :disabled="mutating || !canReconcile"
        :executions="uncertainExecutions"
        @reconcile="reconcileUncertain"
      />
      <section
        v-else
        class="detail-panel projection-state"
        role="alert"
      >
        <h2>不确定请求核对</h2>
        <p>
          {{ projectionState.uncertain === "loading" ? projectionMessage.uncertain.loading : projectionState.uncertain === "forbidden" ? projectionMessage.uncertain.forbidden : projectionMessage.uncertain.unavailable }}
        </p>
        <button
          v-if="projectionState.uncertain === 'unavailable'"
          type="button"
          :disabled="mutating"
          @click="retryProjection('uncertain')"
        >
          {{ projectionMessage.uncertain.retry }}
        </button>
      </section>
    </template>
  </div>
</template>

<style scoped>
.sourcing-detail-shell { overflow-y: auto; }
.detail-panel { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: var(--space4); display: grid; gap: var(--space3); }
.detail-panel > header, .candidate-card > header { display: flex; justify-content: space-between; gap: var(--space3); }
.case-summary { grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); }
.case-summary div { display: grid; gap: 2px; }.case-summary span, .muted, .meta { color: var(--text-secondary); font-size: 12px; }
.ladder-list, .indicative-prices { padding-left: 20px; display: grid; gap: var(--space2); }.ladder-list small { display: block; color: var(--text-secondary); }
.candidate-card { border-top: 1px solid var(--border); padding-top: var(--space4); display: grid; gap: var(--space3); }.candidate-card h3 { font-size: 16px; }.candidate-card h4 { color: var(--fact); font-size: 13px; }
.candidate-columns { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: var(--space3); }.candidate-columns section { background: var(--canvas); border-radius: var(--radius-sm); padding: var(--space3); }.candidate-columns dl { display: grid; gap: 2px; }.candidate-columns dt { color: var(--text-secondary); font-size: 12px; }.candidate-columns dd { margin: 0; overflow-wrap: anywhere; }
.candidate-audit { border: 1px solid var(--border); border-radius: var(--radius-sm); padding: var(--space3); }.candidate-audit h4 { color: var(--fact); font-size: 13px; }.candidate-audit ul { margin: 0; padding-left: 20px; display: grid; gap: var(--space1); }
.indicative { color: var(--warning); border-color: var(--warning); background: var(--warning-soft); }.source-warning { display: grid; gap: 2px; }.empty { padding: var(--space5); text-align: center; color: var(--text-secondary); }
</style>
