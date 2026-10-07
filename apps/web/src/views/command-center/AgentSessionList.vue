<script setup lang="ts">
import type { components } from '../../api/api';
defineProps<{sessions:components['schemas']['SessionView'][]; selected:string|null; busy:boolean}>();
defineEmits<{select:[id:string];create:[]}>();
</script>
<template>
  <nav
    aria-label="我的会话"
    class="sessions"
  >
    <button
      type="button"
      :disabled="busy"
      @click="$emit('create')"
    >
      新建会话
    </button>
    <button
      v-for="(session,index) in sessions"
      :key="session.session_id"
      type="button"
      :aria-current="selected===session.session_id?'true':undefined"
      @click="$emit('select',session.session_id)"
    >
      会话 {{ sessions.length-index }} · {{ new Date(session.created_at).toLocaleDateString('zh-CN') }}
    </button>
  </nav>
</template>
<style scoped>.sessions{display:flex;flex-direction:column;gap:8px;min-width:0}.sessions button{text-align:left;overflow-wrap:anywhere}[aria-current=true]{border-color:var(--action);color:var(--action)}@media(max-width:650px){.sessions{max-height:150px;overflow-y:auto}}</style>
