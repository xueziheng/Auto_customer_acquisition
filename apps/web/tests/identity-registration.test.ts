import { createApp, nextTick } from "vue";
import { it, expect, vi } from "vitest";
import SendingIdentityCenter from "../src/views/SendingIdentityCenter.vue";
import { createApiClient } from "../src/api/client";
const flush=async()=>{for(let i=0;i<15;i++){await nextTick();await new Promise(r=>setTimeout(r,0));}};
it("登记先确认，响应丢失后冻结原payload并以原域幂等核对winner",async()=>{
 const writes:unknown[]=[];
 const fetch=vi.fn<typeof globalThis.fetch>(async input=>{
  const req=input as Request;
  if(req.method==="POST") { writes.push(await req.json()); if(writes.length===1)throw new Error("lost"); return new Response(JSON.stringify({identity_id:"sid-winner",address:"sales@cold.example.test",domain:"cold.example.test",state:"created",role:"cold_outreach",remaining_today:0}),{headers:{"content-type":"application/json"}}); }
  return new Response(JSON.stringify(new URL(req.url).pathname.endsWith("status")?{state:"disabled"}:[]),{headers:{"content-type":"application/json"}});
 });
 const root=document.createElement("div");document.body.append(root); const app=createApp(SendingIdentityCenter);app.provide("tradeos-api-client",createApiClient({baseUrl:"https://tradeos.test",fetch}));app.mount(root);await flush();
 const address=root.querySelector<HTMLInputElement>('[aria-label="发件地址"]');expect(address).not.toBeNull();
 address!.value="sales@cold.example.test";address!.dispatchEvent(new Event("input"));
 const domain=root.querySelector<HTMLInputElement>('[aria-label="发件域名"]')!;domain.value="cold.example.test";domain.dispatchEvent(new Event("input"));await nextTick();
 [...root.querySelectorAll("button")].find(b=>b.textContent?.trim()==="登记发件身份")!.click();await nextTick();expect(writes).toHaveLength(0);
 [...root.querySelectorAll('[role="dialog"] button')].find(b=>b.textContent?.trim()==="确认登记")!.dispatchEvent(new MouseEvent("click"));await flush();
 expect(root.textContent).toContain("结果待核对");expect(address!.disabled).toBe(true);
 [...root.querySelectorAll("button")].find(b=>b.textContent?.includes("按原登记内容核对"))!.click();await flush();
 expect(writes).toHaveLength(2);expect(writes[1]).toEqual(writes[0]);expect(root.textContent).toContain("sid-winner");
 app.unmount();root.remove();
});

it("预热响应丢失仅核对精确sid，目标相同不能宣称本次成功或自动重启",async()=>{
 const sid="sid-exact",paths:string[]=[];let writes=0;
 const identity={identity_id:sid,address:"sales@example.test",domain:"example.test",state:"auth_pending",auth:{checked_at:"2026-09-06T00:00:00Z",spf_passed:true,dkim_passed:true,dmarc_passed:true,failures:[]},role:"cold_outreach",remaining_today:0};
 const fetch=vi.fn<typeof globalThis.fetch>(async input=>{
  const req=input as Request,path=new URL(req.url).pathname;paths.push(req.method+" "+path);
  if(req.method==="POST"){writes++;expect(await req.json()).toEqual({target_daily_volume:15,confirmed:true});throw new Error("lost");}
  const value=path.endsWith("/management")?[identity]:path.endsWith(sid)?{...identity,state:"warming",target_daily_volume:15,warmup_day:1}: {state:"active",identity_id:sid};
  return new Response(JSON.stringify(value),{headers:{"content-type":"application/json"}});
 });
 const root=document.createElement("div");document.body.append(root);const app=createApp(SendingIdentityCenter);app.provide("tradeos-api-client",createApiClient({baseUrl:"https://tradeos.test",fetch}));app.mount(root);await flush();
 expect(root.textContent).toContain("已绑定，处理状态待核对");expect(root.textContent).not.toContain("正在同步");
 const input=root.querySelector<HTMLInputElement>('[aria-label="预热目标日量"]')!;input.value="15";input.dispatchEvent(new Event("input"));await nextTick();
 const button=[...root.querySelectorAll("button")].find(b=>b.textContent?.trim()==="启动预热")!;button.click();await nextTick();expect(writes).toBe(0);
 [...root.querySelectorAll("button")].find(b=>b.textContent?.trim()==="确认启动预热")!.click();await flush();expect(root.textContent).toContain("待核对");
 [...root.querySelectorAll("button")].find(b=>b.textContent?.trim()==="核对原预热身份当前状态")!.click();await flush();
 expect(writes).toBe(1);expect(paths).toContain(`GET /crm/sending-identities/${sid}`);expect(root.textContent).toContain("原预热请求结果仍待核对");expect(root.textContent).not.toContain("预热已启动，日期");expect(button.disabled).toBe(true);
 app.unmount();root.remove();
});
