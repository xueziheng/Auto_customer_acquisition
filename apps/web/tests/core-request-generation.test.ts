import { createApp, nextTick, type Component, type App } from "vue";
import { createMemoryHistory, createRouter } from "vue-router";
import { afterEach, describe, expect, it } from "vitest";
import { createApiClient, type WebIdentityProvider } from "../src/api/client";
import SmartInbox from "../src/views/inbox/SmartInbox.vue";
import NotificationCenter from "../src/views/NotificationCenter.vue";
import NotificationBadge from "../src/components/NotificationBadge.vue";
import SendingIdentityCenter from "../src/views/SendingIdentityCenter.vue";
import ValidatedNeedDetail from "../src/views/demand-radar/ValidatedNeedDetail.vue";

const mounted: App[] = [];
afterEach(() => { mounted.splice(0).forEach(app => app.unmount()); document.body.replaceChildren(); });
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "content-type": "application/json" } });
function deferred<T>() { let resolve!: (value:T)=>void; let reject!: (value?:unknown)=>void; const promise = new Promise<T>((yes,no)=>{resolve=yes;reject=no;}); return {promise,resolve,reject}; }
async function flush() { for(let i=0;i<12;i++){await nextTick(); await new Promise(resolve=>setTimeout(resolve,0));} }
async function mount(component: Component, fetch: typeof globalThis.fetch, path="/inbox") {
  let generation=0; const listeners=new Set<()=>void>();
  const provider: WebIdentityProvider = { generation:()=>generation, current:()=>({employeeId:`emp-${generation}`,tenantId:"tenant",mode:"authenticated"}), subscribe:fn=>{listeners.add(fn);return ()=>listeners.delete(fn);} };
  const client=createApiClient({baseUrl:"https://tradeos.test",fetch},provider);
  const router=createRouter({history:createMemoryHistory(),routes:[{path:"/:pathMatch(.*)*",component},{path:"/demand/needs/:needId",component}]});
  await router.push(path); const root=document.createElement("div");document.body.append(root);
  const app=createApp(component); app.use(router); app.provide("tradeos-api-client",client); app.mount(root);mounted.push(app);
  return {root,app,router,switchIdentity(){generation++;listeners.forEach(fn=>fn());},listeners};
}
const notification = {notification_id:"n-old",context:{kind:"handoff_requested",handoff_id:"h-1"},deep_link:"/crm/handoffs?handoff_id=h-1",read_at:null,created_at:"2026-09-06T00:00:00Z",priority:"normal"};
const identity = {identity_id:"sid-old",address:"old-private@example.test",domain:"example.test",role:"cold_outreach",state:"created",created_at:"2026-09-06T00:00:00Z",warmup_complete:false,remaining_today:0};
const item = {conversation_id:"con-old",account_id:"old-private-account",channel:"email",effective_category:"clear_interest",original_category:"clear_interest",last_activity_at:"2026-09-06T00:00:00Z",latest_message_at:null,latest_message_id:null,raw_artifact_ref:null,required_actions:[],correction_count:0};

describe("核心页面身份与请求生命周期",()=>{
  it.each([
    ["通知", NotificationCenter, [notification], "/notifications", "n-old"],
    ["发件身份", SendingIdentityCenter, [identity], "/crm/sending-identities", "old-private@example.test"],
    ["会话", SmartInbox, [item], "/inbox", "old-private-account"],
  ] as const)("%s 身份切换后旧 success/error/finally 不覆盖新权限态",async(_name,component,data,path,secret)=>{
    const old=deferred<Response>(); let first=true;
    const page=await mount(component,async()=>{if(first){first=false;return old.promise;}return json({},403);},path);
    page.switchIdentity();await flush();
    old.resolve(json(data));await flush();
    expect(page.root.textContent).not.toContain(secret);
    expect(page.root.textContent).toMatch(/无权|无法访问|无法查看/);
    page.app.unmount(); expect(page.listeners.size).toBe(0);
  });
  it("徽标首次请求和身份切换后显示未知，不保留前人计数",async()=>{
    const old=deferred<Response>();let calls=0;
    const page=await mount(NotificationBadge,async()=>++calls===1?json([notification]):old.promise,"/notifications");
    await flush(); expect(page.root.textContent).toContain("1");
    page.switchIdentity(); await nextTick();
    expect(page.root.querySelector(".badge-num")?.textContent).toBe("未知");
    old.resolve(json({},403));await flush();expect(page.root.querySelector(".badge-num")?.textContent).toBe("未知");
  });
  it("通知身份切换后旧 markRead 不广播新身份刷新或解除新写锁",async()=>{
    const write=deferred<Response>();let broadcast=0;
    const event=()=>broadcast++;window.addEventListener("tradeos:notifications-changed",event);
    const page=await mount(NotificationCenter,async input=>(input as Request).method==="POST"?write.promise:json([notification]),"/notifications");
    await flush();page.root.querySelector<HTMLElement>("li")!.click();await nextTick();
    [...page.root.querySelectorAll("button")].find(b=>b.textContent?.includes("已读"))!.click();await flush();
    page.switchIdentity();await flush();write.resolve(json({...notification,read_at:"2026-09-06T01:00:00Z"}));await flush();
    expect(broadcast).toBe(0);window.removeEventListener("tradeos:notifications-changed",event);
  });
  it("需求路由改变后旧错误与 finally 不提前结束新加载",async()=>{
    const old=deferred<Response>(),fresh=deferred<Response>();
    const page=await mount(ValidatedNeedDetail,async input=>new URL((input as Request).url).pathname.endsWith("need-old")?old.promise:fresh.promise,"/demand/needs/need-old");
    await page.router.push("/demand/needs/need-new");old.reject(new Error("old-private-error"));await flush();
    expect(page.root.textContent).toContain("正在读取");expect(page.root.textContent).not.toContain("无法连接需求服务");
    fresh.resolve(json({},404));await flush();expect(page.root.textContent).toContain("不存在或不可见");
  });
});

it.each([NotificationCenter,NotificationBadge,SendingIdentityCenter,SmartInbox,ValidatedNeedDetail])("卸载清除身份订阅，旧失败不得发出额外请求",async component=>{
 const old=deferred<Response>();let calls=0;
 const page=await mount(component,async()=>{calls++;return old.promise;},"/demand/needs/need-old");
 await flush();expect(page.listeners.size).toBeGreaterThan(0);page.app.unmount();expect(page.listeners.size).toBe(0);
 await flush();const count=calls;old.reject(new Error("old failure"));page.switchIdentity();await flush();expect(calls).toBe(count);
});

it("选择不同会话立即清旧原件，旧纠正失败不能覆盖新会话",async()=>{
 const correction=deferred<Response>();
 const message=(id:string)=>({message_id:id,direction:"inbound",effective_category:"clear_interest",original_category:"clear_interest",corrections:[],required_actions:[],raw_artifact_ref:id+"-private"});
 const page=await mount(SmartInbox,async input=>{
  const request=input as Request;const path=new URL(request.url).pathname;
  if(request.method==="POST")return correction.promise;
  if(path.endsWith("next-questions"))return json({},503);
  if(path.endsWith("/conversations"))return json([item,{...item,conversation_id:"con-new",account_id:"new-account"}]);
  if(path.endsWith("con-old"))return json({conversation_id:"con-old",account_id:"old-account",channel:"email",messages:[message("msg-old")]});
  return json({conversation_id:"con-new",account_id:"new-account",channel:"email",messages:[message("msg-new")]});
 });
 await flush();[...page.root.querySelectorAll("button")].find(b=>b.textContent?.includes("提交纠正"))!.click();await nextTick();
 [...page.root.querySelectorAll(".conversation-list button")].find(b=>b.textContent?.includes("new-account"))!.dispatchEvent(new MouseEvent("click"));await nextTick();
 expect(page.root.textContent).not.toContain("msg-old-private");await flush();correction.reject(new Error("old correction"));await flush();
 expect(page.root.textContent).toContain("msg-new-private");expect(page.root.textContent).not.toContain("人工纠正结果待核对");
});

it("徽标已读取后身份失效401清除受限计数",async()=>{
 let calls=0;
 const page=await mount(NotificationBadge,async()=>++calls===1?json([notification]):json({},401),"/notifications");
 await flush();expect(page.root.querySelector(".badge-num")?.textContent).toBe("1");
 window.dispatchEvent(new Event("tradeos:notifications-changed"));await flush();
 expect(page.root.querySelector(".badge-num")?.textContent).toBe("未知");
});
