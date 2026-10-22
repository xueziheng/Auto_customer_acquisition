<script setup lang="ts">
import { codeLabel } from "../../components/displayLabels";
/* global setTimeout, clearTimeout */
import { inject, onMounted, onBeforeUnmount, ref } from 'vue';
import type { components } from '../../api/api';
import { apiClient } from '../../api/client';
import { useQuoteRequestScope } from '../costing-quotes/quote-request-scope';
type View=components['schemas']['ModelSettingsView'];
type Limits=components['schemas']['ModelLimits'];
const client=inject('tradeos-api-client',apiClient);
const view=ref<View|null>(null),error=ref(''),busy=ref(false),visible=ref(true);
let key:string|null=null, timer:ReturnType<typeof setTimeout>|undefined;
const labels:Record<View['status'],string>={missing:'尚未配置',pending_restart:'已保存，等待进程重启',unverified:'已装配，尚未验证',verified:'连接验证通过',failed:'连接测试未通过'};
const fields:Record<keyof Limits,string>={window_seconds:'额度窗口（秒）',tenant_calls:'公司调用上限',employee_calls:'每人调用上限',tenant_concurrency:'公司并发上限',employee_concurrency:'每人并发上限',max_input_bytes:'输入上限（字节）',max_output_tokens:'输出上限（模型计量单位）',timeout_seconds:'超时（秒）'};
const scope=useQuoteRequestScope(client,()=>[],()=>{view.value=null;key=null;busy.value=false;if(timer)clearTimeout(timer);});
async function load() {
  const op=scope.begin('load');if(!op)return;
  try {
    const capabilities=await client.GET('/health/capabilities',{signal:op.signal});if(!op.valid())return;
    if(!capabilities.data?.some(c=>c.name==='model'&&c.status==='enabled')){view.value=null;visible.value=false;return;}
    const r=await client.GET('/settings/model',{signal:op.signal});if(!op.valid())return;
    visible.value=r.response.status!==403;
    if(!r.data){view.value=null;error.value=r.response.status===503?'独立模型服务尚未装配':'无法读取当前模型设置';return;}
    view.value=r.data;error.value='';
  }catch{if(op.valid()){view.value=null;error.value='设置读取失败，请刷新';}}
  finally{if(op.valid()){if(timer)clearTimeout(timer);timer=setTimeout(()=>void load(),globalThis.document.hidden?15000:4000);}}
}
async function probe(){
  if(busy.value||!view.value?.can_probe)return;
  const op=scope.begin('write');if(!op)return;
  busy.value=true; key??=globalThis.crypto.randomUUID();
  try{
    const r=await client.POST('/settings/model/probe',{body:{idempotency_key:key},signal:op.signal});if(!op.valid())return;
    if(r.data){key=null;await load();}else error.value='探测未接纳，请核对当前版本、权限和额度';
  }catch{if(op.valid())error.value='探测回执未知；再次点击将核对同一请求';}
  finally{if(op.valid())busy.value=false;}
}
onMounted(()=>void load());onBeforeUnmount(()=>{if(timer)clearTimeout(timer);});
</script>
<template>
  <section
    v-if="visible"
    class="model-panel"
    aria-labelledby="model-settings-title"
  >
    <header>
      <h2 id="model-settings-title">
        模型连接
      </h2><button
        type="button"
        @click="load"
      >
        刷新连接状态
      </button>
    </header>
    <p>模型连接与调用限额由平台管理者配置。本页显示当前新任务使用的连接；已开始任务保留原绑定版本。</p>
    <p
      v-if="error"
      role="alert"
    >
      {{ error }}
    </p>
    <template v-if="view">
      <strong>{{ labels[view.status] }}</strong>
      <p>配置版本：{{ view.configuration_version??'未设置' }} · 后台：{{ view.worker_available?'可用':'不可用或版本未匹配' }} · 业务资料外发：{{ view.model_data_export_enabled?'已允许':'未允许' }}</p>
      <p v-if="view.failure_code">
        最近失败：{{ codeLabel(view.failure_code) }}
      </p>
      <p v-if="view.status==='pending_restart'">
        当前进程配置尚未匹配，请联系平台管理者核对。
      </p>
      <p
        v-if="view.probe_state==='queued'||view.probe_state==='running'"
        role="status"
      >
        连接测试正在后台处理，请等待结果。
      </p>
      <p>连接测试会发出一次短请求，消耗模型服务商额度；结果未知不会自动重试。</p>
      <button
        type="button"
        :disabled="busy||!view.can_probe||['queued','running'].includes(view.probe_state??'')"
        @click="probe"
      >
        测试连接（消耗一次额度）
      </button>
      <p>模型：{{ view.model ?? '未绑定' }} · 平台连接版本：{{ view.platform_connection_version ?? '独立部署' }}</p>
      <dl v-if="view.limits">
        <template v-for="(label,field) in fields" :key="field">
          <dt>{{ label }}</dt><dd>{{ view.limits[field] ?? '未设次数上限' }}</dd>
        </template>
      </dl>
    </template>
  </section>
</template>
<style scoped>.model-panel{border:1px solid var(--border);border-radius:12px;background:var(--surface);padding:20px;min-width:0;overflow-wrap:anywhere}header{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}p{margin:12px 0;color:var(--text-secondary)}form{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-top:18px}label{display:grid;gap:6px}input{width:100%;box-sizing:border-box;min-width:0;padding:8px;border:1px solid var(--border);border-radius:6px;background:var(--surface);color:var(--text-primary)}@media(max-width:500px){form{grid-template-columns:1fr}}</style>
