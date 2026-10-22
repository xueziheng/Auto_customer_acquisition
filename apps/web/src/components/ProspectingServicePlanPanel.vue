<script setup lang="ts">
import { computed, inject, onMounted, ref } from "vue";
import type { components } from "../api/api";
import { apiClient, createApiClient } from "../api/client";
import { useQuoteRequestScope } from "../views/costing-quotes/quote-request-scope";

const props = defineProps<{ query?: components["schemas"]["InventoryQuery"] | null }>();
type Status = components["schemas"]["ServicePlanStatusView"];
type Policy = components["schemas"]["ServiceTierPolicy"];
const client = inject<ReturnType<typeof createApiClient>>("tradeos-api-client", apiClient);
const status = ref<Status | null>(null);
const loading = ref(false), saving = ref(false), uncertain = ref(false);
const error = ref<string | null>(null), notice = ref("");
const enabled = ref(false);
const selectedQuery = computed(() => props.query ?? plan.value?.query ?? null);
const tierLabels: Record<Policy["tier"], string> = { basic: "基础", standard: "标准", advanced: "进阶" };
const plan = computed(() => status.value?.plan ?? null);
const currentPolicy = computed(() => status.value?.catalog.find((item) => item.tier === plan.value?.tier));
const gate = useQuoteRequestScope(client, () => [], () => {
  status.value = null; error.value = null; notice.value = "";
  enabled.value = false; loading.value = false; saving.value = false; uncertain.value = false;
});

function safeError(code: number): string {
  if (code === 403 || code === 401) return "当前身份无权读取或配置获客服务计划";
  if (code === 409) return "计划版本、服务期或产品资料状态已变化，请只读刷新后核对";
  if (code === 400 || code === 422) return "排程未保存，请核对已确认的产品目标与当前服务计划";
  return "获客服务计划暂不可用，请刷新核对；企业列表仍可独立使用";
}
function apply(value: Status): void {
  status.value = value;
  enabled.value = value.plan?.schedule_enabled ?? false;
}
async function refresh(): Promise<void> {
  if (saving.value) return;
  const op = gate.begin("load"); if (!op?.valid()) return;
  loading.value = true; error.value = null;
  try {
    const result = await client.GET("/prospects/service-plan", { signal: op.signal });
    if (!op.valid()) return;
    if (result.response.status === 200 && result.data && Array.isArray(result.data.catalog)) {
      apply(result.data); uncertain.value = false;
    } else { status.value = null; error.value = safeError(result.response.status); }
  } catch { if (op.valid()) { status.value = null; error.value = safeError(503); } }
  finally { if (op.valid()) loading.value = false; }
}
async function save(): Promise<void> {
  const current = plan.value;
  if (!current || saving.value || loading.value || uncertain.value || !currentPolicy.value) return;
  error.value = null; notice.value = "";
  if (enabled.value && !selectedQuery.value?.confirmed_refs?.length) { error.value = "请先在上方选择已确认的企业产品资料并提交获客目标，再保存周期"; return; }
  const op = gate.begin("save"); if (!op?.valid()) return;
  saving.value = true;
  try {
    const result = await client.PUT("/prospects/service-plan/schedule", {
      body: { ...(selectedQuery.value ? {query: selectedQuery.value} : {proposal_id: current.proposal_id}), enabled: enabled.value, expected_version: current.version },
      signal: op.signal,
    });
    if (!op.valid()) return;
    if (result.response.status === 200 && result.data && Array.isArray(result.data.catalog)) {
      apply(result.data); notice.value = "排程配置已保存；实际执行仍由工作流重新核验。";
    } else if (result.response.status >= 500 || result.response.status === 200) {
      uncertain.value = true;
    } else { error.value = safeError(result.response.status); }
  } catch { if (op.valid()) uncertain.value = true; }
  finally { if (op.valid()) saving.value = false; }
}
onMounted(() => void refresh());
</script>

<template>
  <section
    class="service-plan"
    aria-label="获客服务计划"
  >
    <header>
      <h2>获客服务计划</h2>
      <button
        type="button"
        :disabled="loading || saving"
        @click="refresh"
      >
        刷新计划
      </button>
    </header>
    <p>三档提供相同功能，区别在处理数量、探索频率和资料补全深度。邮箱可达性统一自动验证。</p>
    <p
      v-if="loading"
      role="status"
    >
      正在读取服务计划…
    </p>
    <p
      v-if="error"
      role="alert"
    >
      {{ error }}
    </p>
    <p
      v-if="uncertain"
      role="alert"
    >
      排程提交结果待核实；请先只读刷新，核对前不能重复提交。
    </p>
    <p
      v-if="notice"
      role="status"
    >
      {{ notice }}
    </p>
    <template v-if="status">
      <div class="tier-catalog">
        <article
          v-for="policy in status.catalog"
          :key="policy.tier"
          :class="{ current: plan?.tier === policy.tier }"
        >
          <h3>{{ tierLabels[policy.tier] }}<span v-if="plan?.tier === policy.tier"> · 当前档位</span></h3>
          <p>{{ policy.period_days }} 天：候选 {{ policy.candidate_limit }} / 搜索 {{ policy.search_limit }} / 联系人 {{ policy.contact_limit }}</p>
          <p>每 {{ policy.cadence_hours }} 小时探索；每企业最多 {{ policy.max_pages_per_company }} 页</p>
          <p>每企业联系人上限 {{ policy.max_contacts_per_company }}</p>
          <p>每轮上限：搜索 {{ policy.per_run_search_limit }} / 页面 {{ policy.per_run_page_limit }} / 信号 {{ policy.per_run_signal_limit }} / 假设 {{ policy.per_run_hypothesis_limit }}</p>
          <small>政策版本 {{ policy.policy_version }}</small>
        </article>
      </div>
      <p>实际数据来源以已配置的服务商为准。Campaign 与发件身份沿用已有授权。</p>
      <p>档位由后台运维分配。处理上限不承诺交付同等数量的企业、有效邮箱或贸易机会。</p>
      <p v-if="!plan">
        尚未分配服务档位；当前无法配置周期探索。
      </p>
      <template v-else>
        <dl>
          <dt>服务期</dt><dd>{{ plan.period_started_at }} 至 {{ plan.period_ends_at }}</dd>
          <dt>本期保守预留 / 上限</dt><dd>候选 {{ plan.candidates_reserved }} / {{ currentPolicy?.candidate_limit ?? "未知" }} · 搜索 {{ plan.searches_reserved }} / {{ currentPolicy?.search_limit ?? "未知" }} · 联系人 {{ plan.contacts_reserved }} / {{ currentPolicy?.contact_limit ?? "未知" }}</dd>
          <dt>周期探索</dt><dd>{{ plan.schedule_enabled ? "已启用" : "已暂停" }}</dd>
          <dt>下次计划</dt><dd>{{ plan.next_run_at ?? "尚未安排" }}</dd>
          <dt>最近运行</dt><dd>{{ plan.last_run_at ?? "尚无记录" }}</dd>
        </dl>
        <p>预留包含未决消耗，不代表已取得结果；切档不清零。本期结束后停止新增任务，续期由运维处理。</p>
        <form @submit.prevent="save">
          <p>周期目标：{{ selectedQuery?.product_tags.join("、") ?? "请在上方提交获客目标" }}</p>
          <p v-if="plan.target_kind === 'legacy_research'">历史研究排程已保留；需重新确认产品目标后才会进入统一获客任务。</p>
          <label class="checkbox"><input
            v-model="enabled"
            name="schedule_enabled"
            type="checkbox"
            :disabled="saving || uncertain || loading"
          >启用周期探索</label>
          <button
            type="submit"
            :disabled="saving || uncertain || loading || !currentPolicy"
          >
            {{ saving ? "正在保存…" : "保存获客周期" }}
          </button>
        </form>
        <p>目标引用的企业产品资料须仍然确认有效，服务端会重新验证。周期提交统一库存请求，不自动附加联系人准备或发信授权；暂停不删除历史记录。</p>
      </template>
    </template>
  </section>
</template>

<style scoped>
.service-plan { display: grid; gap: 12px; padding: 20px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface); overflow-wrap: anywhere; }
header { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
h2 { font-size: 16px; } h3 { font-size: 14px; } p, small { color: var(--text-secondary); }
.tier-catalog { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
article { display: grid; gap: 8px; padding: 14px; border: 1px solid var(--border); border-radius: 8px; }
article.current { border-color: var(--fact); }
dl { display: grid; grid-template-columns: minmax(100px, .25fr) 1fr; gap: 8px 12px; } dd { min-width: 0; } dt { color: var(--text-secondary); }
form { display: flex; flex-wrap: wrap; align-items: end; gap: 12px; }
label { display: grid; gap: 6px; min-width: 0; } input:not([type=checkbox]) { max-width: 100%; min-width: 0; padding: 8px; color: var(--text-primary); background: var(--canvas); border: 1px solid var(--border); border-radius: 6px; }
.checkbox { display: flex; align-items: center; }
button { padding: 8px 12px; } [role=alert] { color: var(--warning); }
@media (max-width: 760px) { .tier-catalog { grid-template-columns: 1fr; } dl { grid-template-columns: 1fr; } form { align-items: stretch; flex-direction: column; } }
</style>
