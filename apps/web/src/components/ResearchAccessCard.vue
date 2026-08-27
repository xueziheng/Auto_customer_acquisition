<script setup lang="ts">
import type { components } from "../api/api";
import { researchAccessLabels } from "./researchLabels";
defineProps<{ status: components["schemas"]["ResearchAccessView"] | null }>();
</script>

<template>
  <section
    class="research-access"
    aria-label="免费研究账户状态"
  >
    <h3>免费公开研究 · Tavily basic</h3>
    <strong>{{ status ? researchAccessLabels[status.state] : "研究账户状态暂不可读" }}</strong>
    <p v-if="status?.confirmation_requires_recheck">
      确认仅请求重新核验后研究，不代表已允许搜索；只有 Gateway 核实当前免费额度并成功预留后才可搜索，不重试不确定调用、不释放原预留。
    </p>
    <p>运行时激活尚未证实；配置存在不代表生产调度已激活。实际执行仍须经过 Tool Gateway 用量核验与预留。</p>
    <p v-if="status?.remaining_lower_bound != null">
      账户剩余额度安全下界：{{ status.remaining_lower_bound }} credits（不是供应商精确余额）
    </p>
    <p v-if="status?.checked_at">
      最近持久核验：{{ status.checked_at }}
    </p>
    <p>仅研究公开来源，不补全联系人、不验证邮箱、不发送、不报价；模型与基础设施仍可能产生费用。</p>
  </section>
</template>

<style scoped>
.research-access { padding: 16px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface); display: grid; gap: 8px; min-width: 0; overflow-wrap: anywhere; flex-shrink: 0; }
h3 { font-size: 14px; } p { color: var(--text-secondary); font-size: 12px; }
</style>
