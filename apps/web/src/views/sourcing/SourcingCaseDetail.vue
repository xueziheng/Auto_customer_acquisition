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
function idempotencyKey(prefix: string): string {
  return `${prefix}-${globalThis.crypto.randomUUID()}`;
}

function safeError(status: number): string {
  if (status === 403) return "当前身份无权执行此寻源操作";
  if (status === 404) return "案例不存在或不属于当前租户";
  if (status === 503) return "寻源服务暂不可用";
  return "寻源操作未完成，请核对范围与版本后重试";
}

async function loadCase(preserveStatus = false): Promise<void> {
  loading.value = true;
  error.value = null;
  if (!preserveStatus) notice.value = null;
  try {
    const [caseResult, candidatesResult, checksResult, planResult, reviewResult, quotaResult, uncertainResult] = await Promise.all([
      client.GET("/sourcing-cases/{case_id}", { params: { path: { case_id: caseId.value } } }),
      client.GET("/sourcing-cases/{case_id}/candidates", { params: { path: { case_id: caseId.value } } }),
      client.GET("/sourcing-cases/{case_id}/ladder-checks", { params: { path: { case_id: caseId.value } } }),
      client.GET("/sourcing-cases/{case_id}/public-search-plan", { params: { path: { case_id: caseId.value } } }),
      client.GET("/sourcing-cases/{case_id}/review", { params: { path: { case_id: caseId.value } } }),
      client.GET("/sourcing-cases/{case_id}/current-quota", { params: { path: { case_id: caseId.value } } }),
      client.GET("/sourcing-cases/{case_id}/uncertain-reconciliations", { params: { path: { case_id: caseId.value } } }),
    ]);
    if (caseResult.response.status !== 200 || !caseResult.data) {
      error.value = safeError(caseResult.response.status);
      return;
    }
    sourcingCase.value = caseResult.data;
    candidates.value = candidatesResult.data ?? [];
    checks.value = checksResult.data ?? [];
    plan.value = planResult.data ?? null;
    review.value = reviewResult.data ?? null;
    currentQuota.value = quotaResult.data ?? null;
    uncertainExecutions.value = uncertainResult.data ?? [];
  } catch {
    error.value = "无法连接寻源服务";
  } finally {
    loading.value = false;
  }
}

async function draftPlan(command: PublicPlanCommand): Promise<void> {
  await mutate(async () => client.POST("/sourcing-cases/{case_id}/public-search-plan", {
    body: command,
    params: { path: { case_id: caseId.value } },
  }), "计划草稿已保存；尚未确认、未预留额度、未执行搜索。");
}

async function confirmPlan(reference: PlanReference): Promise<void> {
  await mutate(async () => client.POST("/sourcing-cases/{case_id}/public-search-plan/confirm", {
    body: reference,
    params: {
      header: { "Idempotency-Key": idempotencyKey("sourcing-confirm") },
      path: { case_id: caseId.value },
    },
  }), "计划已确认；仍未执行公开搜索。");
}

async function runPlan(reference: PlanReference): Promise<void> {
  await mutate(async () => client.POST("/sourcing-cases/{case_id}/run", {
    body: reference,
    params: {
      header: { "Idempotency-Key": idempotencyKey("sourcing-run") },
      path: { case_id: caseId.value },
    },
  }), "已请求运行。系统会重新核对已确认计划、精确哈希与免费额度；不会走付费回退。");
}

async function submitReview(command: ReviewCommand): Promise<void> {
  await mutate(async () => client.POST("/sourcing-cases/{case_id}/review", {
    body: command,
    params: {
      header: { "Idempotency-Key": idempotencyKey("sourcing-review") },
      path: { case_id: caseId.value },
    },
  }), "人工选择已提交；这不是报价，也没有创建客户可见价格。");
}

async function reconcileUncertain(command: ReconciliationCommand): Promise<void> {
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
    await loadCase(true);
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

      <section class="detail-panel">
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

      <SourcingPlanForm
        :case-id="caseId"
        :case-version="sourcingCase.version"
        :current-quota="currentQuota"
        :disabled="mutating"
        :plan="plan"
        @draft="draftPlan"
        @confirm="confirmPlan"
        @run="runPlan"
      />
      <SourcingReviewForm
        :candidates="candidates"
        :case-version="sourcingCase.version"
        :disabled="mutating"
        :review="review"
        @submit="submitReview"
      />
      <SourcingRecoveryForm
        :disabled="mutating"
        :executions="uncertainExecutions"
        @reconcile="reconcileUncertain"
      />
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
