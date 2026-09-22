import { createApp, nextTick, type Component, type App } from 'vue';
import { createMemoryHistory, createRouter } from 'vue-router';
import { it, expect, afterEach, vi } from 'vitest';
import { createApiClient } from '../src/api/client';
import type { components } from '../src/api/api';
import CampaignCenter from '../src/views/campaigns/CampaignCenter.vue';
import SettingsCenter from '../src/views/settings/SettingsCenter.vue';
import RunCenter from '../src/views/runs/RunCenter.vue';
import SendingIdentityCenter from '../src/views/SendingIdentityCenter.vue';
import SourcingCaseDetail from '../src/views/sourcing/SourcingCaseDetail.vue';
import SourcingRecoveryForm from '../src/views/sourcing/SourcingRecoveryForm.vue';
const mounted:App[]=[];
afterEach(()=>{mounted.splice(0).forEach(a=>a.unmount());document.body.replaceChildren();});
const json=(data:unknown,status=200)=>new Response(JSON.stringify(data),{status,headers:{'content-type':'application/json'}});
async function flush(){for(let i=0;i<12;i++){await nextTick();await new Promise(r=>setTimeout(r,0));}}
function deferred(){let resolve!:(r:Response)=>void;const promise=new Promise<Response>(yes=>{resolve=yes;});return {promise,resolve};}
async function mount(component:Component,fetch:typeof globalThis.fetch,path='/',props:Record<string,unknown>={}){
 let generation=0,employee='boss-one';const listeners=new Set<()=>void>();
 const provider={generation:()=>generation,current:()=>({employeeId:employee,tenantId:'tenant',mode:'authenticated' as const}),subscribe:(fn:()=>void)=>{listeners.add(fn);return ()=>listeners.delete(fn);}};
 const router=createRouter({history:createMemoryHistory(),routes:[{path:'/sourcing/:caseId',component},{path:'/:pathMatch(.*)*',component}]});await router.push(path);
 const root=document.createElement('div');document.body.append(root);const app=createApp(component,props);app.use(router);app.provide('tradeos-api-client',createApiClient({baseUrl:'https://tradeos.test',fetch},provider));app.mount(root);mounted.push(app);await flush();return {root,router,changeIdentity:()=>{generation++;employee='boss-two';listeners.forEach(fn=>fn());}};
}
const button=(root:HTMLElement,text:string)=>[...root.querySelectorAll('button')].find(x=>x.textContent?.includes(text))!;
function field(root:HTMLElement,selector:string,value:string){const el=root.querySelector<HTMLInputElement>(selector)!;el.value=value;el.dispatchEvent(new Event(el.tagName==='SELECT'?'change':'input',{bubbles:true}));}
const campaign:components['schemas']['CampaignView']={campaign_id:'cmp-one',tenant_id:'tenant',name:'暂停的边界',state:'paused',version:1,created_at:'2026-09-06T00:00:00Z',created_by:'boss-one',approval_id:'apr-one',approved_at:'2026-09-06T00:00:00Z',approved_by:'boss-two',paused_reason:'核对中',today_messages_reserved:3,today_new_contacts_reserved:1,boundary:{allowed_categories:['hinges'],daily_new_contact_limit:5,daily_total_message_limit:10,handoff_triggers:[],markets:['controlled'],sender_identity_ids:['sid-one'],steps:[{intent:'discovery',step_number:1,wait_days:0}],stop_on_reply:true,target_entity_types:['distributor']}};
it('Campaign 读取拒绝不伪装成零联系人或可用发件选项',async()=>{
 const {root}=await mount(CampaignCenter,async input=>{const p=new URL((input as Request).url).pathname;return p==='/crm/campaigns'?json([campaign]):json({},503);});
 expect(root.textContent).toContain('入组进度读取失败');expect(root.textContent).not.toContain('暂无已验证联系人入组');
 button(root,'新建活动').click();await flush();expect(root.textContent).toContain('发件身份读取失败');expect(button(root,'创建草稿').disabled).toBe(true);
});
it('Campaign 列表403撤销仍在途的入组响应',async()=>{
 const old=deferred();let denied=false;
 const {root}=await mount(CampaignCenter,async input=>{const p=new URL((input as Request).url).pathname;if(p==='/crm/campaigns')return denied?json({},403):json([campaign]);if(p.endsWith('/enrollments'))return old.promise;return json([]);});
 denied=true;button(root,'刷新').click();await flush();old.resolve(json([{account_id:'private-old'}]));await flush();expect(root.textContent).not.toContain('暂停的边界');expect(root.textContent).not.toContain('private-old');
});
it('Run 失败读取与无数据分开，缺失深链不回退其他Run',async()=>{
 const calls:string[]=[];const {root}=await mount(RunCenter,async input=>{const p=new URL((input as Request).url).pathname;calls.push(p);return json({},p==='/runs'?503:404);},'/runs?run=missing');
 expect(root.textContent).not.toContain('当前没有可审计的运行记录');expect(root.textContent).not.toContain('选择一条运行记录');expect(root.textContent).toContain('不存在');expect(calls).toContain('/runs/missing');
});
it('Settings 503读取失败显示研究配置错误，不渲染未配置',async()=>{
 const {root}=await mount(SettingsCenter,async()=>json({},503));
 expect(root.textContent).toContain('研究配置读取失败');expect(root.textContent).not.toContain('尚未配置公司业务规则');
});
it('Settings 503冻结Playbook payload，同键恢复先读取历史',async()=>{
 const requests:Request[]=[];const bodies:unknown[]=[];const order:string[]=[];
 const {root}=await mount(SettingsCenter,async input=>{const r=input as Request,p=new URL(r.url).pathname;order.push(r.method+' '+p);if(r.method==='POST'){requests.push(r);bodies.push(await r.json());return json({},503);}if(p.endsWith('/versions'))return json([]);if(p.endsWith('/playbook'))return json({configured:false,active_version:null});if(p.endsWith('/country-policies'))return json({},503);return json({},503);});
 field(root,'[name="company_type"]','trading');field(root,'[name="minimum_deal_amount"]','1000');field(root,'[name="minimum_deal_currency"]','USD');
 root.querySelector('form')!.dispatchEvent(new Event('submit',{cancelable:true}));await flush();expect(root.textContent).toContain('结果待核对');expect(root.querySelector<HTMLInputElement>('[name="company_type"]')!.disabled).toBe(true);
 root.querySelector('form')!.dispatchEvent(new Event('submit',{cancelable:true}));await flush();expect(requests).toHaveLength(2);expect(requests[1]!.headers.get('Idempotency-Key')).toBe(requests[0]!.headers.get('Idempotency-Key'));expect(bodies[1]).toEqual(bodies[0]);const posts=order.map((x,i)=>x.startsWith('POST')?i:-1).filter(i=>i>=0);expect(order.slice(posts[0]!+1,posts[1])).toContain('GET /settings/playbook/versions');
});
const identity:components['schemas']['IdentityView']={identity_id:'sid-one',address:'one@example.test',domain:'example.test',role:'cold_outreach',state:'auth_pending',created_at:'2026-09-06T00:00:00Z',remaining_today:0,can_send_today:false,usable_for_cold_outreach:false,warmup_complete:false,auth:{checked_at:'2026-09-06T00:00:00Z',spf_passed:false,dkim_passed:true,dmarc_passed:true,failures:[]}};
it('未通过认证不可启动预热；入站未来重试期限不能提前解除',async()=>{
 const post=vi.fn();const {root}=await mount(SendingIdentityCenter,async input=>{const r=input as Request;if(r.method==='POST')post();return new URL(r.url).pathname.endsWith('/management')?json([identity]):json({state:'waiting',identity_id:'sid-one',reason:'rate_limited',version:7,next_retry_at:'2099-09-06T01:00:00Z'});});
 field(root,'[aria-label="预热目标日量"]','15');await flush();expect(button(root,'启动预热').disabled).toBe(true);expect(root.textContent).toContain('需先通过');expect(root.textContent).toContain('2099');expect(button(root,'原位重试').disabled).toBe(true);expect(post).not.toHaveBeenCalled();
});
it('入站原位重试只传安全version且409后读取canonical',async()=>{
 let changed=false;let payload:unknown;const {root}=await mount(SendingIdentityCenter,async input=>{const r=input as Request,p=new URL(r.url).pathname;if(r.method==='POST'){payload=await r.json();changed=true;return json({},409);}if(p.endsWith('/management'))return json([]);return json({state:'blocked',identity_id:'sid-one',reason:'provider_permanent',version:changed?8:7});});
 button(root,'原位重试').click();await flush();expect(payload).toEqual({expected_version:7});expect(root.textContent).toContain('状态已变化');expect(root.textContent).toContain('处理已阻断');
});
const execution:components['schemas']['SourcingUncertainExecutionReadView']={execution_id:'execution-one',run_id:'run-one',request_key:'request-one',status:'uncertain',recovery_action:'record_reconciliation',can_current_user_reconcile:true,created_at:'2026-09-06T00:00:00Z',reconciliation:null};
it('Sourcing 核对命令冻结reconciliation_id和原始内容，不把新选择当旧命令',async()=>{
 const commands:unknown[]=[];const {root}=await mount(SourcingRecoveryForm,async()=>json([]),'/',{disabled:false,executions:[execution],onReconcile:(c:unknown)=>commands.push(c)});
 const select=root.querySelector('select')!;select.value='execution-one';select.dispatchEvent(new Event('change'));field(root,'input','art-one');field(root,'textarea','确实已消耗');await flush();root.querySelector('form')!.dispatchEvent(new Event('submit',{cancelable:true}));await flush();root.querySelector('form')!.dispatchEvent(new Event('submit',{cancelable:true}));await flush();expect(commands).toHaveLength(2);expect(commands[1]).toEqual(commands[0]);expect(select.disabled).toBe(true);expect(root.querySelector('input')!.disabled).toBe(true);
});

const caseView:components['schemas']['SourcingCaseReadView']={case_id:'case-one',need_id:'need-one',state:'verifying',version:1,workflow_version:2,opened_at:'2026-09-06T00:00:00Z',state_changed_at:null,ladder_checked_to:5,active_search_plan_id:null,need_snapshot:null,stop:{code:'reconciliation_required',stage:'public_search'}};
it('Sourcing canonical已保存后的恢复消费后端action，保持原command与HTTP键',async()=>{
 const writes:Request[]=[];const bodies:components['schemas']['SourcingUncertainReconciliationCommand'][]=[];let reads=0;
 const {root}=await mount(SourcingCaseDetail,async input=>{const r=input as Request,p=new URL(r.url).pathname;if(r.method==='POST'){writes.push(r);bodies.push(await r.json());return json({},503);}if(p.endsWith('/uncertain-reconciliations')){reads++;const c=bodies[0];return json([{...execution,recovery_action:c?'resume_reconciliation':'record_reconciliation',can_current_user_reconcile:!c,reconciliation:c?{...c,execution_id:execution.execution_id,status:'confirmed_consumed',reconciled_by:'boss-one',reconciled_at:'2026-09-06T00:00:00Z'}:null}]);}if(p==='/sourcing-cases/case-one')return json(caseView);if(p.endsWith('/public-search-plan')||p.endsWith('/review')||p.endsWith('/current-quota'))return json(null);return json([]);},'/sourcing/case-one');
 field(root,'.recovery-panel select','execution-one');field(root,'.recovery-panel input','art-one');field(root,'.recovery-panel textarea','确实已消耗');await flush();button(root,'确认已消耗并请求恢复').click();await flush();expect(root.textContent).toContain('核对结果未知');button(root,'核对原请求并恢复').click();await flush();
 expect(writes).toHaveLength(2);expect(reads).toBeGreaterThanOrEqual(2);expect(bodies[1]).toEqual(bodies[0]);expect(writes[1]!.headers.get('Idempotency-Key')).toBe(writes[0]!.headers.get('Idempotency-Key'));
});

it('国家政策503保留原键与九项来源，历史读取成功后才原样恢复',async()=>{
 const writes:Request[]=[];const bodies:unknown[]=[];
 const {root}=await mount(SettingsCenter,async input=>{const r=input as Request,p=new URL(r.url).pathname;if(r.method==='POST'){writes.push(r);bodies.push(await r.json());return json({},503);}if(p.endsWith('/versions'))return json([]);if(p.endsWith('/country-policies'))return json({active_policies:[],coverage:{active_policy_count:0,contact_enrichment_allowed_count:0},contact_enrichment:{state:'blocked',reason_code:'COUNTRY_POLICY_NOT_CONFIGURED'}});if(p.endsWith('/playbook'))return json({configured:false,active_version:null});return json({},503);});
 const booleans=['public_research_allowed','contact_enrichment_allowed','cold_b2b_email_allowed','personal_data_basis_required','subject_type_affects_judgment','contact_type_affects_judgment','local_representative_required'];
 field(root,'[name="country"]','Synthetic Market');field(root,'[name="notes"]','已核验事实');for(const name of booleans)field(root,`[name="${name}"]`,'false');
 for(const name of [...booleans,'opt_out_deadline_days','requirements'])field(root,`[name="source_${name}"]`,'safe-source');
 await flush();button(root,'提交国家政策审批候选').click();await flush();expect(root.textContent).toContain('候选可能已持久保存');expect(root.querySelector<HTMLInputElement>('[name="country"]')!.disabled).toBe(true);
 button(root,'原内容').click();await flush();expect(writes).toHaveLength(2);expect(bodies[1]).toEqual(bodies[0]);expect(writes[1]!.headers.get('Idempotency-Key')).toBe(writes[0]!.headers.get('Idempotency-Key'));
});
it('Settings 身份变更后旧研究读取不回填当前工作区',async()=>{
 const old=deferred();let researchReads=0;
 const {root,changeIdentity}=await mount(SettingsCenter,async input=>{const p=new URL((input as Request).url).pathname;if(p==='/settings/research')return ++researchReads===1?old.promise:json({},403);return json({},503);});
 changeIdentity();await flush();old.resolve(json({state:'configured_unverified',confirmation_requires_recheck:true,remaining_lower_bound:1234567,checked_at:null}));await flush();expect(root.textContent).not.toContain('1234567');expect(root.textContent).toContain('无权');
});
it('Sourcing 切换case后旧成功和finally不覆盖新对象',async()=>{
 const old=deferred();const {root,router}=await mount(SourcingCaseDetail,async input=>{const p=new URL((input as Request).url).pathname;if(p==='/sourcing-cases/case-one')return old.promise;if(p==='/sourcing-cases/case-two')return json({...caseView,case_id:'case-two',need_id:'need-two'});if(p.endsWith('/public-search-plan')||p.endsWith('/review')||p.endsWith('/current-quota'))return json(null);return json([]);},'/sourcing/case-one');
 await router.push('/sourcing/case-two');await flush();old.resolve(json(caseView));await flush();expect(root.textContent).toContain('need-two');expect(root.textContent).not.toContain('need-one');expect(button(root,'刷新').disabled).toBe(false);
});
it('刷新后legacy canonical仅按后端resume续交付同事实与稳定header',async()=>{
 const fact={execution_id:execution.execution_id,reconciliation_id:'legacy-canonical',reason:'原人工核对',provider_usage_artifact_ref:'art-original',reconciled_by:'boss-one',reconciled_at:'2026-09-06T00:00:00Z',status:'confirmed_consumed' as const};const commands:unknown[]=[];const keys:string[]=[];
 const fetch:typeof globalThis.fetch=async input=>{const r=input as Request,p=new URL(r.url).pathname;if(r.method==='POST'){commands.push(await r.json());keys.push(r.headers.get('Idempotency-Key')!);return json({},503);}if(p==='/sourcing-cases/case-one')return json(caseView);if(p.endsWith('/uncertain-reconciliations'))return json([{...execution,recovery_action:'resume_reconciliation',can_current_user_reconcile:false,reconciliation:fact}]);if(p.endsWith('/public-search-plan')||p.endsWith('/review')||p.endsWith('/current-quota'))return json(null);return json([]);};
 const first=await mount(SourcingCaseDetail,fetch,'/sourcing/case-one');button(first.root,'恢复已记录核对').click();await flush();mounted.pop()!.unmount();first.root.remove();const second=await mount(SourcingCaseDetail,fetch,'/sourcing/case-one');button(second.root,'恢复已记录核对').click();await flush();expect(commands).toHaveLength(2);expect(commands[1]).toEqual({reconciliation_id:'legacy-canonical',reason:'原人工核对',provider_usage_artifact_ref:'art-original',run_id:'run-one',request_key:'request-one',resolution:'count_as_consumed'});expect(keys).toEqual(['sourcing-reconcile-legacy-canonical','sourcing-reconcile-legacy-canonical']);
});
it('Campaign 暂停响应未知后刷新不把旧active当成功，必须核对精确目标状态',async()=>{
 vi.stubGlobal('prompt',vi.fn(()=> '核对'));let paused=false;let posts=0;
 try {
  const {root}=await mount(CampaignCenter,async input=>{const r=input as Request,p=new URL(r.url).pathname;if(r.method==='POST'){posts++;return json({},503);}return p==='/crm/campaigns'?json([{...campaign,state:paused?'paused':'active'}]):json([]);});
  button(root.querySelector('.detail-actions')!,'暂停').click();await flush();expect(root.textContent).toContain('结果待核对');button(root,'刷新').click();await flush();expect(button(root.querySelector('.detail-actions')!,'暂停').disabled).toBe(true);expect(posts).toBe(1);
  paused=true;button(root,'刷新').click();await flush();expect(button(root,'激活已批准版本').disabled).toBe(false);expect(root.textContent).toContain('3 / 10');expect(root.textContent).toContain('在途');
 } finally {vi.unstubAllGlobals();}
});
it('Settings并行读取中的403立即撤销页面，不等待另一读取完成',async()=>{
 const pending=deferred();const {root}=await mount(SettingsCenter,async input=>{const p=new URL((input as Request).url).pathname;if(p==='/settings/playbook')return json({},403);if(p==='/settings/playbook/versions')return pending.promise;return json({},503);});
 expect(root.textContent).toContain('只有老板可以查看或提交公司业务规则');expect(root.textContent).not.toContain('尚未配置公司业务规则');pending.resolve(json([]));await flush();expect(root.textContent).toContain('只有老板');
});

it.each([200,503])('入站A旧retry %s晚到不覆盖B绑定及B新retry busy/unknown',async staleStatus=>{
 const old=deferred(), latest=deferred();const payloads:unknown[]=[];
 const a={state:'blocked',identity_id:'sid-one',reason:'provider_permanent',version:7};
 const b={...a,identity_id:'sid-two',version:12};
 const {root}=await mount(SendingIdentityCenter,async input=>{
  const r=input as Request,p=new URL(r.url).pathname;
  if(p.endsWith('/management'))return json([identity,{...identity,identity_id:'sid-two',address:'two@example.test'}]);
  if(p.endsWith('/binding'))return json(b);
  if(r.method==='POST'){payloads.push(await r.json());return payloads.length===1?old.promise:latest.promise;}
  return json(a);
 });
 button(root,'原位重试').click();await flush();
 const bind=[...root.querySelectorAll<HTMLButtonElement>('button')].filter(x=>x.textContent?.includes('绑定本机入站邮箱')).at(-1)!;
 bind.click();await flush();button(root,'确认绑定').click();await flush();
 expect(root.textContent).toContain('当前绑定：sid-two');expect(button(root,'原位重试').disabled).toBe(false);
 button(root,'原位重试').click();await flush();expect(payloads).toEqual([{expected_version:7},{expected_version:12}]);
 old.resolve(json({...a,version:8},staleStatus));await flush();
 expect(root.textContent).toContain('当前绑定：sid-two');expect(button(root,'原位重试').disabled).toBe(true);expect(button(root,'刷新入站状态').disabled).toBe(true);
 latest.resolve(json({},503));await flush();expect(root.textContent).toContain('重试结果待核对');expect(button(root,'原位重试').disabled).toBe(true);expect(button(root,'刷新入站状态').disabled).toBe(false);
});
it.each(['busy','unknown'])('入站旧status晚到不覆盖新绑定version及%s边界',async phase=>{
 const old=deferred(), latest=deferred();let reads=0;const payloads:unknown[]=[];
 const a={state:'blocked',identity_id:'sid-one',reason:'provider_permanent',version:7};const b={...a,identity_id:'sid-two',version:12};
 const {root}=await mount(SendingIdentityCenter,async input=>{
  const r=input as Request,p=new URL(r.url).pathname;
  if(p.endsWith('/management'))return json([identity,{...identity,identity_id:'sid-two',address:'two@example.test'}]);
  if(p.endsWith('/binding'))return json(b);
  if(r.method==='POST'){payloads.push(await r.json());return latest.promise;}
  return ++reads===1?json(a):old.promise;
 });
 button(root,'刷新入站状态').click();await flush();
 [...root.querySelectorAll<HTMLButtonElement>('button')].filter(x=>x.textContent?.includes('绑定本机入站邮箱')).at(-1)!.click();await flush();button(root,'确认绑定').click();await flush();
 button(root,'原位重试').click();await flush();expect(payloads).toEqual([{expected_version:12}]);
 if(phase==='unknown'){latest.resolve(json({},503));await flush();}
 old.resolve(json(a));await flush();expect(root.textContent).toContain('当前绑定：sid-two');expect(button(root,'原位重试').disabled).toBe(true);
 expect(button(root,'刷新入站状态').disabled).toBe(phase==='busy');
 if(phase==='unknown')expect(root.textContent).toContain('重试结果待核对');else {latest.resolve(json({...b,version:13}));await flush();expect(button(root,'原位重试').disabled).toBe(false);}
});

it.each([false,true])('Sourcing安全422首次可修正，但此前unknown=%s保留原命令',async wasUnknown=>{
 const bodies:components['schemas']['SourcingUncertainReconciliationCommand'][]=[];const keys:string[]=[];
 const {root}=await mount(SourcingCaseDetail,async input=>{
  const r=input as Request,p=new URL(r.url).pathname;
  if(r.method==='POST'){bodies.push(await r.json());keys.push(r.headers.get('Idempotency-Key')!);return wasUnknown&&bodies.length===1?json({},503):bodies.length===(wasUnknown?2:1)?json({code:'http_error',message:'请求未完成'},422):json(null);}
  if(p==='/sourcing-cases/case-one')return json(caseView);
  if(p.endsWith('/uncertain-reconciliations'))return json([execution]);
  if(p.endsWith('/public-search-plan')||p.endsWith('/review')||p.endsWith('/current-quota'))return json(null);return json([]);
 },'/sourcing/case-one');
 field(root,'.recovery-panel select','execution-one');field(root,'.recovery-panel input','art-invalid');field(root,'.recovery-panel textarea','确实消耗');await flush();button(root,'确认已消耗并请求恢复').click();await flush();
 if(wasUnknown){button(root,'核对原请求并恢复').click();await flush();expect(root.querySelector<HTMLInputElement>('.recovery-panel input')!.disabled).toBe(true);expect(bodies[1]).toEqual(bodies[0]);expect(keys[1]).toBe(keys[0]);}
 else {expect(root.querySelector<HTMLInputElement>('.recovery-panel input')!.disabled).toBe(false);field(root,'.recovery-panel input','art-fixed');await flush();button(root,'确认已消耗并请求恢复').click();await flush();expect(bodies[1]!.provider_usage_artifact_ref).toBe('art-fixed');expect(bodies[1]!.reconciliation_id).not.toBe(bodies[0]!.reconciliation_id);expect(root.textContent).toContain('核对请求已被接受');}
});
it('Sourcing业务400及已存在canonical的422均不能按首次字段失败解锁',async()=>{
 for(const canonical of [false,true]){
  const fact={execution_id:execution.execution_id,reconciliation_id:'recorded-one',reason:'原理由',provider_usage_artifact_ref:'art-one',reconciled_by:'boss-one',reconciled_at:'2026-09-06T00:00:00Z',status:'confirmed_consumed' as const};
  const {root}=await mount(SourcingCaseDetail,async input=>{const r=input as Request,p=new URL(r.url).pathname;if(r.method==='POST')return canonical?json({code:'http_error',message:'请求未完成'},422):json({code:'validation_error',message:'请求参数无效'},400);if(p==='/sourcing-cases/case-one')return json(caseView);if(p.endsWith('/uncertain-reconciliations'))return json([{...execution,recovery_action:canonical?'resume_reconciliation':'record_reconciliation',can_current_user_reconcile:!canonical,reconciliation:canonical?fact:null}]);if(p.endsWith('/public-search-plan')||p.endsWith('/review')||p.endsWith('/current-quota'))return json(null);return json([]);},'/sourcing/case-one');
  if(canonical)button(root,'恢复已记录核对').click();else {field(root,'.recovery-panel select','execution-one');field(root,'.recovery-panel input','art-one');field(root,'.recovery-panel textarea','原理由');await flush();button(root,'确认已消耗并请求恢复').click();}
  await flush();expect(root.querySelector<HTMLInputElement>('.recovery-panel input')!.disabled).toBe(true);expect(button(root,'核对原请求并恢复')).toBeDefined();mounted.pop()!.unmount();root.remove();
 }
});
it('入站409核对读取在途不重新开放旧version，成功后仅用当前version',async()=>{
 const pending=deferred();let reads=0;const payloads:unknown[]=[];
 const {root}=await mount(SendingIdentityCenter,async input=>{const r=input as Request,p=new URL(r.url).pathname;if(p.endsWith('/management'))return json([]);if(r.method==='POST'){payloads.push(await r.json());return json({},409);}return ++reads===1?json({state:'blocked',identity_id:'sid-one',reason:'provider_permanent',version:7}):reads===2?pending.promise:json({state:'blocked',identity_id:'sid-one',reason:'provider_permanent',version:8});});
 button(root,'原位重试').click();await flush();expect(button(root,'原位重试').disabled).toBe(true);pending.resolve(json({state:'blocked',identity_id:'sid-one',reason:'provider_permanent',version:8}));await flush();expect(button(root,'原位重试').disabled).toBe(false);button(root,'原位重试').click();await flush();expect(payloads).toEqual([{expected_version:7},{expected_version:8}]);
});
