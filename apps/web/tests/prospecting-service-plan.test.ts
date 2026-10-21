import { createApp, nextTick } from 'vue';
import { expect, it } from 'vitest';
import { createApiClient } from '../src/api/client';
import ProspectingServicePlanPanel from '../src/components/ProspectingServicePlanPanel.vue';

const query = { product_tags: ['hinges'], country: 'US', entity_types: ['distributor'], confirmed_refs: [{ document_id: 'doc', revision_id: 'rev' }] };
const status = { catalog: [{ tier: 'basic', period_days: 30, candidate_limit: 100, search_limit: 200, contact_limit: 20, cadence_hours: 168, max_pages_per_company: 2, max_contacts_per_company: 1, per_run_search_limit: 10, per_run_page_limit: 10, per_run_signal_limit: 10, per_run_hypothesis_limit: 10 }], plan: { tier: 'basic', version: 7, schedule_enabled: false, target_kind: 'inventory_request', query, candidates_reserved: 10, searches_reserved: 10, contacts_reserved: 1 } };
async function flush() { for (let index = 0; index < 5; index++) { await nextTick(); await new Promise(resolve => setTimeout(resolve, 0)); } }

it('只读展示权益，显式保存携带当前确认目标和版本；未知结果不能重发', async () => {
  const writes: unknown[] = [];
  const provider = { generation: () => 0, current: () => ({ employeeId: 'emp', tenantId: 'tn', mode: 'authenticated' as const }), subscribe: () => () => {} };
  const client = createApiClient({ baseUrl: 'https://test.local', fetch: async input => {
    const request = input as Request;
    if (request.method === 'PUT') { writes.push(await request.json()); return new Response('{}', { status: 503, headers: { 'content-type': 'application/json' } }); }
    return new Response(JSON.stringify(status), { headers: { 'content-type': 'application/json' } });
  } }, provider);
  const host = document.createElement('div'), app = createApp(ProspectingServicePlanPanel);
  app.provide('tradeos-api-client', client); document.body.append(host);
  try {
    app.mount(host); await flush();
    expect(writes).toHaveLength(0); expect(host.textContent).toContain('后台运维分配');
    expect(host.querySelector('select')).toBeNull();
    host.querySelector<HTMLInputElement>('input[type=checkbox]')!.click();
    host.querySelector<HTMLButtonElement>('button[type=submit]')!.click(); await flush();
    expect(writes).toEqual([{ query, enabled: true, expected_version: 7 }]);
    expect(host.textContent).toContain('排程提交结果待核实');
    expect(host.querySelector<HTMLButtonElement>('button[type=submit]')!.disabled).toBe(true);
    host.querySelector<HTMLButtonElement>('button[type=submit]')!.click(); await flush();
    expect(writes).toHaveLength(1);
  } finally { app.unmount(); host.remove(); }
});

it('身份变化立即清空前一企业的计划和目标，旧响应不能恢复它', async () => {
  let generation = 0, changed = () => {};
  const provider = { generation: () => generation, current: () => ({ employeeId: 'emp', tenantId: generation ? 'other' : 'tn', mode: 'authenticated' as const }), subscribe: (listener: () => void) => { changed = listener; return () => {}; } };
  const client = createApiClient({ baseUrl: 'https://test.local', fetch: async () => new Response(JSON.stringify(status), { headers: { 'content-type': 'application/json' } }) }, provider);
  const host = document.createElement('div'), app = createApp(ProspectingServicePlanPanel);
  app.provide('tradeos-api-client', client); document.body.append(host);
  try {
    app.mount(host); await flush(); expect(host.textContent).toContain('hinges');
    generation++; changed(); await flush();
    expect(host.textContent).not.toContain('hinges'); expect(host.querySelector('form')).toBeNull();
  } finally { app.unmount(); host.remove(); }
});
