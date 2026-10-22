import { createApp, nextTick } from "vue";
import { describe, it, expect, vi } from "vitest";
import type { components } from "../src/api/api";
import ReplyRetry from "../src/views/inbox/ReplyRetry.vue";

type RetryResult = { data: Partial<components["schemas"]["ReplyRetryView"]> | undefined; response: Response };

async function flush() { for(let i=0;i<8;i++) { await nextTick(); await Promise.resolve(); } }
function fixture(unknown=false) {
  let listener=()=>{};
  let generation=0;
  const client={
    identitySnapshot:()=>({identity:{tenantId:"tenant",employeeId:"owner",mode:"fixed-dev"},generation}),
    subscribeIdentity:(fn:()=>void)=>{listener=fn;return ()=>{};},
    GET:vi.fn(async(): Promise<RetryResult>=>({data:{message_id:"msg_test",state:"eligible",reason:"not_dispatched",can_retry:true},response:new Response()})),
    POST:vi.fn(async()=>{if(unknown)throw new Error("unknown");return {data:{message_id:"msg_test",state:"queued",reason:"manual_recovery",can_retry:false},response:new Response()};}),
  };
  const root=document.createElement("div");document.body.append(root);
  const app=createApp(ReplyRetry,{messageId:"msg_test"});app.provide("tradeos-api-client",client);app.mount(root);
  return {client,root,close:()=>{app.unmount();root.remove();},changeIdentity:()=>{generation++;listener();}};
}
describe("reply classification recovery",()=>{
  it("one click creates one command; refresh only reads",async()=>{
    const f=fixture();try {
      await flush();const button=Array.from(f.root.querySelectorAll("button")).find(b=>b.textContent==="重新识别")!;
      button.click();button.click();await flush();expect(f.client.POST).toHaveBeenCalledTimes(1);
      expect(f.root.textContent).toContain("已提交");
      Array.from(f.root.querySelectorAll("button")).find(b=>b.textContent==="刷新识别状态")!.click();
      await flush();expect(f.client.POST).toHaveBeenCalledTimes(1);
    }finally{f.close();}
  });
  it("unknown result retains the original key and never retries automatically",async()=>{
    const f=fixture(true);try {
      await flush();Array.from(f.root.querySelectorAll("button")).find(b=>b.textContent==="重新识别")!.click();await flush();
      expect(f.root.textContent).toContain("结果待核对");expect(f.client.POST).toHaveBeenCalledTimes(1);
      Array.from(f.root.querySelectorAll("button")).find(b=>b.textContent==="刷新识别状态")!.click();await flush();
      Array.from(f.root.querySelectorAll("button")).find(b=>b.textContent==="使用原请求继续")!.click();await flush();
      const calls=f.client.POST.mock.calls as unknown as [string,{params:{header:{"Idempotency-Key":string}}}][];
      expect(calls[0]![1].params.header["Idempotency-Key"]).toBe(calls[1]![1].params.header["Idempotency-Key"]);
      f.changeIdentity();await flush();expect(f.root.textContent).not.toContain("结果待核对");
    }finally{f.close();}
  });
});

 it("legacy test expiry can be continued once; normal requests show no continuation",async()=>{
    const f=fixture(true);try {
      await flush();expect(f.root.textContent).not.toContain("继续未开始的识别");
      f.client.GET.mockResolvedValue({data:{message_id:"msg_test",state:"expired",reason:"manual_recovery",can_retry:false,can_resume:true,resumed:false},response:new Response()});
      Array.from(f.root.querySelectorAll("button")).find(b=>b.textContent==="刷新识别状态")!.click();await flush();
      const button=Array.from(f.root.querySelectorAll("button")).find(b=>b.textContent==="继续未开始的识别")!;
      expect(button).toBeDefined();button.click();button.click();await flush();
      expect(f.client.POST).toHaveBeenCalledTimes(1);
      expect((f.client.POST.mock.calls as unknown as [string,unknown][])[0]?.[0]).toBe("/inbox/messages/{message_id}/retry-classification/resume");
      Array.from(f.root.querySelectorAll("button")).find(b=>b.textContent==="刷新识别状态")!.click();await flush();
      Array.from(f.root.querySelectorAll("button")).find(b=>b.textContent==="继续未开始的识别")!.click();await flush();
      const calls=f.client.POST.mock.calls as unknown as [string,{params:{header:{"Idempotency-Key":string}}}][];
      expect(calls[0]![1].params.header["Idempotency-Key"]).toBe(calls[1]![1].params.header["Idempotency-Key"]);
      f.changeIdentity();await flush();expect(f.root.textContent).not.toContain("继续未开始的识别");
    }finally{f.close();}
 });

 it("stopped failure requires cost confirmation and submits the observed attempt",async()=>{
   const f=fixture();try {
     await flush();
     f.client.GET.mockResolvedValue({data:{message_id:"msg_test",state:"failed",reason:"manual_recovery",can_retry:true,expected_sequence:1,requires_cost_confirmation:true},response:new Response()});
     Array.from(f.root.querySelectorAll("button")).find(b=>b.textContent==="刷新识别状态")!.click();await flush();
     const checkbox=f.root.querySelector<HTMLInputElement>('input[type="checkbox"]');
     expect(checkbox).not.toBeNull();
     const button=Array.from(f.root.querySelectorAll("button")).find(b=>b.textContent==="重新识别")!;
     expect(button.disabled).toBe(true);expect(f.client.POST).not.toHaveBeenCalled();
     checkbox!.checked=true;checkbox!.dispatchEvent(new Event("change",{bubbles:true}));await flush();
     button.click();button.click();await flush();
     const calls=f.client.POST.mock.calls as unknown as [string,{body:unknown}][];
     expect(calls).toHaveLength(1);
     expect(calls[0]![0]).toBe("/inbox/messages/{message_id}/retry-classification/failed");
     expect(calls[0]![1].body).toEqual({expected_sequence:1,acknowledge_cost:true});
   }finally{f.close();}
 });

 it("a newer failed attempt resets consent while same-attempt uncertainty keeps its key",async()=>{
   const f=fixture(true);try {
     await flush();
     const observed=(sequence:number): RetryResult=>({data:{message_id:"msg_test",state:"failed",reason:"manual_recovery",can_retry:true,expected_sequence:sequence,requires_cost_confirmation:true},response:new Response()});
     const refresh=async()=>{Array.from(f.root.querySelectorAll("button")).find(b=>b.textContent==="刷新识别状态")!.click();await flush();};
     const consent=async()=>{const c=f.root.querySelector<HTMLInputElement>('input[type="checkbox"]')!;c.checked=true;c.dispatchEvent(new Event("change",{bubbles:true}));await flush();};
     f.client.GET.mockResolvedValue(observed(1));await refresh();await consent();
     Array.from(f.root.querySelectorAll("button")).find(b=>b.textContent==="重新识别")!.click();await flush();
     await refresh();
     Array.from(f.root.querySelectorAll("button")).find(b=>b.textContent==="使用原请求继续")!.click();await flush();
     let calls=f.client.POST.mock.calls as unknown as [string,{params:{header:{"Idempotency-Key":string}},body:{expected_sequence:number}}][];
     expect(calls[0]![1].body).toEqual(calls[1]![1].body);
     expect(calls[0]![1].params.header).toEqual(calls[1]![1].params.header);
     f.client.GET.mockResolvedValue(observed(2));await refresh();
     expect(f.client.POST).toHaveBeenCalledTimes(2);
     expect(f.root.querySelector<HTMLInputElement>('input[type="checkbox"]')!.checked).toBe(false);
     const button=Array.from(f.root.querySelectorAll("button")).find(b=>b.textContent==="重新识别")!;
     expect(button.disabled).toBe(true);await consent();button.click();await flush();
     calls=f.client.POST.mock.calls as unknown as typeof calls;
     expect(calls[2]![1].body.expected_sequence).toBe(2);
     expect(calls[2]![1].params.header["Idempotency-Key"]).not.toBe(calls[0]![1].params.header["Idempotency-Key"]);
   }finally{f.close();}
 });

 it.each(["running", "read-error"])("%s clears consent from an older failure",async(mode)=>{
   const f=fixture();try {
     await flush();
     const refresh=async()=>{Array.from(f.root.querySelectorAll("button")).find(b=>b.textContent==="刷新识别状态")!.click();await flush();};
     f.client.GET.mockResolvedValue({data:{message_id:"msg_test",state:"failed",can_retry:true,expected_sequence:1,requires_cost_confirmation:true},response:new Response()});await refresh();
     const c=f.root.querySelector<HTMLInputElement>('input[type="checkbox"]')!;c.checked=true;c.dispatchEvent(new Event("change",{bubbles:true}));await flush();
     f.client.GET.mockResolvedValue({data: mode === "running" ? {message_id:"msg_test",state:"running",can_retry:false,expected_sequence:null} : undefined,response:new Response(null,{status:mode === "running" ? 200 : 503})});await refresh();
     f.client.GET.mockResolvedValue({data:{message_id:"msg_test",state:"failed",can_retry:true,expected_sequence:2,requires_cost_confirmation:true},response:new Response()});await refresh();
     expect(f.root.querySelector<HTMLInputElement>('input[type="checkbox"]')!.checked).toBe(false);
     expect(f.client.POST).not.toHaveBeenCalled();
   }finally{f.close();}
 });
