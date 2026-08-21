<script setup lang="ts">
/* global document, KeyboardEvent */
import { inject, onBeforeUnmount, onMounted, ref } from "vue";

import { apiClient, createApiClient } from "../api/client";
import type { components } from "../api/api";

type ApiClient = ReturnType<typeof createApiClient>;
type IdentityView = components["schemas"]["IdentityView"];

const client = inject<ApiClient>("tradeos-api-client", apiClient);

const identities = ref<IdentityView[]>([]);
const listLoading = ref(true);
const listError = ref<string | null>(null);
const checkFeedback = ref<string | null>(null);
const checkBusyId = ref<string | null>(null);
let listVersion = 0;
let checkVersion = 0;
// 幂等 request_key 按 identity 隔离：重试只复用同一个 identity 的 key
const requestKeys = new Map<string, string>();

const stateLabels: Record<string, string> = {
  created: "已创建",
  auth_pending: "认证待修复",
  warming: "预热中",
  active: "活跃",
  throttled: "已限流",
  suspended: "已暂停",
  retired: "已退役",
};

async function loadIdentities(): Promise<void> {
  const version = ++listVersion;
  listLoading.value = true;
  listError.value = null;
  const { data, response } = await client.GET("/crm/sending-identities", {
    params: { query: { limit: 200 } },
  });
  if (version !== listVersion) return;
  listLoading.value = false;
  if (response.status === 403) {
    listError.value = "当前账号无法查看发件身份";
    return;
  }
  if (response.status !== 200) {
    listError.value = "发件身份加载失败，请刷新后重试";
    return;
  }
  identities.value = data ?? [];
}

function statusLabel(value: string): string {
  return stateLabels[value] ?? value;
}

function warmupLabel(identity: IdentityView): string {
  if (identity.warmup_complete) return "已完成（第 28 / 28 天）";
  const day = identity.warmup_day ?? 0;
  return `第 ${day} / 28 天 · 未完成`;
}

function capacityLabel(identity: IdentityView): string {
  const used = (identity.target_daily_volume ?? 0) - identity.remaining_today;
  return `${used} / ${identity.target_daily_volume ?? 0}`;
}

function reputationLabel(identity: IdentityView): string {
  const reputation = identity.reputation;
  if (!reputation) return "暂无样本";
  return `硬退信率 ${reputation.hard_bounce_rate} · 投诉率 ${reputation.complaint_rate}（代码确定性计算）`;
}

function newRequestKey(): string {
  const random = Math.random().toString(36).slice(2);
  return `auth-check-${Date.now()}-${random}`;
}

async function requestCheck(identityId: string): Promise<void> {
  if (checkBusyId.value !== null) return; // 双击锁：任一检查在途时忽略再次点击
  checkBusyId.value = identityId;
  checkFeedback.value = null;
  const version = ++checkVersion;
  // 每个 identity 独立生成 request_key；该 identity 重试复用同一个 key。
  let key = requestKeys.get(identityId);
  if (key === undefined) {
    key = newRequestKey();
    requestKeys.set(identityId, key);
  }
  const { response } = await client.POST(
    "/crm/sending-identities/{identity_id}/authentication-checks",
    {
      params: { path: { identity_id: identityId } },
      body: { request_key: key },
    },
  );
  if (version !== checkVersion) return; // stale response 忽略
  checkBusyId.value = null;
  if (response.status === 200) {
    checkFeedback.value = "认证检查已提交";
    return;
  }
  if (response.status === 403) {
    checkFeedback.value = "当前账号无法查看发件身份";
    return;
  }
  checkFeedback.value = "认证检查提交失败，请稍后重试";
}

function onEscape(event: KeyboardEvent): void {
  if (event.key === "Escape") {
    checkFeedback.value = null;
  }
}

onMounted(() => {
  void loadIdentities();
  document.addEventListener("keydown", onEscape);
});

onBeforeUnmount(() => {
  document.removeEventListener("keydown", onEscape);
});
</script>

<template>
  <div class="shell">
    <div class="page-head">
      <h1>发件身份中心</h1>
      <span class="meta">仅展示后端已返回的事实</span>
    </div>
    <div v-if="listLoading" class="state" role="status">正在加载发件身份…</div>
    <div v-else-if="listError" class="state" role="alert">{{ listError }}</div>
    <div v-else-if="identities.length === 0" class="state">
      暂无可用于 Campaign 的发件身份
    </div>
    <section v-else class="grid" aria-label="发件身份卡片">
      <article v-for="identity in identities" :key="identity.identity_id" class="card">
        <div class="name">{{ identity.domain }}</div>
        <div class="addr">{{ identity.address }}</div>
        <div v-if="identity.auth" class="auth-grid" aria-label="认证状态">
          <span class="auth-item" :class="identity.auth.spf_passed ? 'pass' : 'fail'">
            {{ identity.auth.spf_passed ? "✓" : "✕" }} SPF
            {{ identity.auth.spf_passed ? "通过" : "未通过" }}
          </span>
          <span class="auth-item" :class="identity.auth.dkim_passed ? 'pass' : 'fail'">
            {{ identity.auth.dkim_passed ? "✓" : "✕" }} DKIM
            {{ identity.auth.dkim_passed ? "通过" : "未通过" }}
          </span>
          <span class="auth-item" :class="identity.auth.dmarc_passed ? 'pass' : 'fail'">
            {{ identity.auth.dmarc_passed ? "✓" : "✕" }} DMARC
            {{ identity.auth.dmarc_passed ? "通过" : "未通过" }}
          </span>
        </div>
        <div v-else class="auth-grid" aria-label="认证状态">
          <span class="auth-item">认证尚未完成</span>
        </div>
        <dl class="kv">
          <dt>预热</dt><dd>{{ warmupLabel(identity) }}</dd>
          <dt>每日容量</dt><dd>{{ capacityLabel(identity) }}</dd>
          <dt>信誉</dt><dd>{{ reputationLabel(identity) }}</dd>
          <dt>状态</dt><dd><span class="status">{{ statusLabel(identity.state) }}</span></dd>
        </dl>
        <button
          :disabled="checkBusyId !== null"
          :aria-label="`对 ${identity.domain} 重新检查认证`"
          @click="requestCheck(identity.identity_id)"
        >
          重新检查认证
        </button>
      </article>
    </section>
    <div v-if="checkFeedback" class="feedback" role="status">
      <span aria-hidden="true">✓</span>
      <div>{{ checkFeedback }}</div>
    </div>
  </div>
</template>

<style scoped>
.state {
  color: var(--text-secondary);
  padding: var(--space4) 0;
}
.grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: var(--space4);
  overflow-y: auto;
  flex: 1;
  min-height: 0;
  align-content: start;
  padding-bottom: var(--space4);
}
.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: var(--space4);
  display: flex;
  flex-direction: column;
  gap: var(--space3);
}
.card .name {
  font-weight: 600;
}
.card .addr {
  color: var(--text-secondary);
  font-size: 12px;
  word-break: break-all;
}
.auth-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: var(--space2);
}
.auth-item {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 6px 8px;
  font-size: 12px;
  display: flex;
  align-items: center;
  gap: 6px;
}
.auth-item.pass {
  color: var(--fact);
  border-color: var(--fact);
  background: var(--fact-soft);
}
.auth-item.fail {
  color: var(--danger);
  border-color: var(--danger);
  background: var(--danger-soft);
}
.kv {
  display: grid;
  grid-template-columns: 110px 1fr;
  gap: var(--space2) var(--space4);
  font-size: 13px;
}
.kv dt {
  color: var(--text-secondary);
}
.kv dd {
  margin: 0;
}
.feedback {
  border: 1px solid var(--fact);
  background: var(--fact-soft);
  color: var(--fact);
  border-radius: var(--radius);
  padding: var(--space3) var(--space4);
  display: flex;
  gap: var(--space3);
  align-items: flex-start;
  font-size: 13px;
}
@media (max-width: 1240px) {
  .grid {
    grid-template-columns: repeat(2, 1fr);
  }
}
</style>
