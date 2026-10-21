<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref } from 'vue';
import { platformClient, PlatformDataSourceError, type ModelEntry, type ModelSave } from '../api/platform-auth';
const props = defineProps<{ entry: ModelEntry | null; endpoints: readonly string[] }>();
const emit = defineEmits<{ changed: [ModelEntry]; back: [] }>();
const current = ref(props.entry);
const name = ref(props.entry?.name ?? '');
const provider = ref<ModelSave['provider']>(props.entry?.provider ?? 'deepseek');
const baseUrl = ref(props.entry?.base_url ?? 'https://api.deepseek.com');
const protocol = ref<ModelSave['protocol']>(props.entry?.protocol ?? 'responses');
const model = ref(props.entry?.model ?? '');
const apiKey = ref('');
const busy = ref('');
const message = ref('');
const feedback = ref<{state: string; title: string; detail: string; at?: string | null} | null>(null);
const resultElement = ref<globalThis.HTMLElement | null>(null);
let live = true;
const dirty = computed(() => !current.value || apiKey.value !== '' || name.value !== current.value.name || model.value !== current.value.model || baseUrl.value !== current.value.base_url || protocol.value !== current.value.protocol || provider.value !== current.value.provider);
const canKeep = computed(() => current.value?.configured && provider.value === current.value.provider && baseUrl.value === current.value.base_url);
const choices = computed(() => props.endpoints.filter(url => provider.value !== 'deepseek' || /^https:\/\/api.deepseek.com(\/v1)?$/.test(url)));
const labels: Record<string, string> = {authentication:'密钥无效或没有访问权限', insufficient_balance:'余额不足', rate_limit:'服务商限流，请稍后重试', invalid_request:'模型名称、协议或运行配置不匹配', invalid_response:'模型没有完成所需的结构化响应或工具调用', output_limit:'测试输出达到限制', provider_error:'服务商暂时不可用', unknown:'未能获得完整结果，请刷新状态后重试'};
function changeProvider() { apiKey.value = ''; baseUrl.value = provider.value === 'deepseek' ? 'https://api.deepseek.com' : 'https://dashscope.aliyuncs.com/compatible-mode/v1'; protocol.value = provider.value === 'deepseek' ? 'responses' : 'chat_completions'; }
async function action(kind: 'save' | 'test' | 'agent') {
  if (busy.value || (kind !== 'save' && (!current.value || dirty.value))) return;
  busy.value = kind; message.value = '';
  const previous = kind === 'agent' ? current.value?.agent_tested_at : current.value?.tested_at;
  feedback.value = {state:'running', title:kind === 'save' ? '正在保存…' : kind === 'agent' ? '正在测试 Codex Agent…' : '正在测试 API 连接…', detail:kind === 'agent' ? '启动 Codex、读取任务上下文并验证最终结果，请稍候。' : '请稍候。'};
  try {
    let value: ModelEntry;
    if (kind === 'save') {
      const body: ModelSave = {name:name.value.trim(), expected_revision:current.value?.revision ?? 0, provider:provider.value, base_url:baseUrl.value, protocol:protocol.value, model:model.value.trim(), api_key:apiKey.value || null};
      apiKey.value = '';
      value = await platformClient.saveModel(current.value?.id ?? null, body);
    } else value = await platformClient.testModel(current.value!.id, current.value!.revision ?? 0, kind === 'agent');
    if (!live) return;
    current.value = value; name.value = value.name; model.value = value.model ?? ""; provider.value = value.provider ?? "deepseek"; baseUrl.value = value.base_url ?? "https://api.deepseek.com"; protocol.value = value.protocol ?? "responses"; emit('changed', value);
    if (kind === 'save') { feedback.value = null; message.value = '配置已保存。测试通过后，可在公司设置中选择此版本。'; }
    else {
      const passed = (kind === 'agent' ? value.agent_test_status : value.test_status) === 'passed';
      const at = kind === 'agent' ? value.agent_tested_at : value.tested_at;
      const code = kind === 'agent' ? value.agent_failure_code : value.failure_code;
      feedback.value = {state:passed?'passed':'failed', title:passed ? (kind === 'agent' ? 'Codex Agent 测试通过' : 'API 连接成功') : '测试失败', detail:(passed ? (kind === 'agent' ? '已完成 Codex 启动、MCP 上下文工具调用和 JSON 结果验证。' : `模型 ${value.model} 已正常响应。`) : labels[code ?? 'unknown'] ?? labels.unknown) + (at && at === previous ? ' 已复用一分钟内的结果，未重复调用。' : ''), at};
    }
  } catch (reason) { if (live) feedback.value = {state:'failed', title:'操作未完成', detail:reason instanceof PlatformDataSourceError && reason.status === 409 ? '配置已变化，请返回列表刷新后操作。' : '请检查填写内容或刷新状态后重试。'}; }
  finally { if (live) { busy.value = ''; await nextTick(); resultElement.value?.scrollIntoView?.({block:'nearest'}); } }
}
onUnmounted(() => {live = false; apiKey.value = '';});
</script>
<template>
  <section class="model-editor">
    <header>
      <button
        :disabled="!!busy"
        @click="emit('back')"
      >
        ← 返回模型列表
      </button><h2>{{ current?.name || '新增模型配置' }}</h2>
    </header>
    <form @submit.prevent="action('save')">
      <div class="fields">
        <label>配置名称<input
          v-model="name"
          maxlength="80"
          required
          :disabled="!!busy"
          placeholder="例如：DeepSeek 日常任务"
        ></label>
        <label>服务商<select
          v-model="provider"
          :disabled="!!busy"
          @change="changeProvider"
        ><option value="deepseek">DeepSeek</option><option value="compatible">其他兼容模型 API</option></select></label>
        <label>接口地址<select
          v-model="baseUrl"
          :disabled="!!busy"
          @change="apiKey = ''"
        ><option
          v-for="url in choices"
          :key="url"
          :value="url"
        >{{ url }}</option></select></label>
        <label>接口协议<select
          v-model="protocol"
          :disabled="!!busy"
        ><option value="responses">Responses</option><option value="chat_completions">Chat Completions</option></select></label>
        <label>模型名称<input
          v-model="model"
          required
          maxlength="128"
          :disabled="!!busy"
          placeholder="服务商提供的模型 ID"
        ></label>
        <label>API Key <small>{{ current?.configured ? '已配置 · ••••••••' : '尚未配置' }}</small><input
          v-model="apiKey"
          type="password"
          autocomplete="new-password"
          maxlength="8192"
          :required="!canKeep"
          :disabled="!!busy"
          :placeholder="canKeep ? '留空保留现有密钥；填写新密钥可替换' : '输入此服务商的 API Key'"
        ></label>
      </div>
      <p class="hint">
        编辑后保存为新版本。正在运行的任务和公司当前使用的版本不会自动切换。
      </p>
      <div
        v-if="current && !dirty"
        class="statuses"
      >
        <span>API 连接：{{ current.test_status === 'passed' ? '通过' : current.test_status === 'failed' ? '失败' : '尚未测试' }}</span>
        <span>Codex Agent：{{ current.agent_test_status === 'passed' ? '通过' : current.agent_test_status === 'failed' ? '失败' : '尚未测试' }}</span>
      </div>
      <div
        v-if="feedback"
        ref="resultElement"
        class="feedback"
        :class="feedback.state"
        role="status"
        aria-live="polite"
      >
        <strong>{{ feedback.title }}</strong><span>{{ feedback.detail }}</span><small v-if="feedback.at">测试时间：{{ new Date(feedback.at).toLocaleString('zh-CN') }}</small>
      </div>
      <p
        v-if="message"
        role="status"
      >
        {{ message }}
      </p>
      <div class="actions">
        <button
          class="primary"
          type="submit"
          :disabled="!!busy"
        >
          {{ busy === 'save' ? '正在保存…' : '保存配置' }}
        </button>
        <button
          type="button"
          :disabled="!!busy || dirty || !current?.configured"
          @click="action('test')"
        >
          {{ busy === 'test' ? '正在测试…' : '测试 API 连接' }}
        </button>
        <button
          type="button"
          :disabled="!!busy || dirty || current?.test_status !== 'passed'"
          @click="action('agent')"
        >
          {{ busy === 'agent' ? '正在测试 Agent…' : '测试 Codex Agent' }}
        </button>
      </div>
      <p
        v-if="dirty"
        class="hint"
      >
        请先保存修改，再进行测试。
      </p>
      <p class="hint">
        打开页面不调用模型；测试会消耗少量模型额度，一分钟内复用结果。网站与数据源的访问权限仍由各自连接决定。
      </p>
    </form>
  </section>
</template>
<style scoped>
header{display:flex;align-items:center;gap:20px;margin-bottom:20px}h2{margin:0;font-size:22px}form{background:white;border:1px solid #cedbd8;border-radius:12px;padding:28px}.fields{display:grid;grid-template-columns:1fr 1fr;gap:22px}label{display:flex;flex-direction:column;gap:9px;font-weight:600;min-width:0}input,select{box-sizing:border-box;width:100%;min-height:44px;border:1px solid #bccdca;border-radius:6px;padding:10px 12px;background:white;color:inherit;font:inherit;font-weight:400}button{padding:10px 17px;border:1px solid #bdcfca;border-radius:6px;background:white;color:inherit;font:inherit;cursor:pointer}button:disabled{opacity:.5;cursor:not-allowed}.primary{background:#1559e8;color:white;border-color:#1559e8}.actions,.statuses{display:flex;flex-wrap:wrap;gap:12px;margin-top:22px}.statuses{gap:24px}.hint,small{font-size:14px;font-weight:400;color:#647572;line-height:1.7}.feedback{display:flex;flex-direction:column;gap:8px;padding:18px;margin-top:20px;border:1px solid #aac9fa;border-radius:8px;background:#eff5ff;color:#174ea6}.feedback.passed{color:#196139;background:#edf8f1;border-color:#a5d8b8}.feedback.failed{color:#922525;background:#fff2f1;border-color:#e9b7b3}@media(max-width:700px){.fields{grid-template-columns:1fr}form{padding:18px}header{flex-wrap:wrap}}
</style>
