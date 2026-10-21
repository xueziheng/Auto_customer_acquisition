<script setup lang="ts">
/* global URL */
import { inject, onMounted, ref, watch } from "vue";
import type { components } from "../api/api";
import { apiClient, createApiClient } from "../api/client";
import { useQuoteRequestScope } from "../views/costing-quotes/quote-request-scope";

type Detail = components["schemas"]["ScreeningDetailView"];
type Kind = components["schemas"]["ScreeningEvidence"]["kind"];
const props = defineProps<{ accountId: string }>();
const client = inject<ReturnType<typeof createApiClient>>("tradeos-api-client", apiClient);
const detail = ref<Detail | null>(null);
const loading = ref(false);
const error = ref<string | null>(null);
const labels: Record<Kind, string> = {
  business_match: "业务匹配", recent_need: "近期需求", contact_role: "联系人角色",
  independent_confirmation: "独立来源佐证", human_review: "历史人工复核",
};
const tiers = { basic: "基础", standard: "标准", advanced: "进阶" };
const statusLabels: Record<string, string> = {
  ready_for_enrichment: "自动补全，无需逐条审核",
  ready_for_review: "历史筛选记录",
  needs_evidence: "历史筛选记录",
};
const gate = useQuoteRequestScope(client, () => [props.accountId], reset);
function reset(): void {
  detail.value = null; error.value = null; loading.value = false;
}
function safeError(code: number): string {
  if (code === 403 || code === 401) return "当前身份无权读取该企业的联系人处理方式";
  return "联系人处理方式暂不可用，请刷新；企业和联系人资料仍可独立查看";
}
function safeUrl(value: string | null | undefined): string | null {
  try { const url = new URL(value ?? ""); return ["https:", "http:"].includes(url.protocol) ? url.href : null; }
  catch { return null; }
}
async function refresh(): Promise<void> {
  const op = gate.begin("load"); if (!op?.valid()) return;
  loading.value = true; error.value = null;
  try {
    const result = await client.GET("/prospects/accounts/{account_id}/screening", {
      params: { path: { account_id: props.accountId } }, signal: op.signal,
    });
    if (!op.valid()) return;
    if (result.response.status === 200 && result.data && Array.isArray(result.data.evidence)) {
      detail.value = result.data;
    }
    else { detail.value = null; error.value = safeError(result.response.status); }
  } catch { if (op.valid()) { detail.value = null; error.value = safeError(503); } }
  finally { if (op.valid()) loading.value = false; }
}
watch(() => props.accountId, () => { reset(); void refresh(); });
onMounted(() => void refresh());
</script>

<template>
  <section
    class="screening-panel"
    aria-label="联系人处理方式"
  >
    <header>
      <h3>联系人处理方式</h3><button
        type="button"
        :disabled="loading"
        @click="refresh"
      >
        刷新处理方式
      </button>
    </header>
    <p>在有效计划与已配置服务商范围内自动补全联系人；邮箱通过可达性验证后才可进入发送序列。</p>
    <p
      v-if="loading"
      role="status"
    >
      正在读取联系人处理方式…
    </p>
    <p
      v-if="error"
      role="alert"
    >
      {{ error }}
    </p>
    <template v-if="detail">
      <strong v-if="detail.assessment">{{ tiers[detail.assessment.tier] }} · {{ statusLabels[detail.assessment.status] ?? "状态待更新" }}</strong>
      <p v-else>
        暂未取得联系人处理方式，企业资料仍可查看。
      </p>
      <details v-if="detail.evidence.length">
        <summary>查看已记录来源（{{ detail.evidence.length }}）</summary>
        <article
          v-for="(item, index) in detail.evidence"
          :key="`${item.source_ref}:${item.kind}:${index}`"
        >
          <strong>{{ labels[item.kind] }}</strong><blockquote>{{ item.source_quote }}</blockquote>
          <a
            v-if="safeUrl(item.source_url)"
            :href="safeUrl(item.source_url)!"
            target="_blank"
            rel="noopener noreferrer"
          >查看原始来源 ↗</a>
          <dl>
            <dt>来源信号</dt><dd>{{ item.source_ref }}</dd>
            <dt>事件发生时间</dt><dd>{{ item.occurred_at ?? "未知" }}</dd>
            <dt>记录时间</dt><dd>{{ item.recorded_at }}</dd>
          </dl>
        </article>
      </details>
    </template>
  </section>
</template>

<style scoped>
.screening-panel { display: grid; gap: 12px; padding: 16px; border-block: 1px solid var(--border); overflow-wrap: anywhere; }
header { display: flex; justify-content: space-between; align-items: center; gap: 12px; } h3 { font-size: 15px; }
p, dt { color: var(--text-secondary); } [role=alert] { color: var(--warning); }
dl { display: grid; grid-template-columns: minmax(100px, .25fr) 1fr; gap: 8px 12px; } dd { min-width: 0; }
article { display: grid; gap: 10px; margin-top: 10px; padding: 12px; border: 1px solid var(--border); border-radius: 8px; min-width: 0; }
blockquote { margin: 0; white-space: pre-wrap; padding: 10px; background: var(--canvas); }
summary { cursor: pointer; } button { padding: 8px 12px; } a { color: var(--action); }
@media (max-width: 760px) { dl { grid-template-columns: 1fr; } header { align-items: start; flex-wrap: wrap; } }
</style>
