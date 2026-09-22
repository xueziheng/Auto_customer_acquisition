<script lang="ts">
import type { components as ApiComponents } from "../../api/api";

type RetainedPolicyOutcome =
  | { kind: "determinate_failure"; status: number }
  | { kind: "retryable"; status: 503 }
  | { kind: "uncertain" };

type RetainedPolicyRequest = {
  body: ApiComponents["schemas"]["CatalogProposalPolicyContent"];
  key: string;
  outcome: RetainedPolicyOutcome;
  serializedBody: string;
};

const maximumRetainedIdentities = 8;
const retainedPolicyRequests = new WeakMap<object, Map<string, RetainedPolicyRequest>>();

function retainedRequestFor(client: object, identityKey: string): RetainedPolicyRequest | undefined {
  return retainedPolicyRequests.get(client)?.get(identityKey);
}

function retainRequest(client: object, identityKey: string, request: RetainedPolicyRequest): void {
  let registry = retainedPolicyRequests.get(client);
  if (!registry) {
    registry = new Map<string, RetainedPolicyRequest>();
    retainedPolicyRequests.set(client, registry);
  }
  if (!registry.has(identityKey) && registry.size >= maximumRetainedIdentities) {
    const oldestIdentity = registry.keys().next().value as string | undefined;
    if (oldestIdentity !== undefined) registry.delete(oldestIdentity);
  }
  registry.set(identityKey, request);
}

function forgetRequest(client: object, identityKey: string): void {
  const registry = retainedPolicyRequests.get(client);
  if (!registry) return;
  registry.delete(identityKey);
  if (registry.size === 0) retainedPolicyRequests.delete(client);
}
</script>

<script setup lang="ts">
import { computed, inject, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { RouterLink } from "vue-router";

import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";
import { useQuoteRequestScope } from "../costing-quotes/quote-request-scope";

type ApiClient = ReturnType<typeof createApiClient>;
type PolicyApiView = components["schemas"]["CatalogPolicyApiView"];
type PolicyContent = components["schemas"]["CatalogProposalPolicyContent"];
type PolicyState = PolicyApiView["policy"]["state"];
type ApprovalState = components["schemas"]["ApprovalState"];

const emit = defineEmits<{ submitted: [] }>();
const client = inject<ApiClient>("tradeos-api-client", apiClient);
const activePolicy = ref<PolicyApiView | null>(null);
const history = ref<PolicyApiView[]>([]);
const loading = ref(true);
const loaded = ref(false);
const error = ref<string | null>(null);
const submitting = ref(false);
const actionError = ref<string | null>(null);
const actionNotice = ref<string | null>(null);
const retryable = ref(false);

const maximumInteger = 2_147_483_647;
const minimumDistinctAccounts = ref<number | string | null>(null);
const minimumRecurringAccounts = ref<number | string | null>(null);
const minimumDistinctCountries = ref<number | string | null>(null);
const minimumQuantityUnitAccounts = ref<number | string | null>(null);
const requireUnifiedUnit = ref(false);
let retainedIdentityKey: string | null = null;
let componentMounted = false;
let restoringRetainedIntent = false;

function resetPanel(): void {
  activePolicy.value = null;
  history.value = [];
  loading.value = false;
  loaded.value = false;
  error.value = null;
  submitting.value = false;
  actionError.value = null;
  actionNotice.value = null;
  retryable.value = false;
  retainedIdentityKey = null;
  minimumDistinctAccounts.value = null;
  minimumRecurringAccounts.value = null;
  minimumDistinctCountries.value = null;
  minimumQuantityUnitAccounts.value = null;
  requireUnifiedUnit.value = false;
  restoreRetainedPolicyIntent();
  if (componentMounted) globalThis.queueMicrotask(() => void loadPolicies());
}

onBeforeUnmount(() => { componentMounted = false; });
const requestGate = useQuoteRequestScope(client, () => [], resetPanel);

const formSnapshot = computed(() => JSON.stringify([
  minimumDistinctAccounts.value,
  minimumDistinctCountries.value,
  minimumQuantityUnitAccounts.value,
  minimumRecurringAccounts.value,
  requireUnifiedUnit.value,
]));

watch(formSnapshot, () => {
  if (restoringRetainedIntent) return;
  if (retainedIdentityKey) forgetRequest(client, retainedIdentityKey);
  retainedIdentityKey = null;
  retryable.value = false;
  actionError.value = null;
  actionNotice.value = null;
}, { flush: "sync" });

function policyStateLabel(state: PolicyState): string {
  return {
    pending_approval: "待审批",
    active: "生效中",
    superseded: "已被替代",
    rejected: "已拒绝",
    expired: "已过期",
    stale: "已陈旧",
  }[state];
}

function approvalStateLabel(state: ApprovalState): string {
  return {
    pending: "待处理",
    approved: "已批准",
    applied: "已应用",
    apply_failed: "应用失败",
    rejected: "已拒绝",
    expired: "已过期",
  }[state];
}

function safeReadError(status: number): string {
  if (status === 403) return "当前身份无权读取目录策略";
  if (status === 404) return "目录策略记录不存在或不属于当前租户";
  if (status === 503) return "目录策略服务暂不可用";
  return "目录策略读取失败，请稍后重试";
}

function safeSubmitError(status: number): string {
  if (status === 403) return "当前身份无权提交目录策略";
  if (status === 404) return "目录策略依赖记录不存在或不属于当前租户";
  if (status === 409) return "策略基线或请求内容已冲突，请核对当前版本";
  if (status === 422) return "策略内容无效，请检查所有门槛";
  if (status === 503) return "策略服务暂不可用，请按原请求重试";
  return "目录策略提交失败，请核对后重试";
}

function formatDate(value: string | null): string {
  if (!value) return "未知";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

async function loadPolicies(): Promise<void> {
  const operation = requestGate.begin("policies");
  if (!operation?.valid()) return;
  loading.value = true;
  error.value = null;
  const failures: string[] = [];
  const [activeResult, historyResult] = await Promise.allSettled([
    client.GET("/products/catalog-policies/active", { signal: operation.signal }),
    client.GET("/products/catalog-policies", {
      params: { query: { limit: 50 } },
      signal: operation.signal,
    }),
  ]);
  if (!operation.valid()) return;
  const authorizationRevoked = (
    activeResult.status === "fulfilled" && activeResult.value.response.status === 403
  ) || (
    historyResult.status === "fulfilled" && historyResult.value.response.status === 403
  );
  if (authorizationRevoked) {
    activePolicy.value = null;
    history.value = [];
    failures.push(safeReadError(403));
  } else {
    if (activeResult.status === "fulfilled") {
      const status = activeResult.value.response.status;
      if (status === 200) activePolicy.value = activeResult.value.data ?? null;
      else {
        if (status !== 503) activePolicy.value = null;
        failures.push(safeReadError(status));
      }
    } else failures.push("无法连接目录策略服务");
    if (historyResult.status === "fulfilled") {
      const status = historyResult.value.response.status;
      if (status === 200 && historyResult.value.data) history.value = historyResult.value.data;
      else {
        if (status !== 503) history.value = [];
        failures.push(safeReadError(status));
      }
    } else failures.push("无法连接目录策略服务");
  }
  error.value = failures[0] ?? null;
  loaded.value = true;
  loading.value = false;
}

function normalizedOptionalInteger(value: number | string | null): number | null {
  if (value === "" || value === null) return null;
  return typeof value === "number" ? value : Number.NaN;
}

function boundedInteger(value: number | null, minimum: number): value is number {
  return value !== null && Number.isInteger(value) && value >= minimum && value <= maximumInteger;
}

function policyBody(): PolicyContent | null {
  const minimum = normalizedOptionalInteger(minimumDistinctAccounts.value);
  const recurring = normalizedOptionalInteger(minimumRecurringAccounts.value);
  const countries = normalizedOptionalInteger(minimumDistinctCountries.value);
  const quantity = normalizedOptionalInteger(minimumQuantityUnitAccounts.value);
  if (!boundedInteger(minimum, 2)) return null;
  if (recurring !== null && (!boundedInteger(recurring, 1) || recurring > minimum)) return null;
  if (quantity !== null && (!boundedInteger(quantity, 1) || quantity > minimum)) return null;
  if (countries !== null && !boundedInteger(countries, 2)) return null;
  if (requireUnifiedUnit.value && quantity === null) return null;
  return {
    minimum_distinct_accounts: minimum,
    minimum_distinct_countries: countries,
    minimum_quantity_unit_accounts: quantity,
    minimum_recurring_accounts: recurring,
    require_unified_unit: requireUnifiedUnit.value,
  };
}

function exactIdentityKey(): string | null {
  try {
    const snapshot = client.identitySnapshot();
    if (!snapshot.identity) return null;
    return JSON.stringify([
      snapshot.identity.tenantId,
      snapshot.identity.employeeId,
      snapshot.identity.mode,
    ]);
  } catch {
    return null;
  }
}

function restoreRetainedPolicyIntent(): void {
  const identityKey = exactIdentityKey();
  if (!identityKey) return;
  const request = retainedRequestFor(client, identityKey);
  if (!request) return;
  restoringRetainedIntent = true;
  try {
    minimumDistinctAccounts.value = request.body.minimum_distinct_accounts;
    minimumRecurringAccounts.value = request.body.minimum_recurring_accounts;
    minimumDistinctCountries.value = request.body.minimum_distinct_countries;
    minimumQuantityUnitAccounts.value = request.body.minimum_quantity_unit_accounts;
    requireUnifiedUnit.value = request.body.require_unified_unit;
  } finally {
    restoringRetainedIntent = false;
  }
  retainedIdentityKey = identityKey;
  if (request.outcome.kind === "determinate_failure") {
    retryable.value = false;
    actionError.value = safeSubmitError(request.outcome.status);
    actionNotice.value = null;
  } else {
    retryable.value = true;
    actionError.value = request.outcome.kind === "retryable"
      ? safeSubmitError(request.outcome.status)
      : "提交结果未知，保留原请求；请按原请求重试";
    actionNotice.value = "检测到同一身份尚未核清的原请求；重试将沿用原请求内容";
  }
}

async function submitPolicy(): Promise<void> {
  if (submitting.value || !requestGate.hasIdentity.value) return;
  const body = policyBody();
  if (!body) {
    actionError.value = "策略门槛不符合受控范围或字段关系";
    return;
  }
  const operation = requestGate.begin("submit-policy");
  const identityKey = exactIdentityKey();
  if (!operation?.valid() || !identityKey) return;
  const serializedBody = JSON.stringify(body);
  let request = retainedRequestFor(client, identityKey);
  if (!request || request.serializedBody !== serializedBody) {
    forgetRequest(client, identityKey);
    request = {
      body: { ...body },
      key: globalThis.crypto.randomUUID(),
      outcome: { kind: "uncertain" },
      serializedBody,
    };
    retainRequest(client, identityKey, request);
  }
  retainedIdentityKey = identityKey;
  submitting.value = true;
  actionError.value = null;
  actionNotice.value = null;
  retryable.value = false;
  request.outcome = { kind: "uncertain" };
  try {
    const result = await client.POST("/products/catalog-policies", {
      params: { header: { "Idempotency-Key": request.key } },
      body: request.body,
      signal: operation.signal,
    });
    if (!operation.valid() || exactIdentityKey() !== identityKey) return;
    if (result.response.status === 202 && result.data) {
      forgetRequest(client, identityKey);
      retainedIdentityKey = null;
      actionNotice.value = "策略候选已提交，等待 Approval Center 决定";
      emit("submitted");
      await loadPolicies();
      return;
    }
    if (result.response.status === 503) {
      retryable.value = true;
      request.outcome = { kind: "retryable", status: 503 };
    } else {
      retryable.value = false;
      request.outcome = { kind: "determinate_failure", status: result.response.status };
    }
    actionError.value = safeSubmitError(result.response.status);
  } catch {
    if (operation.valid() && exactIdentityKey() === identityKey) {
      retryable.value = true;
      actionError.value = "提交结果未知，保留原请求；请按原请求重试";
    }
  } finally {
    if (operation.valid() && exactIdentityKey() === identityKey) submitting.value = false;
  }
}

onMounted(() => {
  componentMounted = true;
  restoreRetainedPolicyIntent();
  void loadPolicies();
});
</script>

<template>
  <section
    class="catalog-region"
    data-region="catalog-policy"
    aria-labelledby="catalog-policy-title"
  >
    <header class="region-head">
      <div>
        <p class="phase-eyebrow">
          CATALOG POLICY
        </p><h2 id="catalog-policy-title">
          策略版本
        </h2>
      </div>
      <button
        type="button"
        :disabled="loading"
        @click="loadPolicies"
      >
        {{ loading ? "加载中…" : "刷新策略" }}
      </button>
    </header>
    <div
      v-if="error"
      class="safe-banner danger"
      role="alert"
    >
      {{ error }}
    </div>
    <div
      v-if="loading"
      class="region-empty"
    >
      正在读取目录策略…
    </div>
    <template v-if="loaded">
      <div
        v-if="!error && !activePolicy"
        class="disabled-message"
      >
        <strong>未配置即关闭</strong>
        <span>没有活动策略时不会产生新的目录候选产品提案。</span>
      </div>
      <article
        v-if="activePolicy"
        class="active-policy"
      >
        <header><strong>活动策略 {{ activePolicy.policy.policy_version_id }}</strong><span class="status">{{ policyStateLabel(activePolicy.policy.state) }}</span></header>
        <ul>
          <li>去重客户数至少 {{ activePolicy.policy.content.minimum_distinct_accounts }}</li>
          <li>复购客户门槛：{{ activePolicy.policy.content.minimum_recurring_accounts ?? "不要求" }}</li>
          <li>国家数门槛：{{ activePolicy.policy.content.minimum_distinct_countries ?? "不要求" }}</li>
          <li>数量/单位覆盖门槛：{{ activePolicy.policy.content.minimum_quantity_unit_accounts ?? "不要求" }}</li>
          <li>{{ activePolicy.policy.content.require_unified_unit ? "要求统一单位" : "不要求统一单位" }}</li>
        </ul>
        <RouterLink
          v-if="activePolicy.approval"
          :to="{ path: '/approvals', query: { approval_id: activePolicy.approval.approval_id } }"
        >
          审批状态：{{ approvalStateLabel(activePolicy.approval.state) }}
        </RouterLink>
      </article>

      <form
        class="policy-form"
        @submit.prevent="submitPolicy"
      >
        <fieldset :disabled="submitting">
          <legend>提交受控策略候选</legend>
          <label>去重客户数下限<input
            v-model.number="minimumDistinctAccounts"
            name="minimum_distinct_accounts"
            type="number"
            min="2"
            :max="maximumInteger"
            required
          ></label>
          <label>复购客户数下限（可选）<input
            v-model.number="minimumRecurringAccounts"
            name="minimum_recurring_accounts"
            type="number"
            min="1"
            :max="maximumInteger"
            placeholder="不要求"
          ></label>
          <label>国家数下限（可选）<input
            v-model.number="minimumDistinctCountries"
            name="minimum_distinct_countries"
            type="number"
            min="2"
            :max="maximumInteger"
            placeholder="不要求"
          ></label>
          <label>数量/单位覆盖下限（可选）<input
            v-model.number="minimumQuantityUnitAccounts"
            name="minimum_quantity_unit_accounts"
            type="number"
            min="1"
            :max="maximumInteger"
            placeholder="不要求"
          ></label>
          <label class="checkbox"><input
            v-model="requireUnifiedUnit"
            name="require_unified_unit"
            type="checkbox"
          >要求统一单位</label>
          <button
            data-action="submit-catalog-policy"
            type="submit"
          >
            {{ submitting ? "提交中…" : "提交策略候选" }}
          </button>
          <button
            v-if="retryable"
            data-action="retry-catalog-policy"
            type="button"
            @click="submitPolicy"
          >
            按原请求重试
          </button>
        </fieldset>
      </form>
      <p
        v-if="actionError"
        class="action-error"
        role="alert"
      >
        {{ actionError }}
      </p>
      <p
        v-if="actionNotice"
        class="action-notice"
        role="status"
      >
        {{ actionNotice }}
      </p>

      <div class="history-block">
        <h3>版本历史</h3>
        <div
          v-if="!error && !history.length"
          class="region-empty"
        >
          当前没有策略版本历史
        </div>
        <article
          v-for="entry in history"
          :key="entry.policy.policy_version_id"
          class="history-row"
        >
          <div><strong>{{ entry.policy.policy_version_id }}</strong><span>{{ formatDate(entry.policy.created_at) }}</span></div>
          <span class="status">{{ policyStateLabel(entry.policy.state) }}</span>
          <RouterLink
            v-if="entry.approval"
            :to="{ path: '/approvals', query: { approval_id: entry.approval.approval_id } }"
          >
            审批状态：{{ approvalStateLabel(entry.approval.state) }}
          </RouterLink>
          <span v-else>无关联审批</span>
        </article>
      </div>
    </template>
  </section>
</template>

<style scoped>
.catalog-region { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: var(--space5); display: grid; gap: var(--space4); }
.region-head, .active-policy header, .history-row { display: flex; align-items: center; justify-content: space-between; gap: var(--space3); }
.region-head h2 { font-size: 20px; }
.region-empty { color: var(--text-secondary); padding: var(--space3); text-align: center; }
.disabled-message { border: 1px solid var(--warning); background: var(--warning-soft); padding: var(--space3); display: grid; gap: var(--space1); }
.active-policy { border-left: 4px solid var(--fact); background: var(--fact-soft); padding: var(--space3); display: grid; gap: var(--space2); }
.active-policy ul { padding-left: 20px; display: grid; gap: var(--space1); }
.status { border: 1px solid var(--border); border-radius: 999px; padding: 2px 8px; white-space: nowrap; }
.policy-form fieldset { border: 1px solid var(--border); padding: var(--space3); display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: var(--space3); }
.policy-form legend { padding: 0 var(--space2); font-weight: 700; }
.policy-form label { display: grid; gap: var(--space1); color: var(--text-secondary); }
.policy-form input[type="number"] { width: 100%; padding: 7px 9px; border: 1px solid var(--border); border-radius: var(--radius-sm); }
.policy-form .checkbox { display: flex; flex-direction: row; align-items: center; }
.action-error { color: var(--danger); }
.action-notice { color: var(--fact); }
.history-block { display: grid; gap: var(--space2); }
.history-row { border-top: 1px solid var(--border); padding-top: var(--space2); flex-wrap: wrap; }
.history-row div { display: grid; }
.history-row div span, .history-row > span:last-child { color: var(--text-secondary); }
@media (max-width: 680px) { .region-head, .history-row { align-items: flex-start; flex-direction: column; } }
</style>
