import { createApp, nextTick } from 'vue';
import { expect, it } from 'vitest';
import AgentTurnCard from '../src/views/command-center/AgentTurnCard.vue';

it('未知结果提示新调用成本，撤权后移除全文', async () => {
  const host = document.createElement('div');
  const app = createApp(AgentTurnCard, { turn: {
    turn_id: 'turn_test', session_id: 'session_test', run_id: 'run_test',
    state: 'unknown', created_at: '2026-09-22T00:00:00Z', result: null,
    input_text: '测试问题',
  }});
  app.mount(host); await nextTick();
  expect(host.textContent).toContain('结果未知');
  expect(host.textContent).toContain('重新生成将再次消耗额度');
  expect(host.textContent).not.toContain('执行成功');
  app.unmount();
  const hidden = createApp(AgentTurnCard, { turn: {
    turn_id: 'turn_test', session_id: 'session_test', run_id: 'run_test',
    state: 'completed', created_at: '2026-09-22T00:00:00Z',
    content_hidden: true, input_text: '不能泄露', result: null,
  }});
  hidden.mount(host); await nextTick();
  expect(host.textContent).not.toContain('不能泄露');
  expect(host.textContent).toContain('当前权限');
  hidden.unmount();
});

import { createApiClient } from '../src/api/client';
import { useAgentSession } from '../src/composables/useAgentSession';

it('丢失202回执后保留原幂等键；409重新读取已存在轮次',async()=>{
  const bodies:unknown[]=[];
  let posts=0, reads=0;
  const provider={generation:()=>0,current:()=>({employeeId:'emp',tenantId:'tn',mode:'authenticated' as const}),subscribe:()=>()=>{}};
  const client=createApiClient({baseUrl:'https://test.local',fetch:async(input)=>{
    const req=input as Request;
    if(req.method==='POST'){
      bodies.push(await req.json());posts++;
      if(posts===1)throw new TypeError('network');
      return new Response('{}',{status:409,headers:{'content-type':'application/json'}});
    }
    reads++;return new Response('[]',{headers:{'content-type':'application/json'}});
  }},provider);
  let state!:ReturnType<typeof useAgentSession>;
  const app=createApp({setup(){state=useAgentSession(client);return()=>null;}});app.mount(document.createElement('div'));
  await state.selectSession('session');
  await state.send('原始文本');expect(state.uncertain.value).toBe(true);
  await state.send('另一段文本');expect(bodies[0]).toEqual(bodies[1]);
  expect(reads).toBeGreaterThan(1);expect(state.error.value).toContain('状态已变化');app.unmount();
});

it('身份改变时清空历史并拒绝旧身份迟到响应',async()=>{
  let generation=0, change=()=>{};
  let deliver!:(value:Response)=>void;
  const provider={generation:()=>generation,current:()=>({employeeId:`emp${generation}`,tenantId:'tn',mode:'authenticated' as const}),subscribe:(fn:()=>void)=>{change=fn;return()=>{};}};
  const client=createApiClient({baseUrl:'https://test.local',fetch:async(input)=>{
    if((input as Request).url.endsWith('/turns'))return new Promise<Response>(resolve=>{deliver=resolve;});
    return new Response('[]',{headers:{'content-type':'application/json'}});
  }},provider);
  let state!:ReturnType<typeof useAgentSession>;
  const app=createApp({setup(){state=useAgentSession(client);return()=>null;}});app.mount(document.createElement('div'));
  const pending=state.selectSession('session');
  await new Promise(resolve=>setTimeout(resolve,0));generation++;change();
  deliver(new Response(JSON.stringify([{input_text:'旧身份秘密'}]),{headers:{'content-type':'application/json'}}));await pending;
  expect(state.turns.value).toEqual([]);expect(state.sessionId.value).toBeNull();app.unmount();
});
