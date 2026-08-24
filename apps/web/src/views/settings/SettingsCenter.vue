<script setup lang="ts">
import { computed, inject, onMounted, reactive, ref } from "vue";

import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";

type ApiClient = ReturnType<typeof createApiClient>;
type PlaybookOverview = components["schemas"]["PlaybookOverview"];
type PlaybookProposalAccepted = components["schemas"]["PlaybookProposalAccepted"];
type PlaybookProposalCreate = components["schemas"]["PlaybookProposalCreate"];
type PlaybookVersion = components["schemas"]["PlaybookVersionView"];
type PlaybookVersionStatus = components["schemas"]["PlaybookVersionStatusView"];

interface PlaybookForm {
  approvalRequirements: string;
  companyType: string;
  excludedCategories: string;
  excludedCountries: string;
  minimumDealAmount: string;
  minimumDealCurrency: string;
  monthlyBudgetCredits: string;
  sourcingRegions: string;
  supplyCapabilitiesNote: string;
}

const client = inject<ApiClient>("tradeos-api-client", apiClient);
const overview = ref<PlaybookOverview | null>(null);
const versions = ref<PlaybookVersionStatus[]>([]);
const loading = ref(true);
const submitting = ref(false);
const loadError = ref<string | null>(null);
const submitError = ref<string | null>(null);
const accepted = ref<PlaybookProposalAccepted | null>(null);
const idempotencyKey = ref<string | null>(null);
const formTouched = ref(false);

const form = reactive<PlaybookForm>({
  approvalRequirements: "",
  companyType: "",
  excludedCategories: "",
  excludedCountries: "",
  minimumDealAmount: "",
  minimumDealCurrency: "",
  monthlyBudgetCredits: "",
  sourcingRegions: "",
  supplyCapabilitiesNote: "",
});

const statusLabels: Readonly<Record<PlaybookVersionStatus["approval_state"], string>> = Object.freeze({
  applied: "已生效",
  apply_failed: "应用失败",
  approved: "已批准，等待应用",
  expired: "已过期",
  pending: "待审批",
  proposal_pending_submission: "等待创建审批",
  rejected: "已拒绝",
});

const errorLabels = Object.freeze({
  PLAYBOOK_APPROVAL_FACT_INVALID: "审批事实无效，请联系管理员核查",
  PLAYBOOK_BASE_VERSION_CONFLICT: "基准版本冲突，请基于当前生效版本重新提交",
});

const activeVersion = computed(() => overview.value?.active_version?.version ?? null);
const activeActivation = computed(() => overview.value?.active_version?.activation ?? null);

const liveDiff = computed(() => {
  const active = activeVersion.value;
  if (!active) return form.companyType || form.minimumDealAmount ? ["首次配置"] : [];
  const differences: string[] = [];
  const fields: Array<[string, string, string]> = [
    ["公司类型", active.company_type, form.companyType],
    ["最低成交金额", active.minimum_deal_amount, form.minimumDealAmount],
    ["币种", active.minimum_deal_currency, form.minimumDealCurrency],
    ["月度探索额度", active.monthly_budget_credits?.toString() ?? "", form.monthlyBudgetCredits],
    ["排除类别", active.excluded_categories.join(", "), form.excludedCategories],
    ["寻源区域", active.sourcing_regions.join(", "), form.sourcingRegions],
    ["排除国家", active.excluded_countries.join(", "), form.excludedCountries],
    ["额外审批动作", active.approval_requirements.join(", "), form.approvalRequirements],
    ["供应能力说明", active.supply_capabilities_note ?? "", form.supplyCapabilitiesNote],
  ];
  for (const [label, before, after] of fields) {
    if (before !== after) differences.push(`${label}：${before || "空"} → ${after || "空"}`);
  }
  return differences;
});

function displayTime(value: string | null | undefined): string {
  if (!value) return "尚未记录";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString("zh-CN");
}

function csv(values: string[]): string {
  return values.join(", ");
}

function parseList(value: string): string[] {
  return [...new Set(value.split(/[\n,，]/).map((item) => item.trim()).filter(Boolean))];
}

function copyActiveToForm(version: PlaybookVersion): void {
  form.companyType = version.company_type;
  form.minimumDealAmount = version.minimum_deal_amount;
  form.minimumDealCurrency = version.minimum_deal_currency;
  form.monthlyBudgetCredits = version.monthly_budget_credits?.toString() ?? "";
  form.excludedCategories = csv(version.excluded_categories);
  form.sourcingRegions = csv(version.sourcing_regions);
  form.excludedCountries = csv(version.excluded_countries);
  form.approvalRequirements = csv(version.approval_requirements);
  form.supplyCapabilitiesNote = version.supply_capabilities_note ?? "";
}

function safeLoadError(status: number): string {
  if (status === 403) return "只有老板可以查看或提交 Company Playbook";
  if (status === 503) return "Company Playbook 服务暂不可用，请稍后刷新";
  return "Company Playbook 读取失败，请稍后重试";
}

function editForm(): void {
  formTouched.value = true;
  idempotencyKey.value = null;
  accepted.value = null;
  submitError.value = null;
}

function newIdempotencyKey(): string {
  return `settings-playbook-${globalThis.crypto.randomUUID()}`;
}

async function loadSettings(): Promise<void> {
  loading.value = true;
  loadError.value = null;
  try {
    const [overviewResult, versionsResult] = await Promise.all([
      client.GET("/settings/playbook"),
      client.GET("/settings/playbook/versions"),
    ]);
    if (overviewResult.response.status !== 200 || !overviewResult.data) {
      loadError.value = safeLoadError(overviewResult.response.status);
      return;
    }
    if (versionsResult.response.status !== 200 || !versionsResult.data) {
      loadError.value = safeLoadError(versionsResult.response.status);
      return;
    }
    overview.value = overviewResult.data;
    versions.value = versionsResult.data;
    if (!formTouched.value && overviewResult.data.active_version) {
      copyActiveToForm(overviewResult.data.active_version.version);
    }
  } catch {
    loadError.value = "无法连接 Company Playbook 服务";
  } finally {
    loading.value = false;
  }
}

function proposalBody(): PlaybookProposalCreate | null {
  const amountPattern = /^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/;
  if (!form.companyType.trim()) {
    submitError.value = "请填写公司类型";
    return null;
  }
  if (!amountPattern.test(form.minimumDealAmount)) {
    submitError.value = "最低成交金额必须是非负十进制字符串";
    return null;
  }
  const currency = form.minimumDealCurrency.trim().toUpperCase();
  if (!/^[A-Z]{3}$/.test(currency)) {
    submitError.value = "币种必须是三位英文字母";
    return null;
  }
  if (form.monthlyBudgetCredits && !/^(?:0|[1-9][0-9]*)$/.test(form.monthlyBudgetCredits)) {
    submitError.value = "月度探索额度必须是非负整数";
    return null;
  }
  return {
    approval_requirements: parseList(form.approvalRequirements),
    company_type: form.companyType,
    excluded_categories: parseList(form.excludedCategories),
    excluded_countries: parseList(form.excludedCountries),
    minimum_deal_amount: form.minimumDealAmount,
    minimum_deal_currency: currency,
    monthly_budget_credits: form.monthlyBudgetCredits
      ? Number.parseInt(form.monthlyBudgetCredits, 10)
      : null,
    sourcing_regions: parseList(form.sourcingRegions),
    supply_capabilities_note: form.supplyCapabilitiesNote.trim() || null,
  };
}

async function submitProposal(): Promise<void> {
  submitError.value = null;
  const body = proposalBody();
  if (!body) return;
  const key = idempotencyKey.value ?? newIdempotencyKey();
  idempotencyKey.value = key;
  submitting.value = true;
  try {
    const result = await client.POST("/settings/playbook/proposals", {
      body,
      params: { header: { "Idempotency-Key": key } },
    });
    if (result.response.status === 202 && result.data) {
      accepted.value = result.data;
      idempotencyKey.value = null;
      await loadSettings();
      return;
    }
    if (result.response.status === 403) {
      submitError.value = "只有老板可以查看或提交 Company Playbook";
      return;
    }
    if (result.response.status === 503) {
      const retryAfter = result.response.headers.get("Retry-After");
      submitError.value = retryAfter && /^\d+$/.test(retryAfter)
        ? `服务暂不可用，${retryAfter} 秒后可重试`
        : "服务暂不可用，请稍后重试";
      return;
    }
    submitError.value = result.response.status === 400
      ? "候选内容未通过校验，请检查各字段"
      : "候选提交失败，请稍后重试";
  } catch {
    submitError.value = "网络连接失败；未编辑字段时重试会复用本次幂等键";
  } finally {
    submitting.value = false;
  }
}

onMounted(() => void loadSettings());
</script>

<template>
  <div class="shell settings-shell">
    <div class="page-head settings-head">
      <div>
        <p class="phase-eyebrow">
          COMPANY PLAYBOOK
        </p>
        <h1>系统设置</h1>
      </div>
      <div class="head-actions">
        <span class="status manual-status">变更必须审批</span>
        <button
          type="button"
          :disabled="loading"
          @click="loadSettings"
        >
          {{ loading ? "加载中…" : "刷新" }}
        </button>
      </div>
    </div>

    <div class="safe-banner danger">
      <span aria-hidden="true">!</span>
      <div>本页只能创建不可变候选版本；审批、激活和审计链路不能在设置页绕过。</div>
    </div>
    <div
      v-if="loadError"
      class="safe-banner danger"
      role="alert"
    >
      {{ loadError }}
    </div>

    <div
      v-if="overview?.contact_enrichment.reason_code === 'COUNTRY_POLICY_NOT_CONFIGURED'"
      class="safe-banner"
      role="status"
    >
      <span aria-hidden="true">i</span>
      <div><strong>国家政策未配置，联系人补全保持阻断。</strong>先提交含目标/排除国家的 Playbook 候选，审批生效后才能解除。</div>
    </div>

    <section class="settings-grid">
      <article
        class="settings-card active-card"
        aria-label="当前生效 Playbook"
      >
        <header>
          <div>
            <p class="card-kicker">
              ACTIVE VERSION
            </p><h2>当前经营边界</h2>
          </div>
        </header>
        <div
          v-if="loading"
          class="empty"
        >
          正在读取生效版本…
        </div>
        <div
          v-else-if="!activeVersion"
          class="empty compact-empty"
        >
          <strong>尚未配置 Company Playbook</strong>
          <span>系统不会补入默认市场、金额或国家。</span>
        </div>
        <template v-else>
          <h3>当前生效版本 v{{ activeVersion.version_number }}</h3>
          <dl class="fact-list">
            <div><dt>公司类型</dt><dd>{{ activeVersion.company_type }}</dd></div>
            <div><dt>最低成交金额</dt><dd>{{ activeVersion.minimum_deal_amount }} {{ activeVersion.minimum_deal_currency }}</dd></div>
            <div><dt>月度探索额度</dt><dd>{{ activeVersion.monthly_budget_credits ?? "未设置" }}</dd></div>
            <div><dt>排除类别</dt><dd>{{ csv(activeVersion.excluded_categories) || "无" }}</dd></div>
            <div><dt>寻源区域</dt><dd>{{ csv(activeVersion.sourcing_regions) || "无" }}</dd></div>
            <div><dt>排除国家</dt><dd>{{ csv(activeVersion.excluded_countries) || "无" }}</dd></div>
            <div><dt>额外审批</dt><dd>{{ csv(activeVersion.approval_requirements) || "仅全局硬边界" }}</dd></div>
          </dl>
          <div class="audit-box">
            <strong>提案与审批来源</strong>
            <p>提案人 {{ activeVersion.proposed_by }} · {{ displayTime(activeVersion.proposed_at) }}</p>
            <p>来源 {{ activeVersion.content_provenance.source_type }} / {{ activeVersion.content_provenance.source_id }}</p>
            <p>提取者 {{ activeVersion.content_provenance.extracted_by }} · {{ displayTime(activeVersion.content_provenance.extracted_at) }}</p>
            <p>批准人 {{ activeActivation?.approved_by }} · {{ displayTime(activeActivation?.approved_at) }}</p>
            <p>激活者 {{ activeActivation?.activated_by }} · {{ displayTime(activeActivation?.activated_at) }}</p>
          </div>
        </template>
      </article>

      <article
        class="settings-card proposal-card"
        aria-label="Playbook 候选表单"
      >
        <header>
          <div>
            <p class="card-kicker">
              CANDIDATE
            </p><h2>{{ activeVersion ? "修订经营边界" : "首次配置" }}</h2>
          </div><span>只创建候选</span>
        </header>
        <form @submit.prevent="submitProposal">
          <label for="company-type">公司类型<input
            id="company-type"
            v-model="form.companyType"
            name="company_type"
            autocomplete="off"
            required
            @input="editForm"
          ></label>
          <div class="field-row">
            <label for="minimum-deal">最低成交金额<input
              id="minimum-deal"
              v-model="form.minimumDealAmount"
              name="minimum_deal_amount"
              inputmode="decimal"
              pattern="(?:0|[1-9][0-9]*)(?:\.[0-9]+)?"
              autocomplete="off"
              required
              @input="editForm"
            ></label>
            <label for="currency">币种<input
              id="currency"
              v-model="form.minimumDealCurrency"
              name="minimum_deal_currency"
              maxlength="3"
              pattern="[A-Za-z]{3}"
              autocomplete="off"
              required
              @input="editForm"
            ></label>
          </div>
          <label for="budget">月度探索额度<input
            id="budget"
            v-model="form.monthlyBudgetCredits"
            name="monthly_budget_credits"
            inputmode="numeric"
            pattern="(?:0|[1-9][0-9]*)"
            autocomplete="off"
            @input="editForm"
          ></label>
          <label for="excluded-categories">排除类别<textarea
            id="excluded-categories"
            v-model="form.excludedCategories"
            name="excluded_categories"
            rows="2"
            placeholder="用逗号或换行分隔"
            @input="editForm"
          /></label>
          <label for="sourcing-regions">寻源区域<textarea
            id="sourcing-regions"
            v-model="form.sourcingRegions"
            name="sourcing_regions"
            rows="2"
            placeholder="用逗号或换行分隔"
            @input="editForm"
          /></label>
          <label for="excluded-countries">排除国家<textarea
            id="excluded-countries"
            v-model="form.excludedCountries"
            name="excluded_countries"
            rows="2"
            placeholder="用逗号或换行分隔"
            @input="editForm"
          /></label>
          <label for="approval-requirements">额外审批动作<textarea
            id="approval-requirements"
            v-model="form.approvalRequirements"
            name="approval_requirements"
            rows="2"
            placeholder="只允许加严的 action ID"
            @input="editForm"
          /></label>
          <label for="supply-note">供应能力说明<textarea
            id="supply-note"
            v-model="form.supplyCapabilitiesNote"
            name="supply_capabilities_note"
            rows="3"
            maxlength="4000"
            @input="editForm"
          /></label>

          <div
            v-if="liveDiff.length"
            class="diff-box"
            aria-label="候选变更摘要"
          >
            <strong>提交前差异</strong>
            <ul>
              <li
                v-for="item in liveDiff"
                :key="item"
              >
                {{ item }}
              </li>
            </ul>
          </div>
          <div
            v-if="submitError"
            class="form-message error"
            role="alert"
          >
            {{ submitError }}
          </div>
          <div
            v-if="accepted"
            class="form-message success"
            role="status"
          >
            <strong>候选版本已创建</strong>
            <span :data-candidate-id="accepted.playbook_version_id">候选 {{ accepted.playbook_version_id }}</span>
            <span :data-run-id="accepted.run_id">Run {{ accepted.run_id }}</span>
            <div><a href="/approvals">前往审批中心</a><a :href="`/runs/${accepted.run_id}`">查看 Run</a></div>
          </div>
          <button
            class="btn-primary submit-button"
            type="submit"
            :disabled="loading || submitting || Boolean(loadError)"
          >
            {{ submitting ? "正在创建候选…" : "提交审批候选" }}
          </button>
        </form>
      </article>
    </section>

    <section
      class="settings-card history-card"
      aria-label="Playbook 版本历史"
    >
      <header>
        <div>
          <p class="card-kicker">
            VERSION HISTORY
          </p><h2>版本与审批状态</h2>
        </div><span>{{ versions.length }} 个版本</span>
      </header>
      <div
        v-if="loading"
        class="empty compact-empty"
      >
        正在读取版本历史…
      </div>
      <div
        v-else-if="!versions.length"
        class="empty compact-empty"
      >
        尚无候选版本
      </div>
      <ol
        v-else
        class="version-list"
      >
        <li
          v-for="item in versions"
          :key="item.version.playbook_version_id"
        >
          <header>
            <div><strong>v{{ item.version.version_number }}</strong><code>{{ item.version.playbook_version_id }}</code></div>
            <span
              class="status"
              :class="`state-${item.approval_state}`"
            >{{ statusLabels[item.approval_state] }}</span>
          </header>
          <p>{{ item.version.company_type }} · {{ item.version.minimum_deal_amount }} {{ item.version.minimum_deal_currency }}</p>
          <p>提案人 {{ item.version.proposed_by }} · {{ displayTime(item.version.proposed_at) }}</p>
          <p>来源 {{ item.version.content_provenance.source_type }} / {{ item.version.content_provenance.source_id }} · 提取者 {{ item.version.content_provenance.extracted_by }}</p>
          <p v-if="item.version.content_provenance.confirmed_by">
            确认人 {{ item.version.content_provenance.confirmed_by }} · {{ displayTime(item.version.content_provenance.confirmed_at) }}
          </p>
          <p v-if="item.approval_id">
            审批 {{ item.approval_id }}
          </p>
          <p
            v-if="item.application_error_code"
            class="history-error"
          >
            {{ errorLabels[item.application_error_code] }}
          </p>
        </li>
      </ol>
    </section>
  </div>
</template>

<style scoped>
.settings-shell { overflow-y: auto; gap: var(--space4); }
.settings-head { justify-content: space-between; padding-top: var(--space2); }
.head-actions { display: flex; align-items: center; gap: var(--space2); }
.settings-grid { display: grid; grid-template-columns: minmax(300px, .8fr) minmax(480px, 1.2fr); gap: var(--space4); align-items: start; }
.settings-card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: var(--space4); }
.settings-card > header { display: flex; justify-content: space-between; gap: var(--space3); align-items: flex-end; padding-bottom: var(--space3); border-bottom: 1px solid var(--border); }
.settings-card h2 { font-size: 18px; }
.settings-card > header > span { color: var(--text-secondary); font-size: 11px; }
.active-card h3 { margin-top: var(--space4); font-size: 16px; }
.fact-list { display: grid; gap: var(--space2); margin-top: var(--space3); }
.fact-list div { display: grid; grid-template-columns: 110px minmax(0, 1fr); gap: var(--space2); }
.fact-list dt { color: var(--text-secondary); font-size: 11px; }
.fact-list dd { margin: 0; overflow-wrap: anywhere; font-size: 12px; }
.audit-box, .diff-box { margin-top: var(--space4); border-radius: var(--radius); background: var(--fact-soft); padding: var(--space3); }
.audit-box p { margin-top: var(--space1); color: var(--text-secondary); font-size: 11px; overflow-wrap: anywhere; }
.empty { display: grid; place-items: center; min-height: 180px; color: var(--text-secondary); }
.compact-empty { min-height: 110px; gap: var(--space1); text-align: center; }
.compact-empty strong { color: var(--text-primary); }
form { display: grid; gap: var(--space3); padding-top: var(--space4); }
label { display: grid; gap: var(--space1); color: var(--text-secondary); font-size: 11px; font-weight: 700; }
input, textarea { width: 100%; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface); padding: 8px 10px; color: var(--text-primary); font-weight: 400; }
textarea { resize: vertical; min-height: 52px; }
.field-row { display: grid; grid-template-columns: minmax(0, 1fr) 110px; gap: var(--space3); }
.diff-box { background: var(--canvas); }
.diff-box ul { display: grid; gap: var(--space1); margin: var(--space2) 0 0 18px; color: var(--text-secondary); font-size: 11px; }
.form-message { display: grid; gap: var(--space1); border: 1px solid; border-radius: var(--radius); padding: var(--space3); font-size: 12px; overflow-wrap: anywhere; }
.form-message.error { border-color: var(--danger); background: var(--danger-soft); color: var(--danger); }
.form-message.success { border-color: var(--fact); background: var(--fact-soft); color: var(--fact); }
.form-message.success div { display: flex; gap: var(--space3); margin-top: var(--space1); }
.form-message a { color: var(--action); }
.submit-button { justify-self: start; }
.history-card { margin-bottom: var(--space4); }
.version-list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--space3); margin-top: var(--space4); list-style: none; }
.version-list li { min-width: 0; border: 1px solid var(--border); border-left: 4px solid var(--fact); border-radius: var(--radius); padding: var(--space3); }
.version-list header { display: flex; justify-content: space-between; gap: var(--space2); align-items: center; }
.version-list header > div { display: flex; min-width: 0; gap: var(--space2); align-items: center; }
.version-list code { color: var(--text-secondary); font-size: 10px; overflow-wrap: anywhere; }
.version-list p { margin-top: var(--space1); color: var(--text-secondary); font-size: 11px; overflow-wrap: anywhere; }
.version-list .history-error { color: var(--danger); font-weight: 700; }
.state-pending, .state-proposal_pending_submission, .state-approved { border-color: var(--warning); color: var(--warning); background: var(--warning-soft); }
.state-rejected, .state-expired, .state-apply_failed { border-color: var(--danger); color: var(--danger); background: var(--danger-soft); }
.state-applied { border-color: var(--fact); color: var(--fact); background: var(--fact-soft); }
@media (max-width: 900px) { .settings-grid { grid-template-columns: 1fr; } }
@media (max-width: 700px) { .field-row, .version-list { grid-template-columns: 1fr; } .settings-card { padding: var(--space3); } }
</style>
