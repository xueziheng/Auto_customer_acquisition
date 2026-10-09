<script setup lang="ts">
import { computed, inject, onMounted, ref } from "vue";
import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";
import { takeGmailCallback } from "../../gmail-callback";
import { useQuoteRequestScope } from "../costing-quotes/quote-request-scope";

type Status = components["schemas"]["GmailConnectionStatus"];
type Result = components["schemas"]["GmailTestResult"];
type Template = components["schemas"]["GmailTestTemplate"];
const emit = defineEmits<{ connected: [] }>();
const client = inject<ReturnType<typeof createApiClient>>("tradeos-api-client", apiClient);
const navigate = inject<typeof globalThis.location.assign>("tradeos-gmail-navigate", url => globalThis.location.assign(url));
const status = ref<Status | null>(null);
const email = ref("");
const recipient = ref("");
const error = ref("");
const notice = ref("");
const busy = ref(false);
const reviewing = ref(false);
const confirmed = ref(false);
const template = ref<Template | null>(null);
const result = ref<Result | null>(null);
const requestId = ref("");
const reviewedRecipient = ref("");
const unresolved = computed(() => result.value && !["succeeded", "duplicate", "rejected"].includes(result.value.status));
function clear() {
  status.value = null; email.value = ""; recipient.value = ""; error.value = "";
  notice.value = ""; busy.value = false; reviewing.value = false; confirmed.value = false;
  template.value = null; result.value = null; requestId.value = ""; reviewedRecipient.value = "";
}
const gate = useQuoteRequestScope(client, () => [], clear);
async function loadResult() {
  if (!status.value?.email) return;
  const op = gate.begin("test-result"); if (!op) return;
  try {
    const response = await client.GET("/inbox/gmail/test", { signal: op.signal });
    if (op.valid() && response.response.ok) result.value = response.data ?? null;
  } catch { if (op.valid()) error.value = "测试结果暂不可读取，请刷新核对。"; }
}
async function loadStatus() {
  if (!gate.hasIdentity.value) return;
  const op = gate.begin("connection"); if (!op) return;
  try {
    const response = await client.GET("/inbox/gmail/status", { signal: op.signal });
    if (!op.valid()) return;
    if (!response.response.ok || !response.data) { error.value = "邮箱连接服务暂不可用"; return; }
    status.value = response.data;
    if (response.data.email) email.value = response.data.email;
    await loadResult();
  } catch { if (op.valid()) error.value = "邮箱连接服务暂不可用"; }
}
async function start() {
  if (busy.value || !email.value.trim()) return;
  const op = gate.begin("authorize"); if (!op) return;
  busy.value = true; error.value = "";
  try {
    const response = await client.POST("/inbox/gmail/start", { body: { email: email.value.trim() }, signal: op.signal });
    if (!op.valid()) return;
    if (!response.response.ok || !response.data) throw new Error("authorization");
    const url = new globalThis.URL(response.data.authorization_url);
    if (url.protocol !== "https:" || url.hostname !== "accounts.google.com" || url.port
        || url.username || url.password || !["/o/oauth2/auth", "/o/oauth2/v2/auth"].includes(url.pathname)) throw new Error("destination");
    navigate(url.href);
  } catch { if (op.valid()) error.value = "暂时无法开始 Google 授权，请检查邮箱地址或联系管理员。"; }
  finally { if (op.valid()) busy.value = false; }
}
async function complete(code: string, state: string) {
  const op = gate.begin("authorize"); if (!op) return;
  busy.value = true; error.value = "";
  try {
    const response = await client.POST("/inbox/gmail/complete", { body: { code, state }, signal: op.signal });
    if (!op.valid()) return;
    if (!response.response.ok || !response.data) throw new Error("authorization");
    status.value = response.data; email.value = response.data.email ?? "";
    notice.value = "Gmail 已连接，后台开始同步本人邮件。";
    emit("connected"); await loadResult();
  } catch { if (op.valid()) error.value = "授权未完成或已过期。请用同一 TradeOS 账号重新连接，选择对应 Google 账号并授予读取和发送权限。"; }
  finally { if (op.valid()) busy.value = false; }
}
async function review(previous = false) {
  if (busy.value) return;
  const address = previous ? result.value?.recipient : recipient.value.trim();
  if (!address) return;
  const op = gate.begin("preview"); if (!op) return;
  busy.value = true; error.value = ""; confirmed.value = false;
  try {
    const response = await client.GET("/inbox/gmail/template", { signal: op.signal });
    if (!op.valid()) return;
    if (!response.response.ok || !response.data) throw new Error("template");
    template.value = response.data;
    reviewedRecipient.value = address;
    requestId.value = previous && result.value ? result.value.request_id : globalThis.crypto.randomUUID();
    reviewing.value = true;
  } catch { if (op.valid()) error.value = "测试邮件预览暂不可用，尚未发送。"; }
  finally { if (op.valid()) busy.value = false; }
}
async function send() {
  if (busy.value || !confirmed.value || !reviewing.value || !requestId.value) return;
  const op = gate.begin("send"); if (!op) return;
  busy.value = true; error.value = "";
  try {
    const response = await client.POST("/inbox/gmail/test", { body: {
      email: reviewedRecipient.value, request_id: requestId.value, confirm_my_mailbox: true,
    }, signal: op.signal });
    if (!op.valid()) return;
    if (!response.response.ok || !response.data) {
      error.value = "测试未完成，请先核对发送记录；发件身份可能缺失、受限或当前账号无权限。";
      await loadResult(); return;
    }
    result.value = response.data; reviewing.value = false; confirmed.value = false; emit("connected");
  } catch {
    if (op.valid()) { error.value = "发送结果未知，请核对原记录，不要重新创建测试邮件。"; await loadResult(); }
  } finally { if (op.valid()) busy.value = false; }
}
onMounted(async () => {
  const callback = takeGmailCallback();
  await loadStatus();
  if (callback?.failed) error.value = "Google 授权未完成，请重新连接 Gmail。";
  else if (callback && gate.hasIdentity.value) await complete(callback.code, callback.state);
});
</script>

<template>
  <section
    class="gmail-connection"
    aria-label="Gmail 连接"
  >
    <p
      v-if="error"
      role="alert"
    >
      {{ error }}
    </p>
    <p
      v-if="notice"
      role="status"
    >
      {{ notice }}
    </p>
    <template v-if="status?.configured === true">
      <h2>{{ status.email ? "Gmail 已连接" : "连接你的 Gmail" }}</h2>
      <p>授权后同步整个邮箱的历史与新邮件，仅本人可见。测试发送需要单独确认。</p>
      <div class="connection-controls">
        <label>Gmail 地址 <input
          v-model="email"
          aria-label="Gmail 地址"
          type="email"
          maxlength="254"
          :readonly="!!status.email"
          :disabled="busy"
          placeholder="name@gmail.com"
        ></label>
        <button
          :disabled="busy || !email.trim()"
          @click="start"
        >
          {{ status.email ? "重新授权 Gmail" : "连接 Gmail" }}
        </button>
      </div>
      <p v-if="status.email">
        已绑定：{{ status.email }}
      </p>
      <section
        v-if="status.can_test"
        class="gmail-test"
        aria-label="邮件收发测试"
      >
        <h3>邮件收发测试</h3>
        <p>向你自己的另一个邮箱发送一封固定测试邮件，再回复它来核验收件。每天最多 5 次。</p>
        <div class="connection-controls">
          <label>测试收件邮箱 <input
            v-model="recipient"
            aria-label="测试收件邮箱"
            type="email"
            maxlength="254"
            :disabled="busy || !!unresolved"
            placeholder="用于测试的 QQ 或其他邮箱"
          ></label>
          <button
            :disabled="busy || !recipient.trim() || !!unresolved"
            @click="review(false)"
          >
            预览测试邮件
          </button>
        </div>
        <section
          v-if="reviewing && template"
          class="test-preview"
          role="dialog"
          aria-label="测试邮件确认"
        >
          <p>发件人：{{ status.email }}<br>收件人：{{ reviewedRecipient }}</p>
          <strong>{{ template.subject }}</strong>
          <pre>{{ template.body }}</pre>
          <label><input
            v-model="confirmed"
            type="checkbox"
            :disabled="busy"
          >这是本人用于收发测试的邮箱，我确认发送上述邮件</label>
          <div class="connection-controls">
            <button
              :disabled="busy || !confirmed"
              @click="send"
            >
              确认发送一封测试邮件
            </button>
            <button
              :disabled="busy"
              @click="reviewing = false"
            >
              取消
            </button>
          </div>
        </section>
        <section
          v-if="result"
          class="test-result"
          aria-label="最近测试结果"
        >
          <p>最近测试：{{ result.sender }} → {{ result.recipient }}</p>
          <p
            v-if="['succeeded', 'duplicate'].includes(result.status) && result.provider_ref"
            role="status"
          >
            Gmail 已接受此邮件。是否进入收件箱仍需要收件人确认；请回复它核验收件同步。
          </p>
          <p
            v-else
            role="status"
          >
            结果尚未确认：{{ result.status }}。保留原发送记录，勿换收件人重试。
          </p>
          <small>发送记录：{{ result.tool_call_id ?? "尚未返回" }}</small>
          <div class="connection-controls">
            <button
              :disabled="busy"
              @click="loadResult"
            >
              刷新测试结果
            </button>
            <button
              v-if="unresolved"
              :disabled="busy"
              @click="review(true)"
            >
              预览并核对原测试
            </button>
          </div>
        </section>
      </section>
    </template>
    <p v-else-if="status?.configured === false">
      管理员尚未启用网页 Gmail 连接。
    </p>
  </section>
</template>

<style scoped>
.gmail-connection { border: 1px solid #dce5ec; border-radius: 10px; padding: 18px; margin: 16px 0; background: #f8fbfe; }
.gmail-connection:empty { display: none; }
h2 { margin: 0 0 10px; font-size: 19px; } h3 { margin-bottom: 8px; }
p { line-height: 1.6; } [role="alert"] { color: #9d2727; }
.connection-controls { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin: 12px 0; }
input,button { font: inherit; padding: 9px 12px; border: 1px solid #c9d6e0; border-radius: 6px; background: white; color: inherit; max-width: 100%; }
button { cursor: pointer; } button:disabled { cursor: default; opacity: .5; }
.gmail-test { border-top: 1px solid #dce5ec; margin-top: 18px; padding-top: 6px; }
.test-preview,.test-result { margin-top: 14px; padding: 16px; background: white; border: 1px solid #c9d6e0; border-radius: 8px; overflow-wrap: anywhere; }
pre { white-space: pre-wrap; font: inherit; line-height: 1.6; }
</style>
