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

it('未装配模型时不读取登录专属设置接口',async()=>{
 const paths:string[]=[];
 const provider={generation:()=>0,current:()=>({employeeId:'emp',tenantId:'tn',mode:'fixed-dev' as const}),subscribe:()=>()=>{}};
 const client=createApiClient({baseUrl:'https://test.local',fetch:async input=>{
  paths.push(new URL((input as Request).url).pathname);
  return new Response('[]',{headers:{'content-type':'application/json'}});
 }},provider);
 const app=createApp(ModelSettingsPanel);app.provide('tradeos-api-client',client);app.mount(document.createElement('div'));
 for(let i=0;i<5;i++){await nextTick();await new Promise(r=>setTimeout(r,0));}
 expect(paths).toEqual(['/health/capabilities']);app.unmount();
});

it('编辑期间后台版本变化时阻止覆盖，放弃旧修改后可基于新版本保存',async()=>{
 const limits={window_seconds:60,tenant_calls:10,employee_calls:5,tenant_concurrency:2,employee_concurrency:1,max_input_bytes:1024,max_output_tokens:128,timeout_seconds:10};
 let current={provider:'deepseek',status:'unverified',model:'model-one',configuration_version:'v1',can_probe:true,worker_available:true,model_data_export_enabled:false,limits};
 const writes:Record<string,unknown>[]=[];
 const provider={generation:()=>0,current:()=>({employeeId:'emp',tenantId:'tn',mode:'authenticated' as const}),subscribe:()=>()=>{}};
 const client=createApiClient({baseUrl:'https://test.local',fetch:async input=>{
  const request=input as Request;
  if(request.url.endsWith('/health/capabilities'))return new Response(JSON.stringify([{name:'model',status:'enabled',reason:'composed'}]),{headers:{'content-type':'application/json'}});
  if(request.method==='POST'){writes.push(await request.json());return new Response(JSON.stringify(current),{headers:{'content-type':'application/json'}});}
  return new Response(JSON.stringify(current),{headers:{'content-type':'application/json'}});
 }},provider);
 const host=document.createElement('div'),app=createApp(ModelSettingsPanel);app.provide('tradeos-api-client',client);
 const flush=async()=>{for(let i=0;i<6;i++){await nextTick();await new Promise(r=>setTimeout(r,0));}};
 try {
  app.mount(host);await flush();
  const edit=host.querySelector('input[maxlength="128"]') as HTMLInputElement;
  edit.value='my-edit';edit.dispatchEvent(new Event('input',{bubbles:true}));await flush();
  current={...current,configuration_version:'v2',model:'other-admin-model',limits:{...limits,tenant_calls:3}};
  [...host.querySelectorAll('button')].find(b=>b.textContent?.includes('刷新连接状态'))!.click();await flush();
  const save=host.querySelector('button[type=submit]') as HTMLButtonElement;
  expect(save.disabled).toBe(true);
  expect(edit.value).toBe('my-edit');
  expect(host.textContent).toContain('配置已被更新');
  expect(writes).toHaveLength(0);
  [...host.querySelectorAll('button')].find(b=>b.textContent?.includes('放弃修改并载入当前配置'))!.click();await flush();
  expect(edit.value).toBe('other-admin-model');
  edit.value='my-new-edit';edit.dispatchEvent(new Event('input',{bubbles:true}));await flush();
  expect(save.disabled).toBe(false);
  host.querySelector('form')!.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));await flush();
  expect(writes).toHaveLength(1);expect(writes[0]?.expected_version).toBe('v2');
  expect(writes[0]?.limits).toEqual({...limits,tenant_calls:3});
 } finally {app.unmount();}
});
