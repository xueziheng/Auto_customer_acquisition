import { createApp, nextTick } from 'vue';
import { expect,it } from 'vitest';
import ModelSettingsPanel from '../src/views/settings/ModelSettingsPanel.vue';
import { createApiClient } from '../src/api/client';

it('打开设置不探测，只有显式点击消耗一次额度',async()=>{
 let probes=0;
 const provider={generation:()=>0,current:()=>({employeeId:'emp',tenantId:'tn',mode:'authenticated' as const}),subscribe:()=>()=>{}};
 const client=createApiClient({baseUrl:'https://test.local',fetch:async(input)=>{
  if((input as Request).url.endsWith('/health/capabilities'))return new Response(JSON.stringify([{name:'model',status:'enabled',reason:'composed'}]),{headers:{'content-type':'application/json'}});
  if((input as Request).method==='POST')probes++;
  return new Response(JSON.stringify({provider:'deepseek',status:'unverified',model:'test-model',configuration_version:'v1',can_probe:true,worker_available:true}),{headers:{'content-type':'application/json'}});
 }},provider);
 const host=document.createElement('div'),app=createApp(ModelSettingsPanel);app.provide('tradeos-api-client',client);app.mount(host);
 for(let i=0;i<5;i++){await nextTick();await new Promise(r=>setTimeout(r,0));}
 expect(probes).toBe(0);expect(host.querySelector('input[type=password]')).toBeNull();
 const button=[...host.querySelectorAll('button')].find(b=>b.textContent?.includes('测试连接'))!;button.click();
 for(let i=0;i<5;i++){await nextTick();await new Promise(r=>setTimeout(r,0));}
 expect(probes).toBe(1);app.unmount();
});

it('企业只读平台模型与限额，刷新展示新绑定且无保存入口',async()=>{
 const limits={window_seconds:86400,tenant_calls:3,employee_calls:3,tenant_concurrency:1,employee_concurrency:1,max_input_bytes:1024,max_output_tokens:128,timeout_seconds:10};
 let current={provider:'deepseek',status:'verified',model:'platform-model',configuration_version:'v1',can_probe:false,worker_available:true,model_data_export_enabled:true,limits};
 let writes=0;
 const provider={generation:()=>0,current:()=>({employeeId:'emp',tenantId:'tn',mode:'authenticated' as const}),subscribe:()=>()=>{}};
 const client=createApiClient({baseUrl:'https://test.local',fetch:async input=>{
  const request=input as Request;
  if(request.url.endsWith('/health/capabilities'))return new Response(JSON.stringify([{name:'model',status:'enabled'}]),{headers:{'content-type':'application/json'}});
  if(request.method==='POST')writes++;
  return new Response(JSON.stringify(current),{headers:{'content-type':'application/json'}});
 }},provider);
 const host=document.createElement('div'),app=createApp(ModelSettingsPanel);app.provide('tradeos-api-client',client);
 const flush=async()=>{for(let i=0;i<6;i++){await nextTick();await new Promise(r=>setTimeout(r,0));}};
 try {
  app.mount(host);await flush();
  expect(host.querySelector('input')).toBeNull();
  expect(host.querySelector('button[type=submit]')).toBeNull();
  expect(host.textContent).toContain('platform-model');
  expect(host.textContent).toContain('平台管理者');
  current={...current,model:'new-platform-model'};
  [...host.querySelectorAll('button')].find(b=>b.textContent?.includes('刷新连接状态'))!.click();await flush();
  expect(host.textContent).toContain('new-platform-model');expect(writes).toBe(0);
 } finally {app.unmount();}
});
