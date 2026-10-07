<script setup lang="ts">
import { inject, onMounted, ref } from "vue";
import { RouterLink } from "vue-router";

import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";
import CatalogCultivationPanel from "./CatalogCultivationPanel.vue";
import CatalogPolicyPanel from "./CatalogPolicyPanel.vue";
import CatalogProposalPanel from "./CatalogProposalPanel.vue";

type ApiClient = ReturnType<typeof createApiClient>;
type ProductSupplyCard = components["schemas"]["ProductSupplyCardView"];
type CatalogEvaluation = components["schemas"]["CatalogProposalEvaluationView"];

const client = inject<ApiClient>("tradeos-api-client", apiClient);
const cards = ref<ProductSupplyCard[]>([]);
const loading = ref(true);
const error = ref<string | null>(null);
const sourceOnly = ref(false);
const catalogEvaluations = ref<CatalogEvaluation[]>([]);
const catalogRefreshVersion = ref(0);

function refreshCatalogDependents(): void {
  catalogRefreshVersion.value += 1;
}

function safeError(status: number): string {
  if (status === 403) return "当前身份无权读取内部供应卡";
  if (status === 503) return "供应卡服务暂不可用";
  return "供应卡读取失败，请稍后重试";
}

async function loadCards(): Promise<void> {
  loading.value = true;
  error.value = null;
  try {
    const result = await client.GET("/products", {
      params: {
        query: {
          limit: 50,
          source_only: sourceOnly.value || undefined,
        },
      },
    });
    if (result.response.status !== 200 || !result.data) {
      cards.value = [];
      error.value = safeError(result.response.status);
      return;
    }
    cards.value = result.data;
  } catch {
    cards.value = [];
    error.value = "无法连接供应卡服务";
  } finally {
    loading.value = false;
  }
}

onMounted(() => void loadCards());
</script>

<template>
  <div class="shell supply-shell">
    <div class="page-head">
      <div>
        <p class="phase-eyebrow">产品与供应</p>
        <h1>产品与供应能力</h1>
      </div>
      <button type="button" :disabled="loading" @click="loadCards">
        {{ loading ? "加载中…" : "刷新" }}
      </button>
    </div>

    <div class="safe-banner">
      <span aria-hidden="true">i</span>
      <div>本页是内部供应判断视图。来源于公开寻源的产品卡只含参考价与证据链，不能生成客户报价、联系供应商或发送内容。</div>
    </div>
    <label class="source-filter">
      <input v-model="sourceOnly" type="checkbox" @change="loadCards">
      只看公开寻源来源卡（仅公开来源）
    </label>
    <div v-if="error" class="safe-banner danger" role="alert">{{ error }}</div>

    <div v-if="loading" class="empty">正在读取供应卡…</div>
    <div v-else-if="!cards.length" class="empty">当前没有可展示的内部供应卡</div>
    <section v-else class="supply-grid" aria-label="内部供应卡">
      <article v-for="card in cards" :key="card.product_id" class="supply-card">
        <header>
          <div>
            <p class="card-kicker">{{ card.pool }}</p>
            <h2>{{ card.name_zh }}</h2>
            <p>{{ card.name_en }} · {{ card.category }}</p>
          </div>
          <span v-if="card.source_only" class="status source-only">仅公开来源</span>
        </header>
        <p>{{ card.spec_summary ?? "规格信息未知" }}</p>
        <dl>
          <div><dt>最小起订量</dt><dd>{{ card.moq ?? "未知" }}</dd></div>
          <div><dt>交期</dt><dd>{{ card.lead_time_display ?? "未知" }}</dd></div>
        </dl>
        <template v-if="card.source_only && card.source">
          <div class="safe-banner source-warning"><strong>{{ card.quote_warning }}</strong><span>公开页面参考价，不是正式报价。</span></div>
          <ul class="indicative-prices" aria-label="公开页面参考价">
            <li v-for="price in card.source.indicative_prices" :key="`${price.minimum_quantity}-${price.evidence_ref}`">
              {{ price.minimum_quantity }} 起：{{ price.unit_amount }} {{ price.currency }} / {{ price.unit }}
            </li>
          </ul>
          <p class="meta">证据：{{ card.source.evidence_refs.join("、") }}</p>
          <RouterLink :to="`/sourcing/${card.source.sourcing_case_id}`">查看寻源案例与候选证据</RouterLink>
        </template>
      </article>
    </section>

    <div
      class="catalog-regions"
      aria-label="目录候选产品内部管理"
    >
      <CatalogPolicyPanel @submitted="refreshCatalogDependents" />
      <CatalogProposalPanel
        :refresh-version="catalogRefreshVersion"
        @evaluations-loaded="catalogEvaluations = $event"
      />
      <CatalogCultivationPanel
        :evaluations="catalogEvaluations"
        :refresh-version="catalogRefreshVersion"
      />
    </div>
  </div>
</template>

<style scoped>
.supply-shell { overflow-y: auto; }
.supply-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(290px, 1fr)); gap: var(--space4); }
.supply-card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: var(--space4); display: grid; gap: var(--space3); }
.supply-card header { display: flex; justify-content: space-between; gap: var(--space3); }
.supply-card h2 { font-size: 17px; }
.supply-card p { color: var(--text-secondary); }
dl { display: grid; grid-template-columns: 1fr 1fr; gap: var(--space2); }
dt { color: var(--text-secondary); font-size: 12px; }
dd { margin: 0; font-weight: 600; }
.source-only { color: var(--warning); border-color: var(--warning); background: var(--warning-soft); }
.source-warning { display: grid; gap: 2px; }
.indicative-prices { padding-left: 20px; }
.empty { padding: var(--space5); color: var(--text-secondary); text-align: center; }
.source-filter { display: flex; align-items: center; gap: var(--space2); color: var(--text-secondary); font-size: 13px; }
.catalog-regions { display: grid; grid-template-columns: minmax(0, 1fr); gap: var(--space5); margin-top: var(--space6); }
</style>
