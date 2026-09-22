import { createApp, nextTick } from 'vue';
import { expect,it } from 'vitest';
import ModelSettingsPanel from '../src/views/settings/ModelSettingsPanel.vue';
import { createApiClient } from '../src/api/client';

it('打开设置不探测，只有显式点击消耗一次额度',async()=>{
 let probes=0;
 const provider={generation:()=>0,current:()=>({employeeId:'emp',tenantId:'tn',mode:'authenticated' as const}),subscribe:()=>()=>{}};
 const client=createApiClient({baseUrl:'https://test.local',fetch:async(input)=>{
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
