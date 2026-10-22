<script setup lang="ts">
import { inject, onMounted, ref } from "vue";
import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";
import { useQuoteRequestScope } from "../costing-quotes/quote-request-scope";
const client = inject<ReturnType<typeof createApiClient>>("tradeos-api-client", apiClient);
const status = ref<components["schemas"]["AliyunConnectionStatus"] | null>(null);
const address = ref(""), password = ref(""), confirmed = ref(false), busy = ref(false), error = ref("");
const denied = ref(false);
function clear() {
  status.value = null; address.value = ""; password.value = ""; confirmed.value = false; busy.value = false; error.value = "";
}
const gate = useQuoteRequestScope(client, () => [], () => {
  clear(); denied.value = false;
});
function revoke() {
  gate.invalidate(); clear(); denied.value = true; error.value = "当前账号无权使用此邮箱，请重新登录。";
}
async function load() {
  if (!gate.hasIdentity.value || denied.value || busy.value) return;
  const op = gate.begin("status"); if (!op?.valid()) return;
  try {
    const result = await client.GET("/inbox/qq/status", { signal: op.signal, cache: "no-store" });
    if (!op.valid()) return;
    if ([401, 403].includes(result.response.status)) { revoke(); return; }
    if (result.response.ok && result.data) { error.value = ""; status.value = result.data; address.value = result.data.email ?? ""; }
    else error.value = "QQ邮箱连接状态暂不可读取。";
  } catch { if (op.valid()) error.value = "QQ邮箱连接状态暂不可读取。"; }
}
async function connect() {
  if (!gate.hasIdentity.value || denied.value || !confirmed.value || !password.value || !address.value || busy.value || !status.value?.configured) return;
  gate.invalidate();
  const op = gate.begin("connect"); if (!op?.valid()) return;
  busy.value = true; error.value = "";
  const body = { email: address.value.trim(), password: password.value, confirm_connect: true as const };
  password.value = ""; confirmed.value = false;
  try {
    const result = await client.POST("/inbox/qq/connect", { body, signal: op.signal });
    if (!op.valid()) return;
    if ([401, 403].includes(result.response.status)) { revoke(); return; }
    if (result.response.ok && result.data?.email === body.email.toLowerCase()) status.value = result.data;
    else error.value = "连接未完成，请检查授权码和邮箱的 SMTP/IMAP 权限，然后重新输入。";
  } catch { if (op.valid()) error.value = "连接结果待核对，请先刷新状态；不会自动重试。"; }
  finally { body.password = ""; if (op.valid()) busy.value = false; }
}
onMounted(() => void load());
</script>
<template>
  <section
    v-if="status?.configured || error"
    class="aliyun-panel"
    aria-label="QQ 邮箱连接"
  >
    <h2>QQ 邮箱</h2><p>在 QQ 邮箱网页进入设置 → 账户，开启 IMAP/SMTP 并生成授权码，然后直接填入下方。</p>
    <p>连接当前 TradeOS 账号使用的邮箱。专用密码只保存到服务器受保护的凭证目录，不进入聊天或模型。</p>
    <p
      v-if="status?.email"
      role="status"
    >
      {{ status.email }} 已通过收发认证 · {{ status.connected_at ? new Date(status.connected_at).toLocaleString('zh-CN') : '' }}
    </p>
    <p v-if="status?.email">
      连接不代表已经发送客户邮件；自动回复尚未开启。
    </p>
    <label>邮箱地址<input
      v-model="address"
      aria-label="QQ邮箱地址"
      type="email"
      autocomplete="username"
      :disabled="busy || denied || !!status?.email"
    ></label>
    <label>授权码<input
      v-model="password"
      aria-label="QQ邮箱授权码"
      type="password"
      autocomplete="new-password"
      :disabled="busy || denied"
      maxlength="512"
    ></label>
    <label class="confirm"><input
      v-model="confirmed"
      type="checkbox"
      :disabled="busy || denied"
    >我确认将此邮箱连接到当前账号</label>
    <div class="actions">
      <button
        :disabled="busy || denied || !confirmed || !password || !address"
        @click="connect"
      >
        {{ busy ? '正在验证连接…' : '连接QQ邮箱' }}
      </button>
      <button
        :disabled="busy || denied"
        @click="load"
      >
        刷新QQ邮箱状态
      </button>
      <a
        href="https://service.mail.qq.com/"
        target="_blank"
        rel="noopener noreferrer"
      >如何获取授权码</a>
    </div>
    <p
      v-if="error"
      role="alert"
    >
      {{ error }}
    </p>
  </section>
</template>
<style scoped>
.aliyun-panel { display: grid; gap: 12px; padding: 20px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface); min-width: 0; }
label { display: grid; gap: 6px; }
input:not([type="checkbox"]) { width: 100%; min-width: 0; }
.confirm, .actions { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
</style>
