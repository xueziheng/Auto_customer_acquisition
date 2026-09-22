import { createApp, nextTick } from "vue";
import { it, expect, vi } from "vitest";
import SmartInbox from "../src/views/inbox/SmartInbox.vue";
import { createMemoryHistory, createRouter } from "vue-router";
import { createApiClient } from "../src/api/client";
it("会话显示可审阅下一问建议与canonical邮件下载，不以artifact拼地址",async()=>{
  const mid="msg_01K39P9M5D6K4A91YEQ80EJZ0Y",cid="con_01K39P9M5D6K4A91YEQ80EJZ0X";
  const requests:string[]=[];
  const fetch=vi.fn<typeof globalThis.fetch>(async input=>{
    const path=new URL((input as Request).url).pathname;requests.push(path);
    let data:unknown=[];
    if(path.endsWith("/conversations"))data=[{conversation_id:cid,account_id:"acc-one",effective_category:"provides_specification"}];
    else if(path.endsWith(cid))data={conversation_id:cid,account_id:"acc-one",channel:"email",messages:[{message_id:mid,direction:"inbound",raw_artifact_ref:"artifact:restricted",effective_category:"provides_specification",original_category:"provides_specification",corrections:[],required_actions:[]}]};
    else if(path.endsWith("next-questions"))data={conversation_id:cid,source_message_id:mid,need_id:"need-one",state:"suggested",completeness:2,topics:["quantity"],suggestions:["What quantity do you need?"]};
    else if(path.endsWith("/evidence"))return new Response("raw MIME",{headers:{"content-type":"application/octet-stream"}});
    return new Response(JSON.stringify(data),{headers:{"content-type":"application/json"}});
  });
  const root=document.createElement("div");document.body.append(root);const router=createRouter({history:createMemoryHistory(),routes:[{path:"/inbox",component:SmartInbox}]});await router.push("/inbox");
  const app=createApp(SmartInbox);app.use(router);app.provide("tradeos-api-client",createApiClient({baseUrl:"https://tradeos.test",fetch}));app.mount(root);
  for(let i=0;i<15;i++){await nextTick();await new Promise(r=>setTimeout(r,0));}
  expect(root.textContent).toContain("What quantity do you need?");expect(root.textContent).toContain("未发送");
  const create=vi.spyOn(URL,"createObjectURL").mockReturnValue("blob:test");const revoke=vi.spyOn(URL,"revokeObjectURL").mockImplementation(()=>{});
  [...root.querySelectorAll("button")].find(b=>b.textContent?.includes("下载邮件原件"))!.click();
  for(let i=0;i<10;i++){await nextTick();await new Promise(r=>setTimeout(r,0));}
  expect(requests).toContain(`/inbox/messages/${mid}/evidence`);expect(requests.some(path=>path.includes("artifact:"))).toBe(false);expect(create).toHaveBeenCalled();expect(revoke).toHaveBeenCalled();
  app.unmount();root.remove();vi.restoreAllMocks();
});
