import { createApp, nextTick, type Component, type App } from 'vue';
import { createMemoryHistory, createRouter } from 'vue-router';
import { it, expect, afterEach } from 'vitest';
import SendingIdentityCenter from '../src/views/SendingIdentityCenter.vue';
import SmartInbox from '../src/views/inbox/SmartInbox.vue';
import { createApiClient } from '../src/api/client';
const mounted:App[]=[];
afterEach(()=>{mounted.splice(0).forEach(a=>a.unmount());document.body.replaceChildren();});
const json=(x:unknown,status=200)=>new Response(JSON.stringify(x),{status,headers:{'content-type':'application/json'}});
function deferred(){let resolve!:(r:Response)=>void;let reject!:(error:Error)=>void;const promise=new Promise<Response>((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};}
async function flush(){for(let i=0;i<12;i++){await nextTick();await new Promise(r=>setTimeout(r,0));}}
async function mount(component:Component,fetch:typeof globalThis.fetch,path:string){
 const provider={generation:()=>0,current:()=>({employeeId:'boss-one',tenantId:'tenant',mode:'authenticated' as const}),subscribe:()=>()=>{}};
 const router=createRouter({history:createMemoryHistory(),routes:[{path:'/:pathMatch(.*)*',component}]});await router.push(path);
 const root=document.createElement('div');document.body.append(root);const app=createApp(component);app.use(router);app.provide('tradeos-api-client',createApiClient({baseUrl:'https://tradeos.test',fetch},provider));app.mount(root);mounted.push(app);await flush();return root;
}
function button(root:HTMLElement,text:string){return [...root.querySelectorAll('button')].find(x=>x.textContent?.trim()===text||(text==='刷新'&&x.textContent?.trim()==='加载中…'))!;}
it('管理403后旧复核列表不得恢复受限内容',async()=>{
 const old=deferred();let denied=false;
 const root=await mount(SendingIdentityCenter,async input=>{
  const p=new URL((input as Request).url).pathname;
  if(p.endsWith('/reviews'))return old.promise;
  if(p.endsWith('/management'))return denied?json({},403):json([]);
  return json({state:'disabled'});
 },'/crm/sending-identities');
 button(root,'读取未关联邮件复核').click();await flush();denied=true;button(root,'刷新状态').click();await flush();
 expect(root.textContent).toContain('当前账号无法查看发件身份');
 old.resolve(json([{review_id:'old-private-review',reason:'protected-review-summary',created_at:'2026-09-06',archived:false}]));await flush();
 expect(root.textContent).not.toContain('protected-review-summary');
});
it('会话列表403后旧详情不得恢复受限原件',async()=>{
 const old=deferred();let denied=false;
 const item=(id:string)=>({conversation_id:id,account_id:id+'-account',effective_category:null,required_actions:[]});
 const detail=(id:string)=>({conversation_id:id,account_id:id+'-account',channel:'email',messages:[{message_id:'message-'+id,direction:'outbound',raw_artifact_ref:'protected-'+id,corrections:[],required_actions:[]}]});
 const root=await mount(SmartInbox,async input=>{
  const p=new URL((input as Request).url).pathname;
  if(p.endsWith('/conversations'))return denied?json({},403):json([item('old'),item('second')]);
  if(p.endsWith('/second'))return old.promise;
  return json(detail('old'));
 },'/inbox');
 [...root.querySelectorAll('.conversation-list button')].find(b=>b.textContent?.includes('second-account'))!.dispatchEvent(new MouseEvent('click'));await flush();
 denied=true;button(root,'刷新').click();await flush();expect(root.textContent).toContain('当前身份无权访问 Smart Inbox');
 old.resolve(json(detail('second')));await flush();expect(root.textContent).not.toContain('protected-second');expect(root.textContent).not.toContain('second-account');expect(root.textContent).not.toContain('下载邮件原件');
});

const sender=(id:string)=>({identity_id:id,address:id+'@example.test',domain:'example.test',state:'auth_pending',auth:{checked_at:'2026-09-06T00:00:00Z',spf_passed:true,dkim_passed:true,dmarc_passed:true,failures:[]},role:'cold_outreach',remaining_today:0});
async function warmup(root:HTMLElement){
 const target=root.querySelector<HTMLInputElement>('[aria-label="预热目标日量"]')!;target.value='15';target.dispatchEvent(new Event('input'));await nextTick();
 expect(button(root,'启动预热').disabled).toBe(false);button(root,'启动预热').click();await nextTick();button(root,'确认启动预热').click();await flush();
}
it.each(['success','error'] as const)('管理拒绝使旧预热%s与finally失效，不污染恢复后的新写锁',async outcome=>{
 const old=deferred(),fresh=deferred();let phase='initial',writes=0;
 const root=await mount(SendingIdentityCenter,async input=>{
  const req=input as Request,path=new URL(req.url).pathname;
  if(req.method==='POST')return ++writes===1?old.promise:fresh.promise;
  if(path.endsWith('/management'))return phase==='denied'?json({},403):json([sender(phase==='initial'?'sid-old':'sid-new')]);
  return json({state:'disabled'});
 },'/crm/sending-identities');
 await warmup(root);phase='denied';button(root,'刷新状态').click();await flush();
 expect(root.textContent).not.toContain('sid-old@example.test');expect(button(root,'刷新状态').disabled).toBe(false);
 phase='recovered';button(root,'刷新状态').click();await flush();await warmup(root);
 expect(writes).toBe(2);expect(root.querySelector<HTMLInputElement>('[aria-label="发件地址"]')!.disabled).toBe(true);
 if(outcome==='success')old.resolve(json({...sender('sid-old'),state:'warming'}));else old.reject(new Error('old command error'));
 await flush();expect(root.textContent).not.toContain('sid-old@example.test');expect(root.textContent).not.toContain('操作结果待核对');expect(root.textContent).not.toContain('预热已启动');expect(root.querySelector<HTMLInputElement>('[aria-label="发件地址"]')!.disabled).toBe(true);
 fresh.resolve(json({...sender('sid-new'),state:'warming'}));await flush();expect(root.textContent).toContain('预热已启动');expect(root.querySelector<HTMLInputElement>('[aria-label="发件地址"]')!.disabled).toBe(false);
});
it('管理401拒绝使旧精确身份读取失效',async()=>{
 const exact=deferred();let denied=false;
 const root=await mount(SendingIdentityCenter,async input=>{
  const req=input as Request,path=new URL(req.url).pathname;
  if(req.method==='POST')throw new Error('response lost');
  if(path.endsWith('/management'))return denied?json({},401):json([sender('sid-exact')]);
  if(path.endsWith('/sid-exact'))return exact.promise;
  return json({state:'disabled'});
 },'/crm/sending-identities');
 await warmup(root);button(root,'核对原预热身份当前状态').click();await flush();denied=true;button(root,'刷新状态').click();await flush();
 exact.resolve(json({...sender('sid-exact'),state:'warming'}));await flush();expect(root.textContent).not.toContain('sid-exact');expect(root.textContent).not.toContain('已读取精确身份');expect(button(root,'刷新状态').disabled).toBe(false);
});
it('复核404撤权同步收尾在途管理加载，旧finally不结束恢复加载',async()=>{
 const oldList=deferred(),freshList=deferred();let lists=0;
 const root=await mount(SendingIdentityCenter,async input=>{
  const path=new URL((input as Request).url).pathname;
  if(path.endsWith('/management')){lists++;return lists===1?json([]):lists===2?oldList.promise:freshList.promise;}
  if(path.endsWith('/reviews'))return json({},404);
  return json({state:'disabled'});
 },'/crm/sending-identities');
 button(root,'刷新状态').click();await flush();expect(button(root,'刷新状态').disabled).toBe(true);
 button(root,'读取未关联邮件复核').click();await flush();expect(button(root,'刷新状态').disabled).toBe(false);
 button(root,'刷新状态').click();await flush();oldList.reject(new Error('old list error'));await flush();
 expect(button(root,'刷新状态').disabled).toBe(true);expect(root.textContent).not.toContain('加载失败');
 freshList.resolve(json([sender('sid-recovered')]));await flush();expect(button(root,'刷新状态').disabled).toBe(false);expect(root.textContent).toContain('sid-recovered@example.test');
});
const inboxItem=(id:string)=>({conversation_id:id,account_id:id+'-account',effective_category:'clear_interest',required_actions:[]});
const inboxDetail=(id:string)=>({conversation_id:id,account_id:id+'-account',channel:'email',messages:[{message_id:'msg-'+id,direction:'inbound',effective_category:'clear_interest',raw_artifact_ref:'protected-'+id,corrections:[],required_actions:[]}]});
it.each(['success','error'] as const)('列表拒绝后的旧纠正%s不得在恢复列表加载时请求详情或回写finally',async outcome=>{
 const correction=deferred(),recovery=deferred();let lists=0,detailReads=0;
 const root=await mount(SmartInbox,async input=>{
  const req=input as Request,path=new URL(req.url).pathname;
  if(req.method==='POST')return correction.promise;
  if(path.endsWith('/conversations')){lists++;return lists===1?json([inboxItem('first')]):lists===2?json({},403):recovery.promise;}
  if(path.endsWith('/next-questions'))return json({},503);
  detailReads++;return json(inboxDetail(path.endsWith('/first')?'first':'recovered'));
 },'/inbox');
 button(root,'提交纠正').click();await flush();button(root,'刷新').click();await flush();expect(root.textContent).not.toContain('protected-first');
 button(root,'刷新').click();await flush();expect(button(root,'刷新').disabled).toBe(true);
 if(outcome==='success')correction.resolve(json({}));else correction.reject(new Error('old correction error'));
 await flush();expect(detailReads).toBe(1);expect(root.textContent).not.toContain('人工纠正结果待核对');expect(root.textContent).not.toContain('人工纠正已记录');expect(button(root,'刷新').disabled).toBe(true);
 recovery.resolve(json([inboxItem('recovered')]));await flush();expect(root.textContent).toContain('protected-recovered');expect(button(root,'刷新').disabled).toBe(false);
});
it('列表401后旧详情错误不得覆盖拒绝消息，404拒绝后结束加载',async()=>{
 const old=deferred();let lists=0;
 const root=await mount(SmartInbox,async input=>{
  const path=new URL((input as Request).url).pathname;
  if(path.endsWith('/conversations')){lists++;return lists===1?json([inboxItem('first'),inboxItem('second')]):json({},lists===2?401:404);}
  if(path.endsWith('/second'))return old.promise;
  if(path.endsWith('/next-questions'))return json({},503);
  return json(inboxDetail('first'));
 },'/inbox');
 [...root.querySelectorAll('.conversation-list button')].find(b=>b.textContent?.includes('second-account'))!.dispatchEvent(new MouseEvent('click'));await flush();button(root,'刷新').click();await flush();
 old.reject(new Error('old detail error'));await flush();expect(root.textContent).toContain('当前身份无权访问 Smart Inbox');expect(root.textContent).not.toContain('会话详情加载失败');expect(button(root,'刷新').disabled).toBe(false);
 button(root,'刷新').click();await flush();expect(root.textContent).toContain('所选会话不存在或不可见');expect(button(root,'刷新').disabled).toBe(false);expect(root.querySelector('.conversation-detail')?.getAttribute('aria-busy')).not.toBe('true');
});
