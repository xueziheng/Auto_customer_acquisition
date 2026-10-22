import { createApp, nextTick } from "vue";
import { createMemoryHistory, createRouter } from "vue-router";
import { expect, it, vi } from "vitest";
import SmartInbox from "../src/views/inbox/SmartInbox.vue";

async function settle() { for (let i=0;i<30;i++) { await nextTick(); await new Promise(resolve=>setTimeout(resolve,0)); } }

it("keeps manual recovery status after classification and displays downstream failure", async()=>{
  let state="eligible";
  let classified=false;
  let recoveryForbidden=false;
  const message={message_id:"msg_one",direction:"inbound",sent_at:"2026-10-19T12:00:00Z",raw_artifact_ref:"artifact:one",outbound_message_id:"out_one",corrections:[],required_actions:[]};
  const category=()=>classified?"willing_to_continue":null;
  const projection=()=>({original_category:category(),effective_category:category(),classified_by:classified?"test-model":null,classified_at:classified?"2026-10-19T12:00:05Z":null});
  const client={
    identitySnapshot:()=>({identity:{tenantId:"tenant",employeeId:"owner",mode:"fixed-dev"},generation:0}),
    subscribeIdentity:()=>()=>{},
    GET:vi.fn(async(path:string)=>{
      let data:unknown;
      if(path==="/inbox/conversations")data=[{account_id:"acc_one",conversation_id:"con_one",channel:"email",latest_message_id:"msg_one",latest_message_at:message.sent_at,last_activity_at:message.sent_at,raw_artifact_ref:message.raw_artifact_ref,required_actions:[],correction_count:0,...projection()}];
      else if(path==="/inbox/conversations/{conversation_id}")data={account_id:"acc_one",conversation_id:"con_one",channel:"email",created_at:message.sent_at,last_inbound_at:message.sent_at,last_outbound_at:message.sent_at,messages:[{...message,...projection()}]};
      else if(path.endsWith("/retry-classification") && recoveryForbidden)return {response:new Response(null,{status:403})};
      else if(path.endsWith("/retry-classification"))data={message_id:"msg_one",state,can_retry:state==="eligible",reason:state==="eligible"?"not_dispatched":"manual_recovery",original_failure_preserved:true};
      else if(path.endsWith("/content"))data={subject:"reply",sender:"buyer@example.test",recipients:"owner@example.test",body_text:"I want to continue",html_only:false,truncated:false};
      else if(path==="/inbox/reply-work")data=[];
      else return {response:new Response(null,{status:404})};
      return {data,response:new Response()};
    }),
    POST:vi.fn(async()=>{state="queued";return {data:{message_id:"msg_one",state,can_retry:false,reason:"manual_recovery"},response:new Response()};}),
  };
  const router=createRouter({history:createMemoryHistory(),routes:[{path:"/inbox",component:SmartInbox}]});
  await router.push("/inbox");await router.isReady();
  const root=document.createElement("div");document.body.append(root);
  const app=createApp(SmartInbox);app.use(router);app.provide("tradeos-api-client",client);app.mount(root);
  const click=(label:string)=>{const button=Array.from(root.querySelectorAll("button")).find(x=>x.textContent?.trim()===label);expect(button).toBeTruthy();button!.click();};
  try {
    await settle();click("重新识别");await settle();expect(client.POST).toHaveBeenCalledTimes(1);
    state="classified";classified=true;click("刷新识别状态");await settle();
    expect(root.textContent).toContain("重新识别已保存，后续任务处理中");
    expect(root.textContent).toContain("原失败记录保留");
    state="failed";click("刷新识别状态");await settle();
    expect(root.textContent).toContain("本次恢复失败，请联系管理员核对");
    expect(root.textContent).toContain("原失败记录保留");
    expect(client.POST).toHaveBeenCalledTimes(1);
    recoveryForbidden=true;click("刷新识别状态");await settle();
    expect(root.textContent).toContain("I want to continue");
    expect(root.querySelector('[aria-label="回复识别恢复"]')).toBeNull();
  } finally {app.unmount();root.remove();}
});
