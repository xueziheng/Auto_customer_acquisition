<script setup lang="ts">
/* global HTMLTextAreaElement */
import { inject, onMounted, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import { apiClient } from '../../api/client';
import { useAgentSession } from '../../composables/useAgentSession';
import AgentSessionList from './AgentSessionList.vue';
import AgentTurnCard from './AgentTurnCard.vue';
const client=inject('tradeos-api-client',apiClient), router=useRouter();
const emit=defineEmits<{available:[value:boolean]}>();
const {sessions,turns,sessionId,loading,error,available,uncertain,active,startSession,selectSession,refreshSessions,send,cancel,regenerate}=useAgentSession(client);
const draft=ref(''), editor=ref<HTMLTextAreaElement|null>(null);
watch(available,v=>emit('available',v));
watch(sessionId,()=>{draft.value='';editor.value?.focus();});
async function submit(){if(await send(draft.value))draft.value='';}
onMounted(()=>void refreshSessions());
</script>
<template>
  <section
    class="assistant"
    aria-labelledby="assistant-title"
  >
    <header>
      <div>
        <h2 id="assistant-title">
          TradeOS 助手
        </h2><p>查询业务、补全研究范围、准备待确认提案。会话仅本人可见。</p>
      </div><button
        type="button"
        @click="refreshSessions"
      >
        刷新会话
      </button>
    </header>
    <p
      v-if="error"
      role="alert"
    >
      {{ error }} <RouterLink to="/settings">
        查看系统设置
      </RouterLink>
    </p>
    <div
      v-if="available"
      class="conversation-grid"
    >
      <AgentSessionList
        :sessions="sessions"
        :selected="sessionId"
        :busy="loading"
        @select="selectSession"
        @create="startSession"
      />
      <div class="conversation">
        <p v-if="!sessionId">
          新建一个会话，说明你要了解的业务或研究方向。
        </p>
        <div
          class="turns"
          aria-live="polite"
        >
          <AgentTurnCard
            v-for="turn in turns"
            :key="turn.turn_id"
            :turn="turn"
            :busy="loading"
            @cancel="cancel"
            @regenerate="regenerate"
            @open-proposal="id=>router.push({path:'/commands',query:{proposal_id:id}})"
            @open-run="id=>router.push({path:'/runs',query:{run:id}})"
          />
        </div>
        <form
          v-if="sessionId"
          @submit.prevent="submit"
        >
          <label for="assistant-input">给助手的消息</label>
          <textarea
            id="assistant-input"
            ref="editor"
            v-model="draft"
            rows="4"
            maxlength="10000"
            :disabled="loading||active||uncertain"
            placeholder="例如：查看我负责的贸易机会。研究时请明确国家、品类、排除项和预算。"
          />
          <p>请勿输入密钥。聊天中的“好”不会批准提案、发信或报价。</p>
          <button
            class="btn-primary"
            type="submit"
            :disabled="loading||(!uncertain&&(!draft.trim()||active))"
          >
            {{ uncertain?'核对原请求':loading?'正在提交…':'发送消息' }}
          </button>
        </form>
      </div>
    </div>
  </section>
</template>
<style scoped>.assistant{border:1px solid var(--border);border-radius:12px;padding:20px;background:var(--surface)}header{display:flex;gap:16px;justify-content:space-between;flex-wrap:wrap}header p,form p{color:var(--text-secondary);font-size:12px;margin:8px 0}.conversation-grid{display:grid;grid-template-columns:185px minmax(0,1fr);gap:20px;margin-top:20px}.conversation,.turns{min-width:0}.turns{display:grid;gap:12px}form{display:grid;gap:10px;margin-top:20px}textarea{width:100%;max-width:100%;box-sizing:border-box;resize:vertical;border:1px solid var(--border);border-radius:8px;padding:12px;font:inherit;background:var(--surface);color:var(--text-primary)}button:focus-visible,textarea:focus-visible{outline:3px solid var(--action);outline-offset:2px}@media(max-width:650px){.conversation-grid{grid-template-columns:1fr}.assistant{padding:14px}}</style>
