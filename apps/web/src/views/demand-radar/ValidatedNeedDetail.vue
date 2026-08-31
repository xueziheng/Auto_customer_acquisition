<script setup lang="ts">
/* global HTMLDetailsElement, HTMLElement, KeyboardEvent */
import { inject, onMounted, ref } from "vue";
import { useRoute } from "vue-router";

import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";

type ApiClient = ReturnType<typeof createApiClient>;
type SourcingCase = components["schemas"]["SourcingCaseReadView"];
type ValidatedNeed = components["schemas"]["ValidatedNeedView"];

const client = inject<ApiClient>("tradeos-api-client", apiClient);
const route = useRoute();
const need = ref<ValidatedNeed | null>(null);
const loading = ref(true);
const error = ref<string | null>(null);
const sourcingCase = ref<SourcingCase | null>(null);
const sourcingNotice = ref<string | null>(null);

function fieldLabel(value: string): string {
  return (
    {
      quantity: "数量",
      specification: "规格",
      destination: "目的地",
      required_by: "时间要求",
      target_price: "目标价",
      product_category: "产品品类",
      application: "应用场景",
      size_range: "尺寸范围",
    }[value] ?? value
  );
}

function formatDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleString("zh-CN", { hour12: false });
}

function safeError(status: number): string {
  if (status === 403) return "当前身份无权读取该已验证需求";
  if (status === 400 || status === 404) return "该已验证需求不存在或不可见";
  return "已验证需求加载失败，请稍后重试";
}

function toggleProvenance(event: KeyboardEvent): void {
  if (event.key !== "Enter" && event.key !== " ") return;
  event.preventDefault();
  const summary = event.currentTarget as HTMLElement | null;
  const details = summary?.parentElement as HTMLDetailsElement | null;
  if (details) details.open = !details.open;
}

async function loadNeed(): Promise<void> {
  loading.value = true;
  error.value = null;
  sourcingNotice.value = null;
  const needId = String(route.params.needId ?? "");
  try {
    const result = await client.GET("/demand/needs/{need_id}", {
      params: { path: { need_id: needId } },
    });
    if (result.response.status === 200 && result.data) {
      need.value = result.data;
      try {
        const sourcing = await client.GET("/sourcing-cases", {
          params: { query: { limit: 50 } },
        });
        if (sourcing.response.status !== 200 || !sourcing.data) {
          sourcingCase.value = null;
          sourcingNotice.value = sourcing.response.status === 403
            ? "当前身份无权读取关联寻源 Case"
            : "关联寻源 Case 暂不可读取";
        } else {
          sourcingCase.value = sourcing.data.find(
            (item) => item.need_id === result.data!.need_id,
          ) ?? null;
          if (!sourcingCase.value) sourcingNotice.value = "尚无关联寻源 Case";
        }
      } catch {
        sourcingCase.value = null;
        sourcingNotice.value = "关联寻源 Case 暂不可读取";
      }
    } else {
      error.value = safeError(result.response.status);
    }
  } catch {
    error.value = "无法连接需求服务";
  } finally {
    loading.value = false;
  }
}

onMounted(() => void loadNeed());
</script>

<template>
  <div class="shell need-detail-shell">
    <div class="page-head detail-head">
      <div>
        <RouterLink to="/demand">
          ← 返回需求雷达
        </RouterLink>
        <p class="eyebrow">
          CUSTOMER-VALIDATED EVIDENCE
        </p>
        <h1>已验证需求证据链</h1>
      </div>
      <button
        type="button"
        :disabled="loading"
        @click="loadNeed"
      >
        {{ loading ? "加载中…" : "刷新" }}
      </button>
    </div>

    <div class="safe-banner">
      <span aria-hidden="true">i</span>
      <div>“已验证”只代表客户明确表达过；每个关键字段必须能回到客户原话或人工确认记录。</div>
    </div>
    <div
      v-if="error"
      class="safe-banner danger"
      role="alert"
    >
      {{ error }}
    </div>
    <div
      v-if="loading"
      class="empty"
      role="status"
    >
      正在读取客户原话与 Provenance…
    </div>

    <main
      v-else-if="need"
      class="need-packet"
    >
      <header>
        <div>
          <span class="validated-badge">客户确认</span>
          <h2>{{ need.account_name }}</h2>
          <p>{{ need.account_id }} · {{ need.need_id }}</p>
        </div>
        <div class="completeness">
          <strong>{{ need.completeness }}/5</strong>
          <span>确定性完整度</span>
        </div>
      </header>

      <RouterLink
        v-if="sourcingCase"
        class="sourcing-link"
        :to="`/sourcing/${sourcingCase.case_id}`"
      >
        查看关联寻源 Case（{{ sourcingCase.state }}）
      </RouterLink>
      <p
        v-else-if="sourcingNotice"
        class="muted"
      >
        {{ sourcingNotice }}
      </p>

      <section
        class="summary-grid"
        aria-label="需求摘要"
      >
        <article><span>产品品类</span><strong>{{ need.product_category }}</strong></article>
        <article><span>数量</span><strong>{{ need.quantity ?? "未确认" }}</strong></article>
        <article><span>目的地</span><strong>{{ need.destination ?? "未确认" }}</strong></article>
        <article><span>时间要求</span><strong>{{ need.required_by ?? "未确认" }}</strong></article>
        <article>
          <span>目标价</span>
          <strong v-if="need.target_price">{{ need.target_price.amount }} {{ need.target_price.currency }}</strong>
          <strong v-else>未确认</strong>
        </article>
        <article><span>状态</span><strong>{{ need.status }}</strong></article>
      </section>

      <section class="evidence-section">
        <div class="section-head">
          <div>
            <span class="validated-badge">字段级 Provenance</span>
            <h2>客户原话与确认记录</h2>
          </div>
          <span>{{ need.fields.length }} 个字段</span>
        </div>
        <article
          v-for="field in need.fields"
          :key="`${field.name}:${field.source_ref}`"
          class="field-evidence"
        >
          <header>
            <span>{{ fieldLabel(field.name) }}</span>
            <strong>{{ field.value }}</strong>
          </header>
          <blockquote v-if="field.source_quote">
            “{{ field.source_quote }}”
          </blockquote>
          <p
            v-else
            class="no-quote"
          >
            该字段没有保存客户原话摘录，请通过来源记录核对原件。
          </p>
          <details class="field-provenance">
            <summary @keydown="toggleProvenance">
              查看字段 Provenance
            </summary>
            <dl>
              <div><dt>来源记录</dt><dd><code>{{ field.source_ref }}</code></dd></div>
              <div><dt>人工确认</dt><dd>{{ field.confirmed_by ?? "未人工确认" }}</dd></div>
            </dl>
          </details>
        </article>
      </section>

      <footer>
        <div>
          <strong>创建时间</strong>
          <span>{{ formatDate(need.created_at) }}</span>
        </div>
        <div :class="need.missing_for_sourcing.length ? 'missing' : 'complete'">
          <strong>{{ need.missing_for_sourcing.length ? "寻源前仍缺：" : "关键字段已齐备" }}</strong>
          <span v-if="need.missing_for_sourcing.length">
            {{ need.missing_for_sourcing.map(fieldLabel).join("、") }}
          </span>
        </div>
      </footer>
    </main>
  </div>
</template>

<style scoped>
.need-detail-shell { max-width: 1180px; overflow-y: auto; }
.detail-head { justify-content: space-between; align-items: center; }
.detail-head a { color: var(--fact); text-decoration: none; font-size: 12px; }
.eyebrow { color: var(--fact); font-size: 10px; font-weight: 800; letter-spacing: .12em; margin-top: 5px; }
.empty { color: var(--text-secondary); padding: var(--space5); }
.need-packet { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: var(--space5); display: grid; gap: var(--space5); }
.need-packet > header { display: flex; justify-content: space-between; gap: var(--space4); border-bottom: 1px solid var(--border); padding-bottom: var(--space4); }
.need-packet > header h2 { margin-top: 6px; }
.need-packet > header p { color: var(--text-secondary); font-size: 12px; overflow-wrap: anywhere; }
.sourcing-link { color: var(--fact); font-weight: 700; text-decoration: none; }
.muted { color: var(--text-secondary); font-size: 12px; }
.validated-badge { display: inline-flex; color: var(--action); background: #edf4ff; border: 1px solid #b2ccff; border-radius: 999px; padding: 2px 8px; font-size: 11px; font-weight: 800; }
.completeness { display: grid; text-align: right; }
.completeness strong { color: var(--action); font-size: 24px; }
.completeness span { color: var(--text-secondary); font-size: 11px; }
.summary-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: var(--space3); }
.summary-grid article { background: var(--canvas); border-radius: var(--radius-sm); padding: var(--space3); display: grid; }
.summary-grid span { color: var(--text-secondary); font-size: 11px; }
.evidence-section { display: grid; gap: var(--space3); }
.section-head { display: flex; justify-content: space-between; align-items: center; gap: var(--space3); }
.section-head > div { display: flex; align-items: center; gap: var(--space2); }
.section-head h2 { font-size: 16px; }
.section-head > span { color: var(--text-secondary); font-size: 12px; }
.field-evidence { border: 1px solid var(--border); border-left: 4px solid var(--action); border-radius: var(--radius); padding: var(--space4); display: grid; gap: var(--space3); }
.field-evidence > header { display: flex; justify-content: space-between; gap: var(--space3); }
.field-evidence > header span { color: var(--text-secondary); }
blockquote { background: var(--fact-soft); color: var(--fact); border-radius: var(--radius-sm); padding: var(--space3); font-size: 15px; }
.no-quote { color: var(--warning); background: var(--warning-soft); padding: var(--space3); border-radius: var(--radius-sm); }
.field-evidence dl { display: grid; gap: 6px; }
.field-evidence dl div { display: grid; grid-template-columns: 100px 1fr; gap: var(--space3); }
.field-evidence dt { color: var(--text-secondary); }
code { overflow-wrap: anywhere; }
.need-packet > footer { border-top: 1px solid var(--border); padding-top: var(--space4); display: flex; justify-content: space-between; gap: var(--space4); }
.need-packet > footer div { display: grid; }
.need-packet > footer span { color: var(--text-secondary); }
.missing strong { color: var(--warning); }
.complete strong { color: var(--fact); }
@media (max-width: 700px) {
  .summary-grid { grid-template-columns: repeat(2, 1fr); }
  .need-packet > header, .need-packet > footer { flex-direction: column; }
  .completeness { text-align: left; }
  .section-head, .section-head > div { align-items: flex-start; flex-direction: column; }
}
</style>
