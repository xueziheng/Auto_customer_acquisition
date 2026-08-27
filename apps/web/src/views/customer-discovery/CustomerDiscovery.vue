<script setup lang="ts">
/* global Response */
import { computed, inject, onMounted, ref } from "vue";

import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";
import ResearchEvidenceCard from "../../components/ResearchEvidenceCard.vue";
import ResearchResultsStatus from "../../components/ResearchResultsStatus.vue";

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
const actionMessage = ref<string | null>(null);
const actionBusy = ref(false);
const hypothesisId = ref("");
const campaignId = ref("");
const roleHintsText = ref("procurement, sourcing");
const assessmentRef = ref("");
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

function roleHints(): string[] {
  return Array.from(
    new Set(
      roleHintsText.value
        .split(",")
        .map((item) => item.trim())
        .filter(Boolean),
    ),
  ).slice(0, 10);
}

async function startDiscovery(): Promise<void> {
  if (actionBusy.value) return;
  actionMessage.value = null;
  const hints = roleHints();
  if (!hypothesisId.value || !campaignId.value || !assessmentRef.value || hints.length === 0) {
    actionMessage.value = "请填写假设、Campaign、角色线索和正当利益评估引用";
    return;
  }
  actionBusy.value = true;
  const { data, response } = await client.POST("/prospects/discoveries", {
    body: {
      hypothesis_id: hypothesisId.value.trim(),
      campaign_id: campaignId.value.trim(),
      role_hints: hints,
      assessment_ref: assessmentRef.value.trim(),
    },
  });
  actionBusy.value = false;
  if (response.status === 200 && data) {
    actionMessage.value = `账户发现任务已创建：${data.run_id}`;
    return;
  }
  if (response.status === 403) {
    actionMessage.value = "当前角色不能启动账户发现";
    return;
  }
  actionMessage.value = response.status === 503 ? retryNotice(response) : "账户发现任务创建失败";
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
    <div class="page-head">
      <h1>客户发现</h1>
      <span class="meta">企业消歧、法律依据、可达性验证与 Campaign 入组</span>
    </div>


    <ResearchResultsStatus />
    <section
      class="safe-banner"
      aria-label="只研究入口"
    >
      <p>
        只研究公开来源：无需 Campaign，不补全联系人、不验证邮箱、不发送、不报价。<RouterLink to="/commands">
          到指挥中心创建研究提案 →
        </RouterLink>
      </p>
    </section>

    <section
      class="discovery-command"
      aria-label="启动账户发现"
    >
      <div>
        <h2>从已通过门槛的需求假设开始</h2>
        <p>任务只会把 Provider 结果写入潜客域；只有已验证邮箱会进入 Campaign。</p>
      </div>
      <form @submit.prevent="startDiscovery">
        <label>
          需求假设 ID
          <input
            v-model="hypothesisId"
            autocomplete="off"
            placeholder="hyp_…"
          >
        </label>
        <label>
          Campaign ID
          <input
            v-model="campaignId"
            autocomplete="off"
            placeholder="cmp_…"
          >
        </label>
        <label>
          联系人角色线索
          <input
            v-model="roleHintsText"
            autocomplete="off"
            aria-describedby="role-hint-help"
          >
          <small id="role-hint-help">用英文逗号分隔，最多 10 项</small>
        </label>
        <label>
          正当利益评估引用
          <input
            v-model="assessmentRef"
            autocomplete="off"
            placeholder="LIA 文档引用"
          >
        </label>
        <button
          class="btn-primary"
          type="submit"
          :disabled="actionBusy"
        >
          {{ actionBusy ? "正在创建…" : "启动账户发现" }}
        </button>
      </form>
      <div
        v-if="actionMessage"
        class="safe-banner"
        role="status"
      >
        {{ actionMessage }}
      </div>
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
          <button
            type="button"
            @click="loadAccounts"
          >
            刷新
          </button>
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
          尚无潜在企业
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
            <dt>企业类型</dt><dd>{{ detail.account.entity_type ?? "—" }}</dd>
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
                  <dt>法律依据</dt><dd>{{ point.legal_basis }} · {{ point.contact_type }}</dd>
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
