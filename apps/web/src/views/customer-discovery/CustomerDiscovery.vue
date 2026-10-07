<script setup lang="ts">
import { codeLabel } from "../../components/displayLabels";
/* global Response */
import { computed, inject, onMounted, ref } from "vue";

import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";
import ResearchEvidenceCard from "../../components/ResearchEvidenceCard.vue";

type ApiClient = ReturnType<typeof createApiClient>;
type Account = components["schemas"]["ResearchProspectAccountView"];
type AccountDetail = components["schemas"]["ResearchProspectAccountDetailView"];

const client = inject<ApiClient>("tradeos-api-client", apiClient);
const accounts = ref<Account[]>([]);
const selectedId = ref<string | null>(null);
const detail = ref<AccountDetail | null>(null);
const loading = ref(true);
const detailLoading = ref(false);
const error = ref<string | null>(null);
let listVersion = 0;
let detailVersion = 0;

const selectedAccount = computed(
  () => accounts.value.find((item) => item.account_id === selectedId.value) ?? null,
);

const verifiedCount = computed(
  () =>
    detail.value?.contacts.reduce(
      (total, contact) =>
        total +
        contact.contact_points.filter(
          (point) => point.contact_point.verification === "verified",
        ).length,
      0,
    ) ?? 0,
);

function retryNotice(response: Response): string {
  const value = response.headers.get("retry-after");
  return value && /^\d+$/.test(value)
    ? `服务暂不可用，请在 ${value} 秒后刷新`
    : "服务暂不可用，请稍后刷新";
}

async function loadAccounts(): Promise<void> {
  const version = ++listVersion;
  loading.value = true;
  error.value = null;
  const { data, response } = await client.GET("/prospects/accounts", {
    params: { query: { limit: 200 } },
  });
  if (version !== listVersion) return;
  loading.value = false;
  if (response.status === 403) {
    error.value = "当前角色无权读取潜在企业与联系人资料";
    return;
  }
  if (response.status === 503) {
    error.value = retryNotice(response);
    return;
  }
  if (response.status !== 200) {
    error.value = "潜在企业加载失败";
    return;
  }
  accounts.value = data ?? [];
  if (selectedId.value && !accounts.value.some((item) => item.account_id === selectedId.value)) {
    selectedId.value = null;
    detail.value = null;
  }
}

async function selectAccount(accountId: string): Promise<void> {
  selectedId.value = accountId;
  detail.value = null;
  detailLoading.value = true;
  error.value = null;
  const version = ++detailVersion;
  const { data, response } = await client.GET("/prospects/accounts/{account_id}", {
    params: { path: { account_id: accountId } },
  });
  if (version !== detailVersion || selectedId.value !== accountId) return;
  detailLoading.value = false;
  if (response.status === 200) {
    detail.value = data ?? null;
    return;
  }
  if (response.status === 403) {
    error.value = "当前角色无权读取联系人资料";
    return;
  }
  error.value = response.status === 503 ? retryNotice(response) : "企业详情加载失败";
}

function verificationLabel(value: string): string {
  return (
    {
      verified: "已验证，可入组",
      invalid: "无效，禁止入组",
      risky: "风险地址，禁止入组",
      unverified: "未验证，禁止入组",
    }[value] ?? value
  );
}

onMounted(() => void loadAccounts());
</script>

<template>
  <div class="shell discovery-shell">
    <div class="page-head compact-head">
      <div>
        <p class="eyebrow">第 1 步</p>
        <h1>自动找客户</h1>
      </div>
      <span class="meta">系统自动核验公开企业资料与可用邮箱</span>
    </div>

    <section class="automation-note" aria-label="自动找客户说明">
      <strong>无需手动操作</strong>
      <span>在“自动发邮件”设置目标市场、产品和发件邮箱后，系统会持续寻找并核验客户；只有验证通过的邮箱才会进入发送任务。</span>
    </section>

    <section
      class="discovery-workbench"
      aria-label="潜在企业与联系人"
    >
      <div
        class="account-list"
        :aria-busy="loading ? 'true' : undefined"
      >
        <div class="panel-head">
          <div>
            <h2>潜在企业</h2>
            <span class="meta">{{ accounts.length }} 家</span>
          </div>
        </div>
        <div
          v-if="loading"
          class="state"
          role="status"
        >
          正在加载…
        </div>
        <div
          v-else-if="error"
          class="state"
          role="alert"
        >
          {{ error }}
        </div>
        <div
          v-else-if="accounts.length === 0"
          class="state"
        >
          自动任务启动后，找到的客户会显示在这里
        </div>
        <ol v-else>
          <li
            v-for="account in accounts"
            :key="account.account_id"
            :class="{ selected: selectedId === account.account_id }"
            tabindex="0"
            role="button"
            @click="selectAccount(account.account_id)"
            @keydown.enter="selectAccount(account.account_id)"
          >
            <strong>{{ account.name }}</strong>
            <span>{{ account.country }} · {{ account.website_domain ?? "官网待确认" }}</span>
            <small>{{ account.industry ?? "行业待确认" }}</small>
          </li>
        </ol>
      </div>

      <div class="account-detail">
        <div
          v-if="detailLoading"
          class="state"
          role="status"
        >
          正在加载企业详情…
        </div>
        <div
          v-else-if="!selectedAccount"
          class="state"
        >
          选择企业查看证据、联系人和法律依据
        </div>
        <template v-else-if="detail">
          <div class="detail-head">
            <div>
              <h2>{{ detail.account.name }}</h2>
              <p>{{ detail.account.country }} · {{ detail.account.website_domain }}</p>
              <code>{{ detail.account.account_id }}</code>
            </div>
            <span class="verified-summary">{{ verifiedCount }} 个已验证地址</span>
          </div>

          <dl class="account-facts">
            <dt>企业类型</dt><dd>{{ codeLabel(detail.account.entity_type, "—") }}</dd>
            <dt>行业</dt><dd>{{ detail.account.industry ?? "—" }}</dd>
            <dt>规模线索</dt><dd>{{ detail.account.size_hint ?? "—" }}</dd>
            <dt>来源信号</dt>
            <dd>
              <span v-if="detail.account.source_signal_refs.length === 0">—</span>
              <code
                v-for="signal in detail.account.source_signal_refs"
                :key="signal"
              >{{ signal }}</code>
            </dd>
          </dl>

          <div class="account-evidence">
            <ResearchEvidenceCard
              v-for="signal in detail.account.research_signals"
              :key="signal.signal_id"
              :signal="signal"
            />
            <p v-if="!detail.account.research_signals?.length">
              暂无研究来源归属；历史企业身份仍需核验。
            </p>
          </div>

          <div class="contact-ledger">
            <h3>联系人与联系方式</h3>
            <div
              v-if="detail.contacts.length === 0"
              class="state"
            >
              尚未发现联系人
            </div>
            <article
              v-for="contact in detail.contacts"
              v-else
              :key="contact.contact.contact_id"
            >
              <header>
                <strong>{{ contact.contact.full_name ?? "职务联系人" }}</strong>
                <span>{{ contact.contact.role_title ?? "职位待确认" }}</span>
              </header>
              <div
                v-for="point in contact.contact_points"
                :key="point.contact_point.contact_point_id"
                class="contact-point"
              >
                <div>
                  <span class="contact-value">{{ point.contact_point.value }}</span>
                  <span
                    class="status"
                    :class="point.contact_point.verification"
                  >
                    {{ verificationLabel(point.contact_point.verification) }}
                  </span>
                </div>
                <dl>
                  <dt>法律依据</dt><dd>{{ codeLabel(point.legal_basis) }} · {{ codeLabel(point.contact_type) }}</dd>
                  <dt>来源</dt>
                  <dd>
                    <a
                      v-if="point.source_url"
                      :href="point.source_url"
                      target="_blank"
                      rel="noreferrer"
                    >
                      {{ point.legal_basis_source }}
                    </a>
                    <span v-else>{{ point.legal_basis_source }}</span>
                  </dd>
                  <dt>评估引用</dt><dd>{{ point.assessment_ref ?? "—" }}</dd>
                  <dt>验证观察</dt>
                  <dd>
                    {{ point.contact_point.verification_provider ?? "—" }} ·
                    {{ point.contact_point.verification_checked_at ?? "尚未验证" }}
                  </dd>
                </dl>
              </div>
            </article>
          </div>
        </template>
      </div>
    </section>
  </div>
</template>

<style scoped>
.discovery-shell {
  overflow: auto;
}
.compact-head { align-items: flex-end; }
.compact-head > div { display: flex; align-items: baseline; gap: var(--space3); }
.eyebrow { color: var(--fact); font-size: 11px; font-weight: 800; letter-spacing: .16em; }
.automation-note { display: flex; gap: var(--space3); border-left: 4px solid var(--fact); background: var(--fact-soft); padding: var(--space3) var(--space4); }
.automation-note span { color: var(--text-secondary); }
.account-evidence { padding: 16px; }
.discovery-command,
.discovery-workbench {
  border: 1px solid var(--border);
  background: var(--surface);
  border-radius: var(--radius);
}
.discovery-command {
  padding: var(--space4);
  display: grid;
  grid-template-columns: minmax(220px, 0.8fr) minmax(600px, 2fr);
  gap: var(--space4);
  align-items: end;
}
.discovery-command h2,
.panel-head h2,
.account-detail h2 {
  font-size: 16px;
}
.discovery-command p {
  color: var(--text-secondary);
  margin-top: var(--space2);
}
.discovery-command form {
  display: grid;
  grid-template-columns: repeat(4, minmax(130px, 1fr)) auto;
  gap: var(--space3);
  align-items: end;
}
label {
  display: flex;
  flex-direction: column;
  gap: var(--space1);
  font-size: 12px;
  color: var(--text-secondary);
}
input {
  min-width: 0;
  height: 36px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 0 10px;
  color: var(--text-primary);
  background: var(--surface);
}
small {
  color: var(--text-secondary);
}
.discovery-command .safe-banner {
  grid-column: 1 / -1;
}
.discovery-workbench {
  display: grid;
  grid-template-columns: 360px minmax(0, 1fr);
  min-height: 460px;
}
.account-list {
  border-right: 1px solid var(--border);
  min-width: 0;
}
.panel-head,
.detail-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space3);
  padding: var(--space4);
  border-bottom: 1px solid var(--border);
}
.panel-head > div {
  display: flex;
  align-items: baseline;
  gap: var(--space2);
}
.account-list ol {
  list-style: none;
}
.account-list li {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: var(--space3) var(--space4);
  border-bottom: 1px solid var(--border);
  cursor: pointer;
}
.account-list li:hover,
.account-list li.selected {
  background: var(--fact-soft);
}
.account-list li.selected {
  box-shadow: inset 3px 0 var(--fact);
}
.account-list li span,
.detail-head p {
  color: var(--text-secondary);
}
.account-detail {
  min-width: 0;
}
.state {
  padding: var(--space5);
  color: var(--text-secondary);
}
.verified-summary {
  color: var(--fact);
  font-weight: 600;
}
.account-facts {
  display: grid;
  grid-template-columns: 90px 1fr;
  gap: var(--space2) var(--space4);
  padding: var(--space4);
  border-bottom: 1px solid var(--border);
}
dt {
  color: var(--text-secondary);
}
dd code {
  display: inline-block;
  margin-right: var(--space2);
  color: var(--fact);
}
.contact-ledger {
  padding: var(--space4);
}
.contact-ledger h3 {
  font-size: 14px;
  margin-bottom: var(--space3);
}
article {
  border-top: 1px solid var(--border);
  padding: var(--space3) 0;
}
article header,
.contact-point > div {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space3);
}
article header span {
  color: var(--text-secondary);
}
.contact-point {
  margin-top: var(--space2);
  padding: var(--space3);
  background: var(--canvas);
  border-radius: var(--radius-sm);
}
.contact-value {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.contact-point dl {
  display: grid;
  grid-template-columns: 90px 1fr;
  gap: var(--space1) var(--space3);
  margin-top: var(--space2);
  font-size: 12px;
}
.status.verified {
  color: var(--fact);
  border-color: var(--fact);
  background: var(--fact-soft);
}
.status.invalid,
.status.risky,
.status.unverified {
  color: var(--warning);
  border-color: var(--warning);
  background: var(--warning-soft);
}
@media (max-width: 1100px) {
  .discovery-command {
    grid-template-columns: 1fr;
  }
  .discovery-command form {
    grid-template-columns: repeat(2, 1fr);
  }
}
@media (max-width: 760px) {
  .discovery-workbench {
    grid-template-columns: 1fr;
  }
  .account-list {
    border-right: 0;
    border-bottom: 1px solid var(--border);
  }
  .discovery-command form {
    grid-template-columns: 1fr;
  }
}
</style>
