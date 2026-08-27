<script setup lang="ts">
/* global URL */
import { computed, inject, onMounted, ref } from "vue";

import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";
import ResearchEvidenceCard from "../../components/ResearchEvidenceCard.vue";
import ResearchResultsStatus from "../../components/ResearchResultsStatus.vue";

type ApiClient = ReturnType<typeof createApiClient>;
type Signal = components["schemas"]["DemandSignalView"];
type Hypothesis = components["schemas"]["HypothesisView"];
type ValidatedNeed = components["schemas"]["ValidatedNeedView"];
type NeedCluster = components["schemas"]["NeedClusterView"];
type RadarTab = "signals" | "hypotheses" | "needs" | "clusters";

const client = inject<ApiClient>("tradeos-api-client", apiClient);
const activeTab = ref<RadarTab>("signals");
const signals = ref<Signal[]>([]);
const hypotheses = ref<Hypothesis[]>([]);
const needs = ref<ValidatedNeed[]>([]);
const clusters = ref<NeedCluster[]>([]);
const loading = ref(true);
const error = ref<string | null>(null);
const loadedAt = ref<Date | null>(null);

const tabs = computed(() => [
  { id: "signals" as const, label: "需求信号", count: signals.value.length, kind: "来源观察" },
  { id: "hypotheses" as const, label: "需求假设", count: hypotheses.value.length, kind: "推断" },
  { id: "needs" as const, label: "已验证需求", count: needs.value.length, kind: "客户确认" },
  { id: "clusters" as const, label: "需求簇", count: clusters.value.length, kind: "聚合" },
]);

function formatDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

function safeSourceUrl(value: string | null | undefined): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:" ? url.toString() : null;
  } catch {
    return null;
  }
}

function confidenceLabel(value: string): string {
  return ({ high: "高档", medium: "中档", low: "低档" }[value] ?? value);
}

function fieldLabel(value: string): string {
  return (
    {
      quantity: "数量",
      specification: "规格",
      destination: "目的地",
      required_by: "时间要求",
      target_price: "目标价",
      product_category: "产品品类",
    }[value] ?? value
  );
}

async function loadRadar(): Promise<void> {
  loading.value = true;
  error.value = null;
  try {
    const [signalResult, hypothesisResult, needResult, clusterResult] = await Promise.all([
      client.GET("/demand/signals", { params: { query: { limit: 200 } } }),
      client.GET("/demand/hypotheses", { params: { query: { limit: 200 } } }),
      client.GET("/demand/needs", { params: { query: { limit: 200 } } }),
      client.GET("/demand/clusters", { params: { query: { limit: 200 } } }),
    ]);
    const results = [signalResult, hypothesisResult, needResult, clusterResult];
    if (results.some((result) => result.response.status !== 200)) {
      const forbidden = results.some((result) => result.response.status === 403);
      error.value = forbidden ? "当前身份无权读取需求雷达" : "部分需求数据加载失败，请重试";
      return;
    }
    signals.value = signalResult.data ?? [];
    hypotheses.value = hypothesisResult.data ?? [];
    needs.value = needResult.data ?? [];
    clusters.value = clusterResult.data ?? [];
    loadedAt.value = new Date();
  } catch {
    error.value = "无法连接服务，请稍后重试";
  } finally {
    loading.value = false;
  }
}

onMounted(() => void loadRadar());
</script>

<template>
  <div class="shell radar-shell">
    <div class="page-head radar-head">
      <div>
        <p class="eyebrow">
          DEMAND INTELLIGENCE
        </p>
        <h1>需求雷达</h1>
      </div>
      <div class="radar-actions">
        <span
          v-if="loadedAt"
          class="meta"
        >更新于 {{ loadedAt.toLocaleTimeString("zh-CN", { hour12: false }) }}</span>
        <button
          type="button"
          :disabled="loading"
          @click="loadRadar"
        >
          {{ loading ? "加载中…" : "刷新" }}
        </button>
      </div>
    </div>


    <ResearchResultsStatus />
    <section
      class="legend"
      aria-label="信息性质图例"
    >
      <div class="legend-item fact-legend">
        <span />事实：公开来源中的原始观察
      </div>
      <div class="legend-item inference-legend">
        <span />推断：系统基于证据形成的假设
      </div>
      <div class="legend-item validated-legend">
        <span />客户确认：来自沟通或人工确认的已验证需求
      </div>
    </section>

    <div
      v-if="error"
      class="safe-banner danger"
      role="alert"
    >
      {{ error }}
      <button
        type="button"
        @click="loadRadar"
      >
        重试
      </button>
    </div>

    <nav
      class="radar-tabs"
      aria-label="需求雷达数据层"
      style="flex-shrink: 0"
    >
      <button
        v-for="tab in tabs"
        :key="tab.id"
        type="button"
        :class="{ active: activeTab === tab.id }"
        :aria-current="activeTab === tab.id ? 'page' : undefined"
        @click="activeTab = tab.id"
      >
        <span>{{ tab.label }}</span>
        <strong>{{ tab.count }}</strong>
        <small>{{ tab.kind }}</small>
      </button>
    </nav>

    <section
      class="radar-content"
      :aria-busy="loading ? 'true' : undefined"
    >
      <div
        v-if="loading"
        class="empty-state"
        role="status"
      >
        正在读取需求证据链…
      </div>

      <template v-else-if="activeTab === 'signals'">
        <header class="section-head">
          <div><span class="kind-badge fact">来源观察</span><h2>Demand Signal · 需求信号</h2></div>
          <p>信号只是市场中发生过的观察，不等于客户会购买。</p>
        </header>
        <div
          v-if="signals.length"
          class="card-grid"
        >
          <article
            v-for="signal in signals"
            :key="signal.signal_id"
            class="radar-card signal-card"
          >
            <header>
              <div><span class="signal-type">{{ signal.signal_type }}</span><h3>{{ signal.entity_name }}</h3></div>
              <span class="record-status">{{ signal.status }}</span>
            </header>
            <code class="record-id">Signal ID · {{ signal.signal_id }}</code>
            <ResearchEvidenceCard :signal="signal" />
            <div class="fact-block">
              <span>原始观察</span>
              <p>{{ signal.raw_observation }}</p>
            </div>
            <div
              v-if="signal.possible_need"
              class="inference-block"
            >
              <span class="kind-badge inference">推断</span>
              <p>{{ signal.possible_need }}</p>
            </div>
            <footer>
              <span>{{ formatDate(signal.observed_at) }} · {{ signal.source_type }}</span>
              <a
                v-if="safeSourceUrl(signal.source_url)"
                :href="safeSourceUrl(signal.source_url) ?? undefined"
                target="_blank"
                rel="noopener noreferrer"
              >查看原始来源 ↗</a>
              <span v-else>来源记录：{{ signal.source_ref }}</span>
            </footer>
          </article>
        </div>
        <div
          v-else
          class="empty-state"
        >
          暂无需求信号。确认发现提案后，结果会在这里出现。
        </div>
      </template>

      <template v-else-if="activeTab === 'hypotheses'">
        <header class="section-head inference-head">
          <div><span class="kind-badge inference">推断</span><h2>Need Hypothesis · 需求假设</h2></div>
          <p>档位由代码根据证据等级推导；它不是模型输出的概率。</p>
        </header>
        <div
          v-if="hypotheses.length"
          class="card-grid"
        >
          <article
            v-for="hypothesis in hypotheses"
            :key="hypothesis.hypothesis_id"
            class="radar-card hypothesis-card"
          >
            <header>
              <div><span class="kind-badge inference">推断</span><h3>{{ hypothesis.account_name }}</h3></div>
              <span class="tier">置信档位：{{ confidenceLabel(hypothesis.confidence_tier) }}</span>
            </header>
            <p class="record-id">
              Hypothesis ID · {{ hypothesis.hypothesis_id }} · Account ID · {{ hypothesis.account_id }}
            </p>
            <p class="category">
              {{ hypothesis.category }}
            </p>
            <p class="reasoning">
              {{ hypothesis.reasoning }}
            </p>
            <p class="tier-explanation">
              {{ hypothesis.confidence_explanation }}
            </p>
            <details>
              <summary>查看依据（{{ hypothesis.evidence.length }} 条）</summary>
              <ul class="evidence-list">
                <li
                  v-for="evidence in hypothesis.evidence"
                  :key="`${evidence.level}-${evidence.observed_at}-${evidence.summary}`"
                >
                  <div><strong>{{ evidence.level }}</strong><span>{{ formatDate(evidence.observed_at) }}</span></div>
                  <p>{{ evidence.summary }}</p>
                  <a
                    v-if="safeSourceUrl(evidence.source_url)"
                    :href="safeSourceUrl(evidence.source_url) ?? undefined"
                    target="_blank"
                    rel="noopener noreferrer"
                  >查看证据来源 ↗</a>
                  <span v-if="evidence.source_ref">来源记录：{{ evidence.source_ref }}</span>
                </li>
              </ul>
            </details>
          </article>
        </div>
        <div
          v-else
          class="empty-state"
        >
          暂无需求假设。
        </div>
      </template>

      <template v-else-if="activeTab === 'needs'">
        <header class="section-head validated-head">
          <div><span class="kind-badge validated">客户确认</span><h2>Validated Need · 已验证需求</h2></div>
          <p>这里只展示客户明确表达，或有人工确认留痕的需求字段。</p>
        </header>
        <div
          v-if="needs.length"
          class="needs-list"
        >
          <article
            v-for="need in needs"
            :key="need.need_id"
            class="need-card"
          >
            <header>
              <div><span class="kind-badge validated">已验证</span><h3>{{ need.account_name }}</h3><span>{{ need.product_category }}</span></div>
              <div class="completeness">
                <strong>{{ need.completeness }}/5</strong><span>完整度</span>
              </div>
            </header>
            <div class="field-grid">
              <article
                v-for="field in need.fields"
                :key="`${field.name}-${field.source_ref}`"
              >
                <span>{{ fieldLabel(field.name) }}</span>
                <strong>{{ field.value }}</strong>
                <blockquote v-if="field.source_quote">
                  “{{ field.source_quote }}”
                </blockquote>
                <small>
                  来源记录：{{ field.source_ref }}
                  <template v-if="field.confirmed_by"> · 人工确认：{{ field.confirmed_by }}</template>
                </small>
              </article>
            </div>
            <footer>
              <span>状态：{{ need.status }} · {{ formatDate(need.created_at) }}</span>
              <span
                v-if="need.missing_for_sourcing.length"
                class="missing"
              >寻源前仍缺：{{ need.missing_for_sourcing.join("、") }}</span>
              <span
                v-else
                class="complete"
              >关键字段已齐备</span>
              <RouterLink :to="`/demand/needs/${need.need_id}`">
                查看完整证据链 →
              </RouterLink>
            </footer>
          </article>
        </div>
        <div
          v-else
          class="empty-state"
        >
          暂无客户明确验证过的需求。
        </div>
      </template>

      <template v-else>
        <header class="section-head cluster-head">
          <div><span class="kind-badge cluster">聚合</span><h2>Need Cluster · 需求簇</h2></div>
          <p>需求簇来自已验证需求的确定性聚合，不把相似度包装成事实。</p>
        </header>
        <div
          v-if="clusters.length"
          class="cluster-grid"
        >
          <article
            v-for="cluster in clusters"
            :key="cluster.cluster_id"
            class="cluster-card"
          >
            <header><h3>{{ cluster.category }}</h3><strong>{{ cluster.member_count }} 个已验证需求</strong></header>
            <div class="cluster-stats">
              <div><span>覆盖国家</span><strong>{{ cluster.countries.join("、") || "未记录" }}</strong></div>
              <div><span>潜在总量</span><strong>{{ cluster.total_potential_quantity ?? "待补齐" }}</strong></div>
              <div><span>重复需求</span><strong>{{ cluster.recurring_demand === true ? "是" : cluster.recurring_demand === false ? "否" : "未确认" }}</strong></div>
            </div>
            <div
              v-if="cluster.suggests_catalog_product"
              class="catalog-note"
            >
              已达到“建议评估正式目录产品”的确定性门槛
            </div>
            <details>
              <summary>查看成员需求（{{ cluster.member_needs.length }}）</summary>
              <ul>
                <li
                  v-for="member in cluster.member_needs"
                  :key="member.need_id"
                >
                  <strong>{{ member.account_name }}</strong>
                  <span>{{ member.product_category }} · 完整度 {{ member.completeness }}/5</span>
                </li>
              </ul>
            </details>
          </article>
        </div>
        <div
          v-else
          class="empty-state"
        >
          暂无可聚合的已验证需求。
        </div>
      </template>
    </section>
  </div>
</template>

<style scoped>
.radar-shell { overflow: auto; }
.radar-head { justify-content: space-between; padding-top: var(--space3); }
.radar-head > div:first-child { display: flex; align-items: baseline; gap: var(--space3); }
.eyebrow { color: var(--fact); font-size: 11px; font-weight: 800; letter-spacing: .16em; }
.radar-actions { display: flex; align-items: center; gap: var(--space3); }
.legend { display: flex; flex-wrap: wrap; gap: var(--space5); background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: var(--space3) var(--space4); }
.legend-item { display: flex; align-items: center; gap: var(--space2); color: var(--text-secondary); font-size: 12px; }
.legend-item span { width: 10px; height: 10px; border-radius: 2px; }
.fact-legend span { background: var(--fact); }
.inference-legend span { background: var(--inference); }
.validated-legend span { background: var(--action); }
.safe-banner { justify-content: space-between; }
.radar-tabs { display: grid; grid-template-columns: repeat(4, 1fr); gap: var(--space2); }
.radar-tabs button { display: grid; grid-template-columns: 1fr auto; text-align: left; padding: var(--space3) var(--space4); border-radius: var(--radius); border-bottom: 3px solid transparent; }
.radar-tabs button span { font-weight: 700; }
.radar-tabs button strong { font-size: 18px; }
.radar-tabs button small { color: var(--text-secondary); grid-column: 1 / -1; }
.radar-tabs button.active { border-color: var(--fact); background: var(--fact-soft); }
.radar-content { min-height: 360px; background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: var(--space5); }
.section-head { display: flex; justify-content: space-between; align-items: center; gap: var(--space4); padding-bottom: var(--space4); border-bottom: 1px solid var(--border); }
.section-head > div { display: flex; align-items: center; gap: var(--space2); }
.section-head h2 { font-size: 16px; }
.section-head p { color: var(--text-secondary); font-size: 12px; text-align: right; }
.kind-badge { display: inline-flex; border: 1px solid; border-radius: 999px; padding: 2px 8px; font-size: 11px; font-weight: 800; white-space: nowrap; }
.kind-badge.fact { color: var(--fact); background: var(--fact-soft); border-color: var(--fact); }
.kind-badge.inference { color: var(--inference); background: var(--inference-soft); border-color: var(--inference); }
.kind-badge.validated { color: var(--action); background: #edf4ff; border-color: var(--action); }
.kind-badge.cluster { color: #6941c6; background: #f4f0ff; border-color: #6941c6; }
.card-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--space3); margin-top: var(--space4); }
.radar-card, .need-card, .cluster-card { border: 1px solid var(--border); border-radius: var(--radius); padding: var(--space4); }
.radar-card > header, .need-card > header, .cluster-card > header { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space3); }
.radar-card > header > div, .need-card > header > div:first-child { display: flex; align-items: center; flex-wrap: wrap; gap: var(--space2); }
.radar-card h3, .need-card h3, .cluster-card h3 { font-size: 15px; }
.signal-card { border-left: 4px solid var(--fact); }
.signal-type, .record-status { color: var(--text-secondary); font-size: 11px; }
.fact-block { background: var(--fact-soft); border-radius: var(--radius-sm); padding: var(--space3); margin-top: var(--space3); }
.fact-block > span { color: var(--fact); font-size: 11px; font-weight: 800; }
.inference-block { display: flex; align-items: flex-start; gap: var(--space2); margin-top: var(--space3); padding: var(--space3); border: 1px dashed var(--inference); border-radius: var(--radius-sm); background: var(--inference-soft); }
.radar-card > footer, .need-card > footer { display: flex; flex-wrap: wrap; justify-content: space-between; gap: var(--space2); border-top: 1px solid var(--border); margin-top: var(--space3); padding-top: var(--space3); color: var(--text-secondary); font-size: 11px; }
a { color: var(--action); font-weight: 700; text-decoration: none; }
.hypothesis-card { border-left: 4px solid var(--inference); background: linear-gradient(135deg, #fff 65%, var(--inference-soft)); }
.tier { color: var(--inference); background: var(--inference-soft); border-radius: 999px; padding: 3px 8px; font-size: 11px; font-weight: 800; }
.category { color: var(--text-secondary); font-size: 12px; margin-top: var(--space2); }
.reasoning { font-size: 15px; margin-top: var(--space3); }
.tier-explanation { color: var(--text-secondary); border-left: 2px solid var(--inference); padding-left: var(--space3); margin-top: var(--space3); font-size: 12px; }
details { margin-top: var(--space3); }
summary { color: var(--action); cursor: pointer; font-weight: 700; }
.evidence-list, .cluster-card ul { display: grid; gap: var(--space2); list-style: none; margin-top: var(--space3); }
.evidence-list li { border-top: 1px solid var(--border); padding-top: var(--space2); }
.evidence-list li > div { display: flex; justify-content: space-between; gap: var(--space2); }
.evidence-list span, .evidence-list a { color: var(--text-secondary); font-size: 11px; }
.needs-list { display: grid; gap: var(--space3); margin-top: var(--space4); }
.need-card { border-left: 4px solid var(--action); }
.need-card > header > div:first-child > span:last-child { color: var(--text-secondary); }
.completeness { text-align: right; }
.completeness strong { display: block; color: var(--action); font-size: 18px; }
.completeness span { color: var(--text-secondary); font-size: 11px; }
.field-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: var(--space2); margin-top: var(--space3); }
.field-grid article { background: #f8faf9; border: 1px solid var(--border); border-radius: var(--radius-sm); padding: var(--space3); }
.field-grid article > span, .field-grid small { display: block; color: var(--text-secondary); font-size: 11px; }
.field-grid article > strong { display: block; margin: var(--space1) 0; }
blockquote { border-left: 2px solid var(--fact); color: var(--text-secondary); padding-left: var(--space2); margin: var(--space2) 0; font-size: 12px; }
.missing { color: var(--warning); }
.complete { color: var(--fact); font-weight: 700; }
.cluster-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--space3); margin-top: var(--space4); }
.cluster-card { border-top: 4px solid #6941c6; }
.cluster-card > header strong { color: #6941c6; font-size: 12px; }
.cluster-stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: var(--space2); margin-top: var(--space3); }
.cluster-stats div { background: #f8faf9; border-radius: var(--radius-sm); padding: var(--space3); }
.cluster-stats span { display: block; color: var(--text-secondary); font-size: 11px; }
.catalog-note { color: #6941c6; background: #f4f0ff; border-radius: var(--radius-sm); padding: var(--space2) var(--space3); margin-top: var(--space3); font-size: 12px; font-weight: 700; }
.cluster-card li { display: flex; justify-content: space-between; gap: var(--space2); border-top: 1px solid var(--border); padding-top: var(--space2); }
.cluster-card li span { color: var(--text-secondary); font-size: 12px; }
.empty-state { display: grid; place-items: center; min-height: 280px; color: var(--text-secondary); text-align: center; }
@media (max-width: 900px) {
  .radar-tabs, .card-grid, .cluster-grid { grid-template-columns: repeat(2, 1fr); }
  .field-grid { grid-template-columns: 1fr; }
  .section-head { align-items: flex-start; flex-direction: column; }
  .section-head p { text-align: left; }
}
@media (max-width: 560px) {
  .radar-tabs, .card-grid, .cluster-grid, .cluster-stats { grid-template-columns: 1fr; }
}
</style>
