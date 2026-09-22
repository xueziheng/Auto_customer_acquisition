<script setup lang="ts">
import type { components } from '../../api/api';
type Turn = components['schemas']['TurnView'];
defineProps<{turn: Turn; busy?: boolean}>();
defineEmits<{cancel:[turn:Turn]; regenerate:[turn:Turn]; openProposal:[id:string]; openRun:[id:string]}>();
const states: Record<Turn['state'],string> = {queued:'等待后台处理',running:'正在生成',awaiting_input:'需要补充信息',proposal_ready:'提案待确认',completed:'回答已完成',blocked:'已阻断',failed:'生成失败',unknown:'结果未知',cancelled:'已停止生成'};
function sourceLink(ref: components['schemas']['ObjectRef']) {
  if(ref.kind==='run') return `/runs?run=${encodeURIComponent(ref.object_id)}`;
  if(ref.kind==='handoff') return `/crm/handoffs/${encodeURIComponent(ref.object_id)}`;
  if(ref.kind==='opportunity') return `/crm/opportunities?opportunity=${encodeURIComponent(ref.object_id)}`;
  return `/demand/needs/${encodeURIComponent(ref.object_id)}`;
}
</script>
<template>
  <article
    class="turn-card"
    :aria-label="states[turn.state]"
  >
    <header><strong>{{ states[turn.state] }}</strong><time>{{ new Date(turn.created_at).toLocaleString('zh-CN') }}</time></header>
    <p v-if="turn.content_hidden">
      当前权限已变化，本轮内容已隐藏。
    </p>
    <template v-else>
      <p class="input-text">
        {{ turn.input_text }}
      </p>
      <ul v-if="turn.result?.kind === 'clarify'">
        <li
          v-for="question in turn.result.questions"
          :key="question"
        >
          {{ question }}
        </li>
      </ul>
      <div v-if="turn.result?.kind === 'explain'">
        <div
          v-for="(fragment,index) in turn.result.fragments"
          :key="index"
        >
          <p>{{ fragment.text }}</p>
          <a
            v-for="source in fragment.dependencies.filter(s=>s.kind!=='product_doc')"
            :key="source.object_id"
            :href="sourceLink(source)"
          >查看来源 {{ source.object_id }}</a>
        </div>
      </div>
      <p
        v-if="turn.state==='unknown'"
        role="status"
      >
        请求可能已执行。重新生成将再次消耗额度。
      </p>
      <p v-if="turn.error_code">
        原因：{{ ({quota:'额度不足',permission:'当前权限不足',configuration:'模型配置未就绪',authentication:'连接凭证无效',insufficient_balance:'模型余额不足',invalid_response:'返回内容未通过校验',unknown:'结果无法确认'} as Record<string,string>)[turn.error_code] ?? '服务暂时未完成请求' }}
      </p>
      <p v-if="turn.proposal_id">
        研究提案需要单独核对并确认；发现信号不代表客户需求已验证。
      </p>
      <button
        v-if="turn.proposal_id"
        type="button"
        @click="$emit('openProposal',turn.proposal_id)"
      >
        审阅研究提案
      </button>
      <button
        v-if="['queued','running'].includes(turn.state)"
        type="button"
        :disabled="busy"
        @click="$emit('cancel',turn)"
      >
        停止生成
      </button>
      <button
        v-if="['failed','blocked','unknown','cancelled'].includes(turn.state)"
        type="button"
        :disabled="busy"
        @click="$emit('regenerate',turn)"
      >
        重新生成（再次消耗额度）
      </button>
      <button
        type="button"
        @click="$emit('openRun',turn.run_id)"
      >
        查看运行记录
      </button>
    </template>
  </article>
</template>
<style scoped>
.turn-card{border:1px solid var(--border);border-radius:10px;padding:16px;min-width:0;overflow-wrap:anywhere;background:var(--surface)}header{display:flex;flex-wrap:wrap;gap:8px;justify-content:space-between}time{font-size:12px;color:var(--text-secondary)}p{white-space:pre-wrap;margin:12px 0}.input-text{border-left:3px solid var(--action);padding-left:12px}button,a{margin:6px 10px 0 0}ul{padding-left:20px}
</style>
