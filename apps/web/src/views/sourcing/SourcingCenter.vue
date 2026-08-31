<script setup lang="ts">
import { inject, onMounted, ref } from "vue";
import { RouterLink } from "vue-router";

import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";

type ApiClient = ReturnType<typeof createApiClient>;
type SourcingCase = components["schemas"]["SourcingCaseReadView"];

const client = inject<ApiClient>("tradeos-api-client", apiClient);
const cases = ref<SourcingCase[]>([]);
const loading = ref(true);
const error = ref<string | null>(null);

async function loadCases(): Promise<void> {
  loading.value = true;
  error.value = null;
  try {
    const result = await client.GET("/sourcing-cases", { params: { query: { limit: 50 } } });
    if (result.response.status !== 200 || !result.data) {
      cases.value = [];
      error.value = result.response.status === 403 ? "当前身份无权读取寻源案例" : "寻源案例暂不可读取";
      return;
    }
    cases.value = result.data;
  } catch {
    cases.value = [];
    error.value = "无法连接寻源服务";
  } finally {
    loading.value = false;
  }
}

onMounted(() => void loadCases());
</script>

<template>
  <div class="shell sourcing-shell">
    <div class="page-head">
      <div><p class="phase-eyebrow">SOURCING CENTER</p><h1>寻源中心</h1></div>
      <button type="button" :disabled="loading" @click="loadCases">{{ loading ? "加载中…" : "刷新" }}</button>
    </div>
    <div class="safe-banner"><span aria-hidden="true">i</span><div>寻源从已验证 Need 出发。网页信息是事实或供应商自述，匹配结论必须另列为推断；公开页面价格始终只是 indicative。</div></div>
    <div v-if="error" class="safe-banner danger" role="alert">{{ error }}</div>
    <div v-if="loading" class="empty">正在读取案例队列…</div>
    <div v-else-if="!cases.length" class="empty">当前没有开放的寻源案例</div>
    <section v-else class="case-list" aria-label="寻源案例队列">
      <RouterLink v-for="item in cases" :key="item.case_id" class="case-row" :to="`/sourcing/${item.case_id}`">
        <span><strong>{{ item.case_id }}</strong><small>Need {{ item.need_id }}</small></span>
        <span>{{ item.state }} · 梯子至 {{ item.ladder_checked_to ?? "未知" }}</span>
        <span v-if="item.stop">停止：{{ item.stop.code }}</span>
      </RouterLink>
    </section>
  </div>
</template>

<style scoped>
.sourcing-shell { overflow-y: auto; }
.case-list { display: grid; gap: var(--space2); }
.case-row { display: flex; justify-content: space-between; gap: var(--space3); align-items: center; padding: var(--space3) var(--space4); border: 1px solid var(--border); border-radius: var(--radius); color: var(--text-primary); text-decoration: none; background: var(--surface); }
.case-row:hover { border-color: var(--action); }
.case-row span { display: grid; gap: 2px; }
.case-row small { color: var(--text-secondary); }
.empty { padding: var(--space5); color: var(--text-secondary); text-align: center; }
</style>
