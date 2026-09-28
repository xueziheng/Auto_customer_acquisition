<script setup lang="ts">
import type { components } from "../api/api";
import { laneLabel, sourceChannelLabel, stopLabel } from "./researchLabels";
defineProps<{ research: components["schemas"]["RunResearchView"] }>();
</script>

<template>
  <section
    class="research-run"
    aria-label="研究执行摘要"
  >
    <h3>只研究 · {{ stopLabel(research.stop_reason) }}</h3>
    <p>计划线路：{{ research.planned_discovery_lanes?.map(laneLabel).join(" / ") || "尚未读取计划" }}</p>
    <p>已留证线路：{{ research.discovery_lanes?.map(laneLabel).join(" / ") || "暂无已持久证据" }}</p>
    <p aria-label="计划来源方向">
      计划来源方向：{{ research.planned_source_channels?.map(sourceChannelLabel).join(" / ") || "尚未记录来源计划" }}
    </p>
    <p aria-label="已搜索来源方向">
      已搜索来源方向：{{ research.searched_source_channels?.map(sourceChannelLabel).join(" / ") || "暂无已记录搜索" }}
    </p>
    <p aria-label="已留证来源方向">
      已留证来源方向：{{ research.source_channels?.map(sourceChannelLabel).join(" / ") || "暂无已持久证据" }}
    </p>
    <p>来源方向说明检索范围；已留证不等于身份、联系方式或采购需求已验证。</p>
    <p>免费搜索额度：已消耗 {{ research.consumed_credits }} · 未决预留 {{ (research.reserved_credits ?? 0) + (research.uncertain_credits ?? 0) }}（未派发 {{ research.reserved_credits }} / 结果不确定 {{ research.uncertain_credits }}）</p>
    <p>预算尝试：检索 {{ research.searches_used }} 次 / 页面 {{ research.pages_used }} 次，尝试次数不等于额度消耗。</p>
    <p>需求信号 {{ research.signal_count }} · 需求假设（推断）{{ research.hypothesis_count }} · 待核验 {{ research.pending_verification_count }}</p>
    <p>客户确认需求 {{ research.validated_need_count }} · 合格贸易机会 {{ research.qualified_opportunity_count }} · 触达入组 {{ research.queued_count }}</p>
    <p>流程结果：{{ stopLabel(research.completion_reason) }}。停止或候选缺失不能证明市场没有需求。</p>
  </section>
</template>

<style scoped>
.research-run { display: grid; gap: 8px; padding: 16px; background: var(--warning-soft); border: 1px solid var(--warning); border-radius: 8px; overflow-wrap: anywhere; }
h3 { font-size: 14px; } p { font-size: 12px; }
</style>
