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
type CountryPolicyActive = components["schemas"]["CountryPolicyActiveView"];
type CountryPolicyField = components["schemas"]["CountryPolicyField"];
type CountryPolicyOverview = components["schemas"]["CountryPolicyOverview"];
type CountryPolicyProposal = components["schemas"]["CountryPolicyProposalCreate"];
type CountryPolicyProposalAccepted = components["schemas"]["CountryPolicyProposalAccepted"];
type CountryPolicySource = components["schemas"]["CountryPolicyFieldSourceInput"];
type CountryPolicyVersion = components["schemas"]["CountryPolicyVersionView"];
type CountryPolicyVersionStatus = components["schemas"]["CountryPolicyVersionStatusView"];

type CountryPolicyBooleanField = Exclude<
  CountryPolicyField,
  "opt_out_deadline_days" | "requirements"
>;

type CountryPolicyFormState = Omit<
  CountryPolicyProposal,
  CountryPolicyBooleanField | "field_sources" | "opt_out_deadline_days" | "requirements"
> & Record<CountryPolicyBooleanField, boolean | null> & {
  field_sources: Record<CountryPolicyField, CountryPolicySource>;
  opt_out_deadline_days: string;
  requirements: string;
};

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
const countryOverview = ref<CountryPolicyOverview | null>(null);
const countryVersions = ref<CountryPolicyVersionStatus[]>([]);
const countryLoading = ref(true);
const countryHistoryLoading = ref(false);
const countryLoadError = ref<string | null>(null);
const countrySubmitError = ref<string | null>(null);
const countryAccepted = ref<CountryPolicyProposalAccepted | null>(null);
const countrySubmitting = ref(false);
const countryIdempotencyKey = ref<string | null>(null);
const revisionBase = ref<CountryPolicyVersion | null>(null);
const selectedCountry = ref<string | null>(null);

const countryPolicyFields = Object.freeze([
  { key: "public_research_allowed", label: "public_research_allowed" },
  { key: "contact_enrichment_allowed", label: "contact_enrichment_allowed" },
  { key: "cold_b2b_email_allowed", label: "cold_b2b_email_allowed" },
  { key: "personal_data_basis_required", label: "personal_data_basis_required" },
  { key: "subject_type_affects_judgment", label: "subject_type_affects_judgment" },
  { key: "contact_type_affects_judgment", label: "contact_type_affects_judgment" },
  { key: "opt_out_deadline_days", label: "opt_out_deadline_days" },
  { key: "local_representative_required", label: "local_representative_required" },
  { key: "requirements", label: "requirements" },
] satisfies ReadonlyArray<{ key: CountryPolicyField; label: string }>);

const countryBooleanFields = Object.freeze([
  { key: "public_research_allowed", label: "公开研究" },
  { key: "contact_enrichment_allowed", label: "联系人补全" },
  { key: "cold_b2b_email_allowed", label: "冷启动 B2B 邮件" },
  { key: "personal_data_basis_required", label: "要求个人数据处理依据" },
  { key: "subject_type_affects_judgment", label: "主体类型影响判断" },
  { key: "contact_type_affects_judgment", label: "联系人类型影响判断" },
  { key: "local_representative_required", label: "要求本地代表" },
] satisfies ReadonlyArray<{ key: CountryPolicyBooleanField; label: string }>);

function emptyCountrySources(): Record<CountryPolicyField, CountryPolicySource> {
  return Object.fromEntries(countryPolicyFields.map(({ key }) => [
    key,
    { source_id: "", source_type: "employee_input" },
  ])) as Record<CountryPolicyField, CountryPolicySource>;
}

const countryForm = reactive<CountryPolicyFormState>({
  cold_b2b_email_allowed: null,
  contact_enrichment_allowed: null,
  contact_type_affects_judgment: null,
  country: "",
  field_sources: emptyCountrySources(),
  local_representative_required: null,
  notes: "",
  opt_out_deadline_days: "",
  personal_data_basis_required: null,
  public_research_allowed: null,
  requirements: "",
  subject_type_affects_judgment: null,
});

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

const countryStatusLabels: Readonly<Record<CountryPolicyVersionStatus["approval_state"], string>> = Object.freeze({
  applied: "已应用",
  apply_failed: "应用失败",
  approved: "已批准，等待应用",
  expired: "已过期",
  pending: "待审批",
  proposal_pending_submission: "等待创建审批",
  rejected: "已拒绝",
});

const countryErrorLabels: Readonly<Record<
  NonNullable<CountryPolicyVersionStatus["application_error_code"]>,
  string
>> = Object.freeze({
  COUNTRY_POLICY_APPROVAL_FACT_INVALID: "审批事实无效，请联系管理员核查",
  COUNTRY_POLICY_BASE_VERSION_CONFLICT: "基准版本冲突，请基于当前生效版本重新提交",
});

const activeVersion = computed(() => overview.value?.active_version?.version ?? null);
const activeActivation = computed(() => overview.value?.active_version?.activation ?? null);
const activeCountryPolicies = computed(() => countryOverview.value?.active_policies ?? []);
const missingCountrySources = computed(() => countryPolicyFields
  .filter(({ key }) => {
    const source = countryForm.field_sources[key];
    if (!source.source_id.trim()) return true;
    return source.source_type === "web_page"
      && (!source.source_url?.trim() || !source.page_hash?.trim());
  })
  .map(({ key }) => key));
const countryFormValid = computed(() =>
  countryForm.country.trim().length > 0
  && countryForm.notes.trim().length > 0
  && countryBooleanFields.every(({ key }) => typeof countryForm[key] === "boolean")
  && (
    countryForm.opt_out_deadline_days === ""
    || /^(?:[1-9]|[1-9][0-9]|[12][0-9]{2}|3[0-5][0-9]|36[0-5])$/.test(countryForm.opt_out_deadline_days)
  )
  && missingCountrySources.value.length === 0,
);
const countryReadinessMessage = computed(() => {
  switch (countryOverview.value?.contact_enrichment.reason_code) {
    case "COUNTRY_POLICY_NOT_CONFIGURED":
      return "尚无任何已激活国家政策，联系人补全保持阻断。";
    case "CONTACT_ENRICHMENT_NOT_ALLOWED":
      return "已激活政策均禁止联系人补全，系统不会调用外部 Provider。";
    case "CONTACT_ENRICHMENT_NOT_COMPOSED":
      return "Hunter / Provider 生产组合尚未完成；即使已有允许政策，联系人补全仍保持阻断。";
    default:
      return countryOverview.value?.contact_enrichment.state === "ready"
        ? "联系人补全已就绪。"
        : "正在核对联系人补全就绪状态。";
  }
});

const countryLiveDiff = computed(() => revisionBase.value
  ? policyDifferences(revisionBase.value, countryForm)
  : []);

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

function policyValue(
  policy: CountryPolicyVersion | CountryPolicyFormState,
  field: CountryPolicyField,
): string {
  const value = policy[field];
  if (typeof value === "boolean") return value ? "允许" : "禁止";
  if (value === null || value === "") return "未设置";
  if (Array.isArray(value)) return value.join(", ") || "无";
  return String(value);
}

function policyDifferences(
  before: CountryPolicyVersion,
  after: CountryPolicyVersion | CountryPolicyFormState,
): string[] {
  return countryPolicyFields.flatMap(({ key }) => {
    const beforeValue = policyValue(before, key);
    const afterValue = policyValue(after, key);
    return beforeValue === afterValue ? [] : [`${key}：${beforeValue} → ${afterValue}`];
  });
}

function historyDifferences(version: CountryPolicyVersion): string[] {
  const active = activeCountryPolicies.value.find(
    (item) => item.version.country_key === version.country_key,
  )?.version;
  if (!active || active.country_policy_version_id === version.country_policy_version_id) return [];
  return policyDifferences(active, version);
}

function exactCountrySource(
  source: components["schemas"]["Provenance"],
): CountryPolicySource {
  if (source.source_type === "web_page") {
    return {
      page_hash: source.page_hash,
      source_id: source.source_id,
      source_type: source.source_type,
      source_url: source.source_url,
    };
  }
  if (source.source_type === "upload") {
    return { source_id: source.source_id, source_type: source.source_type };
  }
  return { source_id: source.source_id, source_type: "employee_input" };
}

function normalizeCountrySource(field: CountryPolicyField): void {
  const source = countryForm.field_sources[field];
  if (source.source_type !== "web_page") {
    delete source.source_url;
    delete source.page_hash;
  }
}

function resetCountryForm(): void {
  revisionBase.value = null;
  countryAccepted.value = null;
  countrySubmitError.value = null;
  countryForm.country = "";
  countryForm.public_research_allowed = null;
  countryForm.contact_enrichment_allowed = null;
  countryForm.cold_b2b_email_allowed = null;
  countryForm.personal_data_basis_required = null;
  countryForm.subject_type_affects_judgment = null;
  countryForm.contact_type_affects_judgment = null;
  countryForm.opt_out_deadline_days = "";
  countryForm.local_representative_required = null;
  countryForm.requirements = "";
  countryForm.notes = "";
  countryForm.field_sources = emptyCountrySources();
}

function beginCountryRevision(active: CountryPolicyActive): void {
  const version = active.version;
  revisionBase.value = version;
  countryAccepted.value = null;
  countrySubmitError.value = null;
  countryForm.country = version.country;
  for (const { key } of countryBooleanFields) countryForm[key] = version[key];
  countryForm.opt_out_deadline_days = version.opt_out_deadline_days?.toString() ?? "";
  countryForm.requirements = version.requirements.join(", ");
  countryForm.notes = version.notes;
  countryForm.field_sources = Object.fromEntries(countryPolicyFields.map(({ key }) => [
    key,
    exactCountrySource(version.field_provenance[key]),
  ])) as Record<CountryPolicyField, CountryPolicySource>;
  void selectCountry(version.country);
}

function newCountryIdempotencyKey(): string {
  return `settings-country-policy-${globalThis.crypto.randomUUID()}`;
}

async function loadCountryHistory(country: string): Promise<void> {
  countryHistoryLoading.value = true;
  try {
    const result = await client.GET("/settings/country-policies/versions", {
      params: { query: { country, limit: 50 } },
    });
    countryVersions.value = result.response.status === 200 && result.data ? result.data : [];
  } catch {
    countryVersions.value = [];
  } finally {
    countryHistoryLoading.value = false;
  }
}

async function selectCountry(country: string): Promise<void> {
  selectedCountry.value = country;
  await loadCountryHistory(country);
}

async function loadCountryPolicies(): Promise<void> {
  countryLoading.value = true;
  countryLoadError.value = null;
  try {
    const result = await client.GET("/settings/country-policies");
    if (result.response.status !== 200 || !result.data) {
      countryLoadError.value = result.response.status === 403
        ? "只有老板可以查看或提交国家政策包"
        : "国家政策包读取失败，请稍后重试";
      return;
    }
    countryOverview.value = result.data;
    const first = result.data.active_policies[0];
    if (first) await selectCountry(first.version.country);
    else {
      selectedCountry.value = null;
      countryVersions.value = [];
    }
  } catch {
    countryLoadError.value = "无法连接国家政策包服务";
  } finally {
    countryLoading.value = false;
  }
}

function countryProposalBody(): CountryPolicyProposal | null {
  if (!countryFormValid.value) {
    countrySubmitError.value = missingCountrySources.value.length
      ? "每个决策字段都必须填写安全来源"
      : "请完整填写国家、决策事实与核验说明";
    return null;
  }
  const fieldSources = Object.fromEntries(countryPolicyFields.map(({ key }) => [
    key,
    { ...countryForm.field_sources[key] },
  ])) as CountryPolicyProposal["field_sources"];
  return {
    cold_b2b_email_allowed: countryForm.cold_b2b_email_allowed as boolean,
    contact_enrichment_allowed: countryForm.contact_enrichment_allowed as boolean,
    contact_type_affects_judgment: countryForm.contact_type_affects_judgment as boolean,
    country: countryForm.country.trim(),
    field_sources: fieldSources,
    local_representative_required: countryForm.local_representative_required as boolean,
    notes: countryForm.notes.trim(),
    opt_out_deadline_days: countryForm.opt_out_deadline_days
      ? Number.parseInt(countryForm.opt_out_deadline_days, 10)
      : null,
    personal_data_basis_required: countryForm.personal_data_basis_required as boolean,
    public_research_allowed: countryForm.public_research_allowed as boolean,
    requirements: parseList(countryForm.requirements),
    subject_type_affects_judgment: countryForm.subject_type_affects_judgment as boolean,
  };
}

async function submitCountryProposal(): Promise<void> {
  if (countrySubmitting.value) return;
  countrySubmitError.value = null;
  const body = countryProposalBody();
  if (!body) return;
  const key = countryIdempotencyKey.value ?? newCountryIdempotencyKey();
  countryIdempotencyKey.value = key;
  countrySubmitting.value = true;
  try {
    const result = await client.POST("/settings/country-policies/proposals", {
      body,
      params: { header: { "Idempotency-Key": key } },
    });
    countryIdempotencyKey.value = null;
    if (result.response.status === 202 && result.data) {
      countryAccepted.value = result.data;
      await loadCountryPolicies();
      return;
    }
    countrySubmitError.value = result.response.status === 422
      ? "候选内容未通过严格校验，请核对九项来源"
      : "国家政策候选提交失败，请稍后重试";
  } catch {
    countrySubmitError.value = "网络结果未知；再次提交将复用本次幂等键";
  } finally {
    countrySubmitting.value = false;
  }
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

async function refreshSettings(): Promise<void> {
  await Promise.all([loadSettings(), loadCountryPolicies()]);
}

onMounted(() => void refreshSettings());
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
          :disabled="loading || countryLoading"
          @click="refreshSettings"
        >
          {{ loading || countryLoading ? "加载中…" : "刷新" }}
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

    <section
      class="settings-card country-policy-workspace"
      aria-labelledby="country-policy-title"
    >
      <header>
        <div>
          <p class="card-kicker">
            COUNTRY POLICY
          </p>
          <h2 id="country-policy-title">
            国家政策包
          </h2>
        </div>
        <button
          type="button"
          @click="resetCountryForm"
        >
          新建国家政策
        </button>
      </header>

      <div
        class="safe-banner policy-principle"
        role="note"
      >
        <span aria-hidden="true">i</span>
        <div>
          <strong>系统不提供国家法律默认值。</strong>
          仅由授权人员录入并确认已核验的政策事实；提交后仍须由另一身份审批。
        </div>
      </div>
      <div
        v-if="countryLoadError"
        class="safe-banner danger"
        role="alert"
      >
        {{ countryLoadError }}
      </div>
      <div
        v-else
        class="safe-banner readiness-banner"
        role="status"
      >
        {{ countryReadinessMessage }}
      </div>

      <div
        class="coverage-grid"
        aria-label="国家政策覆盖统计"
      >
        <div>
          <span>生效国家政策 {{ countryOverview?.coverage.active_policy_count ?? 0 }}</span>
        </div>
        <div>
          <span>允许联系人补全 {{ countryOverview?.coverage.contact_enrichment_allowed_count ?? 0 }}</span>
        </div>
      </div>

      <div class="country-policy-grid">
        <article
          class="country-panel"
          aria-label="生效国家政策"
        >
          <header>
            <h3>当前生效政策</h3>
            <span>{{ activeCountryPolicies.length }} 个国家</span>
          </header>
          <div
            v-if="countryLoading"
            class="empty compact-empty"
          >
            正在读取国家政策…
          </div>
          <div
            v-else-if="!activeCountryPolicies.length"
            class="empty compact-empty"
          >
            尚无已激活国家政策
          </div>
          <ol
            v-else
            class="country-list"
          >
            <li
              v-for="active in activeCountryPolicies"
              :key="active.version.country_key"
              :class="{ selected: selectedCountry === active.version.country }"
            >
              <header>
                <div>
                  <strong>{{ active.version.country }}</strong>
                  <code>{{ active.version.country_key }}</code>
                </div>
                <span>v{{ active.version.version_number }}</span>
              </header>
              <p>public_research：{{ active.version.public_research_allowed ? "允许" : "禁止" }}</p>
              <p>contact_enrichment：{{ active.version.contact_enrichment_allowed ? "允许" : "禁止" }}</p>
              <p>cold_b2b_email：{{ active.version.cold_b2b_email_allowed ? "允许" : "禁止" }}</p>
              <p>
                激活时间
                <time :datetime="active.activation.activated_at">{{ displayTime(active.activation.activated_at) }}</time>
              </p>
              <p>激活者 {{ active.activation.activated_by }}</p>
              <div class="country-actions">
                <button
                  type="button"
                  @click="selectCountry(active.version.country)"
                >
                  查看历史
                </button>
                <button
                  type="button"
                  @click="beginCountryRevision(active)"
                >
                  修订此政策
                </button>
              </div>
              <details>
                <summary>查看字段来源</summary>
                <ul class="source-list">
                  <li
                    v-for="field in countryPolicyFields"
                    :key="field.key"
                  >
                    <code>{{ field.key }}</code>
                    {{ active.version.field_provenance[field.key].source_type }} /
                    {{ active.version.field_provenance[field.key].source_id }}
                  </li>
                </ul>
              </details>
            </li>
          </ol>
        </article>

        <article class="country-panel country-form-panel">
          <header>
            <div>
              <h3>{{ revisionBase ? "修订国家政策" : "新建国家政策" }}</h3>
              <p
                v-if="revisionBase"
                class="base-facts"
              >
                基准版本 {{ revisionBase.country_policy_version_id }}<br>
                基准哈希 {{ revisionBase.content_hash }}
              </p>
            </div>
            <span>仅创建候选</span>
          </header>
          <form
            aria-label="国家政策候选表单"
            @submit.prevent="submitCountryProposal"
          >
            <label for="policy-country">国家显示名
              <input
                id="policy-country"
                v-model="countryForm.country"
                name="country"
                maxlength="64"
                autocomplete="off"
                required
              >
            </label>

            <div class="boolean-grid">
              <label
                v-for="field in countryBooleanFields"
                :key="field.key"
                :for="`policy-${field.key}`"
              >{{ field.label }} <code>{{ field.key }}</code>
                <select
                  :id="`policy-${field.key}`"
                  v-model="countryForm[field.key]"
                  :name="field.key"
                  required
                >
                  <option
                    :value="null"
                    disabled
                  >
                    请选择已核验事实
                  </option>
                  <option :value="true">
                    允许 / 是
                  </option>
                  <option :value="false">
                    禁止 / 否
                  </option>
                </select>
              </label>
            </div>

            <label for="policy-opt-out">退订期限（天） <code>opt_out_deadline_days</code>
              <input
                id="policy-opt-out"
                v-model="countryForm.opt_out_deadline_days"
                name="opt_out_deadline_days"
                type="number"
                min="1"
                max="365"
                inputmode="numeric"
              >
            </label>
            <label for="policy-requirements">附加要求代码 <code>requirements</code>
              <textarea
                id="policy-requirements"
                v-model="countryForm.requirements"
                name="requirements"
                rows="2"
                placeholder="用逗号或换行分隔固定 action code"
              />
            </label>
            <label for="policy-notes">核验说明
              <textarea
                id="policy-notes"
                v-model="countryForm.notes"
                name="notes"
                rows="3"
                maxlength="4000"
                required
              />
            </label>

            <fieldset class="source-fieldset">
              <legend>九项决策字段安全来源</legend>
              <p>这里只展示并提交安全 source ID/type，不展示原始网页正文。</p>
              <div
                v-for="field in countryPolicyFields"
                :key="field.key"
                class="source-row"
              >
                <label :for="`source-type-${field.key}`">{{ field.label }} 来源类型
                  <select
                    :id="`source-type-${field.key}`"
                    v-model="countryForm.field_sources[field.key].source_type"
                    :name="`source_type_${field.key}`"
                    @change="normalizeCountrySource(field.key)"
                  >
                    <option value="employee_input">
                      employee_input
                    </option>
                    <option value="upload">
                      upload
                    </option>
                    <option value="web_page">
                      web_page
                    </option>
                  </select>
                </label>
                <label :for="`source-id-${field.key}`">安全 source ID
                  <input
                    :id="`source-id-${field.key}`"
                    v-model="countryForm.field_sources[field.key].source_id"
                    :name="`source_${field.key}`"
                    maxlength="200"
                    autocomplete="off"
                    required
                  >
                </label>
                <template v-if="countryForm.field_sources[field.key].source_type === 'web_page'">
                  <label :for="`source-url-${field.key}`">HTTPS 来源 URL
                    <input
                      :id="`source-url-${field.key}`"
                      v-model="countryForm.field_sources[field.key].source_url"
                      :name="`source_url_${field.key}`"
                      type="url"
                      required
                    >
                  </label>
                  <label :for="`page-hash-${field.key}`">页面哈希
                    <input
                      :id="`page-hash-${field.key}`"
                      v-model="countryForm.field_sources[field.key].page_hash"
                      :name="`page_hash_${field.key}`"
                      minlength="64"
                      maxlength="64"
                      required
                    >
                  </label>
                </template>
              </div>
            </fieldset>

            <p
              v-if="missingCountrySources.length"
              class="source-warning"
              role="status"
            >
              每个决策字段都必须填写安全来源（尚缺 {{ missingCountrySources.length }} 项）。
            </p>
            <div
              v-if="countryLiveDiff.length"
              class="diff-box"
              aria-label="国家政策候选差异"
            >
              <strong>提交前 before / after</strong>
              <ul>
                <li
                  v-for="item in countryLiveDiff"
                  :key="item"
                >
                  {{ item }}
                </li>
              </ul>
            </div>
            <div
              v-if="countrySubmitError"
              class="form-message error"
              role="alert"
            >
              {{ countrySubmitError }}
            </div>
            <div
              v-if="countryAccepted"
              class="form-message success"
              role="status"
            >
              <strong>国家政策候选已创建，等待审批</strong>
              <span>候选 {{ countryAccepted.country_policy_version_id }}</span>
              <span>Run {{ countryAccepted.run_id }}</span>
            </div>
            <button
              class="btn-primary submit-button"
              type="submit"
              :disabled="countryLoading || countrySubmitting || !countryFormValid"
            >
              {{ countrySubmitting ? "正在提交候选…" : "提交国家政策审批候选" }}
            </button>
          </form>
        </article>
      </div>

      <article
        class="country-panel country-history"
        aria-label="国家政策版本历史"
      >
        <header>
          <div>
            <h3>版本、审批与应用状态</h3>
            <p>{{ selectedCountry ?? "选择一个生效国家查看" }}</p>
          </div>
          <span>{{ countryVersions.length }} 个版本</span>
        </header>
        <div
          v-if="countryHistoryLoading"
          class="empty compact-empty"
        >
          正在读取版本历史…
        </div>
        <div
          v-else-if="!countryVersions.length"
          class="empty compact-empty"
        >
          尚无可显示的国家政策历史
        </div>
        <ol
          v-else
          class="country-history-list"
        >
          <li
            v-for="item in countryVersions"
            :key="item.version.country_policy_version_id"
          >
            <header>
              <div>
                <strong>v{{ item.version.version_number }}</strong>
                <code>{{ item.version.country_policy_version_id }}</code>
              </div>
              <span
                class="status"
                :class="`state-${item.approval_state}`"
              >
                {{ countryStatusLabels[item.approval_state] }}
              </span>
            </header>
            <p>提案人 {{ item.version.proposed_by }} · <time :datetime="item.version.proposed_at">{{ displayTime(item.version.proposed_at) }}</time></p>
            <p v-if="item.approval_id">
              审批 {{ item.approval_id }}
            </p>
            <p v-if="item.approval_decided_by && item.approval_decided_at">
              决定人 {{ item.approval_decided_by }} · <time :datetime="item.approval_decided_at">{{ displayTime(item.approval_decided_at) }}</time>
            </p>
            <ul
              v-if="historyDifferences(item.version).length"
              class="history-diff"
            >
              <li
                v-for="difference in historyDifferences(item.version)"
                :key="difference"
              >
                {{ difference }}
              </li>
            </ul>
            <details>
              <summary>字段来源（安全引用）</summary>
              <ul class="source-list">
                <li
                  v-for="field in countryPolicyFields"
                  :key="field.key"
                >
                  <code>{{ field.key }}</code>
                  {{ item.version.field_provenance[field.key].source_type }} /
                  {{ item.version.field_provenance[field.key].source_id }}
                </li>
              </ul>
            </details>
            <p
              v-if="item.application_error_code"
              class="history-error"
            >
              {{ countryErrorLabels[item.application_error_code] }}
            </p>
          </li>
        </ol>
      </article>
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
.country-policy-workspace { display: grid; gap: var(--space4); margin-bottom: var(--space4); overflow: hidden; }
.policy-principle, .readiness-banner { margin: 0; }
.coverage-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--space3); }
.coverage-grid > div { display: flex; justify-content: space-between; gap: var(--space2); align-items: center; min-width: 0; border: 1px solid var(--border); border-radius: var(--radius); padding: var(--space3); }
.coverage-grid span { color: var(--text-secondary); font-size: 12px; }
.coverage-grid strong { font-size: 22px; }
.country-policy-grid { display: grid; grid-template-columns: minmax(280px, .8fr) minmax(480px, 1.2fr); gap: var(--space4); align-items: start; min-width: 0; }
.country-panel { min-width: 0; border: 1px solid var(--border); border-radius: var(--radius); padding: var(--space3); }
.country-panel > header { display: flex; justify-content: space-between; gap: var(--space2); align-items: flex-start; padding-bottom: var(--space3); border-bottom: 1px solid var(--border); }
.country-panel h3 { font-size: 15px; }
.country-panel header span, .country-panel header p { color: var(--text-secondary); font-size: 11px; overflow-wrap: anywhere; }
.country-list, .country-history-list { display: grid; gap: var(--space3); margin-top: var(--space3); list-style: none; }
.country-list > li, .country-history-list > li { min-width: 0; border: 1px solid var(--border); border-radius: var(--radius); padding: var(--space3); }
.country-list > li.selected { border-color: var(--action); }
.country-list li > header, .country-history-list li > header { display: flex; justify-content: space-between; gap: var(--space2); }
.country-list header div, .country-history-list header div { display: grid; min-width: 0; gap: var(--space1); }
.country-list code, .country-history-list code, .base-facts { overflow-wrap: anywhere; }
.country-list p, .country-history-list p { margin-top: var(--space1); color: var(--text-secondary); font-size: 11px; overflow-wrap: anywhere; }
.country-actions { display: flex; flex-wrap: wrap; gap: var(--space2); margin-top: var(--space3); }
.country-list details, .country-history-list details { margin-top: var(--space3); }
.source-list { display: grid; gap: var(--space1); margin: var(--space2) 0 0; padding: 0; list-style: none; color: var(--text-secondary); font-size: 10px; overflow-wrap: anywhere; }
.boolean-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--space3); }
.boolean-grid code { font-size: 9px; overflow-wrap: anywhere; }
select { width: 100%; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface); padding: 8px 10px; color: var(--text-primary); }
.source-fieldset { display: grid; gap: var(--space3); min-width: 0; border: 1px solid var(--border); border-radius: var(--radius); padding: var(--space3); }
.source-fieldset legend { padding: 0 var(--space1); font-size: 12px; font-weight: 700; }
.source-fieldset > p, .source-warning { color: var(--text-secondary); font-size: 11px; }
.source-row { display: grid; grid-template-columns: minmax(130px, .6fr) minmax(180px, 1fr); gap: var(--space2); min-width: 0; border-top: 1px solid var(--border); padding-top: var(--space2); }
.source-warning { color: var(--danger); }
.country-history-list { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.history-diff { display: grid; gap: var(--space1); margin: var(--space2) 0 0 18px; color: var(--text-secondary); font-size: 11px; }
@media (max-width: 900px) { .settings-grid { grid-template-columns: 1fr; } }
@media (max-width: 900px) { .country-policy-grid { grid-template-columns: 1fr; } }
@media (max-width: 700px) {
  .field-row, .version-list, .boolean-grid, .country-history-list, .coverage-grid, .source-row { grid-template-columns: 1fr; }
  .settings-card { padding: var(--space3); }
  .coverage-grid > div { align-items: flex-start; }
}
</style>
