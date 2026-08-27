<script setup lang="ts">
/* global URL */
import { computed } from "vue";
import type { components } from "../api/api";
import { laneLabel } from "./researchLabels";

const props = defineProps<{ signal: components["schemas"]["DemandSignalView"] }>();
const evidence = computed(() => props.signal.research_evidence);
const source = computed(() => {
  try {
    const url = new URL(evidence.value?.source_url ?? "");
    return ["http:", "https:"].includes(url.protocol) ? url.href : null;
  } catch { return null; }
});
const sourceLabel = computed(() => evidence.value ? ({
  company_self_description: "企业自述", directory_listing: "目录收录", unverified_public_page: "公开页面待核验",
}[evidence.value.source_kind] ?? "来源待核验") : "来源待核验");
</script>

<template>
  <section
    v-if="evidence"
    class="research-evidence"
    aria-label="研究来源证据"
  >
    <strong>{{ laneLabel(evidence.discovery_lane) }} · {{ sourceLabel }}</strong>
    <span>{{ evidence.identity_status === "self_described" ? "企业自述，未独立核验" : "待核验" }}</span>
    <p>候选线路不代表运输记录、企业身份认证或客户采购确认。</p>
    <dl>
      <dt>查询市场 / 品类</dt><dd>{{ evidence.query_country }} / {{ evidence.query_category }}</dd>
      <dt>所在地证据</dt><dd>{{ evidence.country ?? "待核验（不以查询国家替代）" }}</dd>
      <dt>检索式</dt><dd>{{ evidence.query }}</dd>
      <dt>身份原文</dt><dd>{{ evidence.identity_quote ?? "无可确认的身份原文" }}</dd>
      <dt>所在地原文</dt><dd>{{ evidence.country_quote ?? "无可确认的所在地原文" }}</dd>
      <dt>观察时间</dt><dd>{{ signal.observed_at }}</dd>
      <dt>页面哈希</dt><dd><code>{{ signal.page_hash ?? "历史记录未提供" }}</code></dd>
      <dt>不可变快照</dt><dd><code>{{ signal.snapshot_artifact_ref ?? "历史记录未提供" }}</code></dd>
    </dl>
    <a
      v-if="source"
      :href="source"
      target="_blank"
      rel="noopener noreferrer"
    >查看原网页 ↗</a>
  </section>
</template>

<style scoped>
.research-evidence { display: grid; gap: 8px; padding: 14px; margin-top: 10px; background: var(--warning-soft); border: 1px solid var(--warning); border-radius: 8px; overflow-wrap: anywhere; }
.research-evidence > span, p { color: var(--text-secondary); font-size: 12px; }
dl { display: grid; grid-template-columns: minmax(90px, .3fr) 1fr; gap: 6px 12px; font-size: 12px; }
dt { color: var(--text-secondary); }
dd { min-width: 0; }
a { color: var(--action); }
</style>
