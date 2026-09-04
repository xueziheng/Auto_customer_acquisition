<script setup lang="ts">
import { computed, inject, onMounted, ref, watch } from "vue";
import { RouterLink } from "vue-router";

import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";

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

const minimumDistinctAccounts = ref<number | null>(3);
const minimumRecurringAccounts = ref<number | null>(null);
const minimumDistinctCountries = ref<number | null>(null);
const minimumQuantityUnitAccounts = ref<number | null>(null);
const requireUnifiedUnit = ref(false);

interface RetainedRequest {
  body: PolicyContent;
  key: string;
  serializedBody: string;
}

let retainedRequest: RetainedRequest | null = null;

const formSnapshot = computed(() => JSON.stringify([
  minimumDistinctAccounts.value,
  minimumDistinctCountries.value,
  minimumQuantityUnitAccounts.value,
  minimumRecurringAccounts.value,
  requireUnifiedUnit.value,
]));

watch(formSnapshot, () => {
  retainedRequest = null;
  retryable.value = false;
  actionError.value = null;
  actionNotice.value = null;
});

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
  if (!loaded.value) loading.value = true;
  error.value = null;
  const failures: string[] = [];
  try {
    const result = await client.GET("/products/catalog-policies/active");
    if (result.response.status === 200) activePolicy.value = result.data ?? null;
    else failures.push(safeReadError(result.response.status));
  } catch {
    failures.push("无法连接目录策略服务");
  }
  try {
    const result = await client.GET("/products/catalog-policies", {
      params: { query: { limit: 50 } },
    });
    if (result.response.status === 200 && result.data) history.value = result.data;
    else failures.push(safeReadError(result.response.status));
  } catch {
    failures.push("无法连接目录策略服务");
  }
  error.value = failures[0] ?? null;
  loaded.value = true;
  loading.value = false;
}

function policyBody(): PolicyContent | null {
  const minimum = minimumDistinctAccounts.value;
  if (!Number.isInteger(minimum) || minimum === null || minimum < 1) return null;
  const optionalValues = [
    minimumDistinctCountries.value,
    minimumQuantityUnitAccounts.value,
    minimumRecurringAccounts.value,
  ];
  if (optionalValues.some((value) => value !== null && (!Number.isInteger(value) || value < 0))) return null;
  return {
    minimum_distinct_accounts: minimum,
    minimum_distinct_countries: minimumDistinctCountries.value,
    minimum_quantity_unit_accounts: minimumQuantityUnitAccounts.value,
    minimum_recurring_accounts: minimumRecurringAccounts.value,
    require_unified_unit: requireUnifiedUnit.value,
  };
}

async function submitPolicy(): Promise<void> {
  if (submitting.value) return;
  const body = policyBody();
  if (!body) {
    actionError.value = "策略门槛必须是有效整数";
    return;
  }
  const serializedBody = JSON.stringify(body);
  if (!retainedRequest || retainedRequest.serializedBody !== serializedBody) {
    retainedRequest = {
      body: { ...body },
      key: globalThis.crypto.randomUUID(),
      serializedBody,
    };
  }
  const request = retainedRequest;
  submitting.value = true;
  actionError.value = null;
  actionNotice.value = null;
  retryable.value = false;
  try {
    const result = await client.POST("/products/catalog-policies", {
      params: { header: { "Idempotency-Key": request.key } },
      body: request.body,
    });
    if (result.response.status === 202 && result.data) {
      retainedRequest = null;
      actionNotice.value = "策略候选已提交，等待 Approval Center 决定";
      emit("submitted");
      await loadPolicies();
      return;
    }
    retryable.value = result.response.status === 503;
    actionError.value = safeSubmitError(result.response.status);
  } catch {
    retryable.value = true;
    actionError.value = "提交结果未知，保留原请求；请按原请求重试";
  } finally {
    submitting.value = false;
  }
}

onMounted(() => void loadPolicies());
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
    <template v-else>
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
            min="1"
          ></label>
          <label>复购客户数下限（可选）<input
            v-model.number="minimumRecurringAccounts"
            name="minimum_recurring_accounts"
            type="number"
            min="0"
            placeholder="不要求"
          ></label>
          <label>国家数下限（可选）<input
            v-model.number="minimumDistinctCountries"
            name="minimum_distinct_countries"
            type="number"
            min="0"
            placeholder="不要求"
          ></label>
          <label>数量/单位覆盖下限（可选）<input
            v-model.number="minimumQuantityUnitAccounts"
            name="minimum_quantity_unit_accounts"
            type="number"
            min="0"
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
