import { computed, onBeforeUnmount, ref } from 'vue';
import type { components } from '../api/api';
import { createApiClient } from '../api/client';
import { sameIdentitySnapshot } from '../views/costing-quotes/quote-request-scope';
type Turn = components['schemas']['TurnView'];
type Session = components['schemas']['SessionView'];

export function useAgentSession(client: ReturnType<typeof createApiClient>) {
  const sessions = ref<Session[]>([]), turns = ref<Turn[]>([]);
  const sessionId = ref<string | null>(null), error = ref(''), loading = ref(false);
  const available = ref(false), uncertain = ref(false), historyError = ref('');
  let pending: { text: string; idempotency_key: string } | null = null;
  let generation = 0, disposed = false;
  const requests = new Map<string, AbortController>();
  let timer: ReturnType<typeof setTimeout> | undefined;
  const active = computed(() => turns.value.some(t => ['queued', 'running'].includes(t.state)));
  function invalidate() {
    generation++; requests.forEach(c => c.abort()); requests.clear();
    if (timer) clearTimeout(timer);
  }
  function clear() {
    invalidate(); sessions.value = []; turns.value = []; sessionId.value = null;
    pending = null; uncertain.value = false; loading.value = false; error.value = ''; historyError.value = ''; available.value = false;
  }
  const unsubscribe = client.subscribeIdentity(() => { clear(); void refreshSessions(); });
  function begin(channel: string) {
    const snapshot = client.identitySnapshot(), version = generation;
    if (disposed || !snapshot.identity) return null;
    requests.get(channel)?.abort();
    const controller = new AbortController(); requests.set(channel, controller);
    return { signal: controller.signal, valid: () => !disposed && generation === version
      && requests.get(channel) === controller && sameIdentitySnapshot(snapshot, client.identitySnapshot()) };
  }
  function message(status: number) {
    return ({403:'当前身份无权访问',404:'会话不存在或当前不可见',409:'会话状态已变化，已重新读取当前轮次',429:'模型额度不足，请联系管理员',503:'模型尚未启用或后台不可用，请到系统设置查看连接状态'} as Record<number,string>)[status] ?? '请求未完成，请刷新核对';
  }
  function schedule() {
    if (timer) clearTimeout(timer);
    if (!disposed && sessionId.value) timer = setTimeout(() => void refreshTurns(), globalThis.document.hidden ? 15000 : 2500);
  }
  async function refreshSessions() {
    const op = begin('sessions'); if (!op) return;
    try {
      const capabilities = await client.GET('/health/capabilities', {signal:op.signal});
      if (!op.valid()) return;
      if (!capabilities.data?.some(c => c.name === 'builtin_assistant' && c.status === 'enabled')) { available.value = false; sessions.value = []; error.value = '当前服务未启用内置助手'; return; }
      const r = await client.GET('/agent/sessions', {signal:op.signal}); if (!op.valid()) return;
      if (!r.data) { error.value = message(r.response.status); sessions.value = []; return; }
      sessions.value = r.data; available.value = true;
      if (!sessionId.value && r.data[0]) await selectSession(r.data[0].session_id);
    } catch { if (op.valid()) { sessions.value = []; error.value = '会话列表读取失败'; } }
  }
  async function selectSession(id: string) {
    invalidate(); sessionId.value = id; turns.value = []; pending = null; uncertain.value = false; loading.value = false; error.value = ''; historyError.value = '';
    await refreshTurns();
  }
  async function refreshTurns() {
    const id = sessionId.value, op = begin('turns'); if (!id || !op) return;
    try {
      const r = await client.GET('/agent/sessions/{session_id}/turns', {params:{path:{session_id:id}},signal:op.signal});
      if (!op.valid()) return;
      turns.value = r.data ?? [];
      historyError.value = r.data ? '' : message(r.response.status);
    } catch { if (op.valid()) { turns.value = []; historyError.value = '历史读取失败，已隐藏旧内容'; } }
    finally { if (op.valid()) schedule(); }
  }
  async function startSession() {
    if (loading.value) return;
    const op = begin('create'); if (!op) return;
    loading.value = true; error.value = '';
    try {
      const r = await client.POST('/agent/sessions', {body:{},signal:op.signal}); if (!op.valid()) return;
      if (r.data) { sessions.value = [r.data,...sessions.value]; await selectSession(r.data.session_id); }
      else error.value = message(r.response.status);
    } catch { if (op.valid()) error.value = '新会话回执未收到，请刷新会话列表核对'; }
    finally { if (op.valid()) loading.value = false; }
  }
  async function send(text: string) {
    const id = sessionId.value; if (!id || loading.value || (active.value && !pending)) return false;
    const op = begin('send'); if (!op) return false;
    if (!pending) pending = {text, idempotency_key:globalThis.crypto.randomUUID()};
    loading.value = true; error.value = '';
    try {
      const r = await client.POST('/agent/sessions/{session_id}/turns', {params:{path:{session_id:id}},body:{...pending,object_refs:[]},signal:op.signal});
      if (!op.valid()) return false;
      if (r.data) { pending = null; uncertain.value = false; await refreshTurns(); return true; }
      uncertain.value = r.response.status >= 500;
      if (!uncertain.value) pending = null;
      error.value = message(r.response.status);
      if (r.response.status === 409) await refreshTurns();
    } catch { if (op.valid()) { uncertain.value = true; error.value = '未收到接纳回执。重试将核对原请求，不会自动创建新的调用。'; } }
    finally { if (op.valid()) loading.value = false; }
    return false;
  }
  let retry: { turn: string; key: string } | null = null;
  async function act(turn: Turn, action: 'cancel' | 'regenerate') {
    if (loading.value || turn.session_id !== sessionId.value) return;
    const op = begin('action'); if (!op) return;
    loading.value = true;
    try {
      const path = {session_id:turn.session_id,turn_id:turn.turn_id};
      if (!retry || retry.turn !== turn.turn_id) retry = {turn:turn.turn_id,key:globalThis.crypto.randomUUID()};
      const r = action === 'cancel'
        ? await client.POST('/agent/sessions/{session_id}/turns/{turn_id}/cancel', {params:{path},body:{},signal:op.signal})
        : await client.POST('/agent/sessions/{session_id}/turns/{turn_id}/regenerate', {params:{path},body:{idempotency_key:retry.key},signal:op.signal});
      if (!op.valid()) return;
      if (r.data || r.response.status < 500) retry = null;
      error.value = r.data ? '' : message(r.response.status); await refreshTurns();
    } catch { if (op.valid()) error.value = '操作回执未知，请刷新核对；再次点击将使用原请求'; }
    finally { if (op.valid()) loading.value = false; }
  }
  function dispose() { disposed = true; clear(); unsubscribe(); retry = null; }
  onBeforeUnmount(dispose);
  return { sessions, turns, sessionId, loading, error:computed(()=>error.value||historyError.value), available, uncertain, active,
    startSession, selectSession, refreshSessions, refreshTurns, send,
    cancel:(t:Turn)=>act(t,'cancel'), regenerate:(t:Turn)=>act(t,'regenerate'), dispose };
}
