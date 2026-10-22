<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { platformClient, type ModelCatalog, type ModelEntry } from '../api/platform-auth';
import PlatformModelEditor from './PlatformModelEditor.vue';
const catalog = ref<ModelCatalog | null>(null);
const selectedCompany = ref('');
const selectedModel = ref('');
const editing = ref<ModelEntry | null | undefined>(undefined);
const busy = ref(false);
const error = ref('');
const message = ref('');
const deleting = ref<ModelEntry | null>(null);
let live = true;
const company = computed(() => catalog.value?.companies.find(c => c.tenant_id === selectedCompany.value));
const chosen = computed(() => catalog.value?.models.find(m => m.id === selectedModel.value));
const eligible = computed(() => catalog.value?.models.filter(m => m.test_status === 'passed' && m.agent_test_status === 'passed') ?? []);
const unchanged = computed(() => company.value?.model_id === chosen.value?.id && company.value?.configuration_version === chosen.value?.configuration_version);
function selectCompany() { selectedModel.value = company.value?.model_id ?? ''; message.value = ''; }
function accept(value: ModelCatalog) { catalog.value = value; if (!value.companies.some(c => c.tenant_id === selectedCompany.value)) selectedCompany.value = value.companies[0]?.tenant_id ?? ''; selectCompany(); }
async function refresh() { busy.value = true; error.value = ''; try {const value = await platformClient.modelCatalog(); if(live) accept(value);} catch {if(live) error.value = '无法读取模型配置，请刷新重试。';} finally {if(live) busy.value = false;} }
function usage(entry: ModelEntry) { const names = catalog.value?.companies.filter(c => (entry.used_by ?? []).includes(c.tenant_id)).map(c => c.name) ?? []; return names.length === 1 ? `${names[0]} 使用中` : names.length > 1 ? `被 ${names.length} 家公司使用` : '未分配'; }
function changed(entry: ModelEntry) { if (!catalog.value) return; const index = catalog.value.models.findIndex(m => m.id === entry.id); if(index < 0) catalog.value.models.push(entry); else catalog.value.models[index] = entry; }
async function bind() {
  if(busy.value || !company.value || !chosen.value || unchanged.value) return;
  busy.value = true; error.value = ''; message.value = '';
  const name = company.value.name;
  try { const value = await platformClient.bindModel(company.value.tenant_id, {expected_revision:company.value.revision ?? 0, model_id:chosen.value.id, model_revision:chosen.value.revision ?? 0}); if(live) {accept(value); message.value = `${name} 的模型已更新，新任务使用所选版本。`;}}
  catch {if(live) error.value = '切换未完成，请刷新并确认此版本通过了两项测试。';}
  finally {if(live) busy.value = false;}
}
async function remove() { if(!deleting.value || busy.value) return; busy.value = true; try { const value = await platformClient.deleteModel(deleting.value.id, deleting.value.revision ?? 0); if(live) {accept(value); deleting.value = null; message.value = '配置已移除。';}} catch {if(live) error.value = '无法删除：配置可能已变化或仍被公司使用。';} finally {if(live) busy.value = false;} }
onMounted(() => {void refresh();}); onUnmounted(() => {live = false;});
</script>
<template>
  <section
    class="model-catalog"
    aria-label="模型 API 设置"
  >
    <PlatformModelEditor
      v-if="editing !== undefined"
      :entry="editing"
      :endpoints="catalog?.supported_endpoints ?? []"
      @changed="changed"
      @back="editing = undefined; refresh()"
    />
    <template v-else>
      <header class="heading">
        <div><h1>模型 API 设置</h1><p>保存多个模型连接，为每家公司选择使用的模型。</p></div><div class="actions">
          <button
            :disabled="busy"
            @click="refresh"
          >
            刷新状态
          </button><button
            class="primary"
            :disabled="busy || !catalog"
            @click="editing = null"
          >
            ＋ 新增模型
          </button>
        </div>
      </header>
      <p
        v-if="error"
        class="error"
        role="alert"
      >
        {{ error }}
      </p><p
        v-if="message"
        class="success"
        role="status"
      >
        {{ message }}
      </p>
      <div
        v-if="catalog"
        class="company-panel"
      >
        <h2>公司使用的模型</h2>
        <div class="company-grid">
          <label>选择公司<select
            v-model="selectedCompany"
            :disabled="busy"
            @change="selectCompany"
          ><option
            v-for="c in catalog.companies"
            :key="c.tenant_id"
            :value="c.tenant_id"
          >{{ c.name }}{{ c.enabled ? '' : '（已停用）' }}</option></select></label>
          <label>使用模型<select
            v-model="selectedModel"
            :disabled="busy || !company?.enabled"
          ><option value="">请选择已通过测试的配置</option><option
            v-if="company?.model_id && !eligible.some(m => m.id === company?.model_id)"
            :value="company.model_id"
            disabled
          >{{ company.configuration_name }} · 当前绑定版本</option><option
            v-for="m in eligible"
            :key="m.id"
            :value="m.id"
          >{{ m.name }} · {{ m.model }}</option></select></label>
          <button
            class="primary"
            :disabled="busy || !company?.enabled || !chosen || chosen.test_status !== 'passed' || chosen.agent_test_status !== 'passed' || unchanged"
            @click="bind"
          >
            {{ busy ? '请稍候…' : '使用此模型' }}
          </button>
        </div>
        <p
          v-if="company"
          class="hint"
        >
          当前：<strong>{{ company.configuration_name || '尚未选择' }}</strong><span v-if="company.model"> · {{ company.model }}</span><span v-if="!company.runtime_available"> · 公司运行环境暂不可用</span>
        </p>
        <p class="hint">
          任务首次调用模型时锁定版本；切换后，已绑定模型的任务保留原版本。执行引擎统一为 Codex CLI。
        </p>
      </div>
      <div
        v-if="catalog"
        class="cards"
      >
        <article
          v-for="entry in catalog.models"
          :key="entry.id"
          class="model-card"
          :class="{active:(entry.used_by?.length ?? 0) > 0}"
        >
          <button
            class="card-main"
            :aria-label="`编辑 ${entry.name}`"
            @click="editing = entry"
          >
            <span class="avatar">{{ entry.name.charAt(0).toUpperCase() }}</span><span class="card-details"><strong>{{ entry.name }}</strong><span>{{ entry.base_url }}</span><small>{{ entry.model }} · {{ entry.protocol === 'responses' ? 'Responses' : 'Chat Completions' }}</small></span>
          </button>
          <div class="card-state">
            <span class="usage">{{ usage(entry) }}</span><small>API {{ entry.test_status === 'passed' ? '通过' : entry.test_status === 'failed' ? '失败' : '未测试' }} · Agent {{ entry.agent_test_status === 'passed' ? '通过' : entry.agent_test_status === 'failed' ? '失败' : '未测试' }}</small><small v-if="catalog.companies.some(c => c.model_id === entry.id && c.configuration_version !== entry.configuration_version)">公司仍使用此前版本</small>
          </div>
          <div class="actions">
            <button @click="editing = entry">
              编辑
            </button><button
              :disabled="busy || (entry.used_by?.length ?? 0) > 0"
              :title="entry.used_by?.length ? '请先为使用它的公司切换模型' : '删除配置'"
              @click="deleting = entry"
            >
              删除
            </button>
          </div>
        </article>
        <p
          v-if="!catalog.models.length"
          class="hint"
        >
          尚未添加模型，请点击“新增模型”。
        </p>
      </div>
      <p
        v-else-if="!error"
        role="status"
      >
        正在读取模型配置…
      </p>
      <div
        v-if="deleting"
        class="delete-confirm"
        role="alertdialog"
        aria-label="删除模型配置"
      >
        <p>删除“{{ deleting.name }}”？删除后不再显示在配置列表中。</p><div class="actions">
          <button
            :disabled="busy"
            @click="deleting = null"
          >
            取消
          </button><button
            :disabled="busy"
            @click="remove"
          >
            确认删除
          </button>
        </div>
      </div>
    </template>
  </section>
</template>
<style scoped>
.model-catalog{max-width:1200px;color:#203330}.heading{display:flex;justify-content:space-between;align-items:center;gap:20px;margin-bottom:24px}h1{font-size:26px;margin:0 0 8px}h2{font-size:18px;margin:0 0 20px}.heading p,.hint{color:#667873;line-height:1.7;font-size:14px;margin:8px 0}.actions{display:flex;gap:10px;flex-wrap:wrap}button{padding:10px 16px;background:white;border:1px solid #c3d1ce;border-radius:7px;color:inherit;font:inherit;cursor:pointer}button:disabled{opacity:.45;cursor:not-allowed}button.primary{color:white;background:#1559e8;border-color:#1559e8}.company-panel{padding:24px;background:white;border:1px solid #cddad6;border-radius:12px;margin-bottom:24px}.company-grid{display:grid;grid-template-columns:1fr 1.4fr auto;gap:16px;align-items:end}label{display:flex;flex-direction:column;gap:9px;font-weight:600;min-width:0}select{width:100%;min-height:44px;padding:10px 12px;background:white;border:1px solid #c3d1ce;border-radius:7px;font:inherit;font-weight:400;color:inherit}.cards{display:grid;gap:14px}.model-card{display:flex;align-items:center;gap:20px;padding:22px;background:white;border:1px solid #d0d9df;border-radius:14px}.model-card.active{background:#eef5ff;border-color:#2886ff}.card-main{border:0;background:none;padding:0;display:flex;align-items:center;gap:20px;text-align:left;min-width:0;flex:1}.avatar{width:52px;height:52px;display:grid;place-items:center;flex-shrink:0;background:white;border:1px solid #d6dfe3;border-radius:13px;font-size:21px}.card-details{display:flex;flex-direction:column;gap:7px;overflow-wrap:anywhere}.card-details strong{font-size:20px}.card-details span{color:#65727c}.card-details small,.card-state small{font-size:13px;color:#6f7c84}.card-state{display:flex;flex-direction:column;gap:8px;max-width:260px}.usage{font-weight:600;color:#1e66b8}.error{color:#a72c2c}.success{color:#196139}.delete-confirm{padding:20px;border:1px solid #e2c2ba;border-radius:10px;background:#fff6f3;margin-top:18px}@media(max-width:800px){.company-grid{grid-template-columns:1fr}.model-card{flex-wrap:wrap}.card-main{flex-basis:100%}.card-state{margin-right:auto}.heading{align-items:flex-start;flex-wrap:wrap}}
</style>
