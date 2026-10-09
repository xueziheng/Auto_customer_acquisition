<script setup lang="ts">
import { computed, inject, onBeforeUnmount, onMounted, ref, watch } from "vue";
import GmailConnectionPanel from "./GmailConnectionPanel.vue";
import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";
import { useQuoteRequestScope } from "../costing-quotes/quote-request-scope";

type Mailbox = components["schemas"]["MailboxView"];
type Thread = components["schemas"]["MailThreadView"];
type Message = components["schemas"]["MailMessageView"];
const client = inject<ReturnType<typeof createApiClient>>("tradeos-api-client", apiClient);
const mailboxes = ref<Mailbox[]>([]);
const mailboxId = ref("");
const label = ref("");
const query = ref("");
const search = ref("");
const threads = ref<Thread[]>([]);
const messages = ref<Message[]>([]);
const selected = ref("");
const nextOffset = ref<number | null>(null);
const nextMessage = ref<number | null>(null);
const offset = ref(0);
const loading = ref(false);
const reading = ref(false);
const error = ref("");
const notice = ref("");
const syncing = ref(false);
let disposed = false;
let polling = false;
let timer: ReturnType<typeof globalThis.setInterval> | undefined;
const current = computed(() => mailboxes.value.find(item => item.mailbox_id === mailboxId.value));
const phases = { backfill: "正在补齐历史邮件", catch_up: "正在追平最新变更", synced: "历史已补齐" };
const failures: Record<string, string> = {
  authorization_required: "Google 授权已失效，请重新连接邮箱",
  account_mismatch: "授权的 Google 账号与绑定邮箱不同，同步已停止",
  rate_limited: "Google 暂时限流，后台将稍后重试",
  provider_unavailable: "暂时无法连接 Gmail，已保留同步进度",
  configuration_invalid: "邮箱连接配置不可用，请重新连接 Gmail",
  invalid_response: "邮件读取未完成，已保留同步进度",
};
function clear() {
  mailboxes.value = []; mailboxId.value = ""; threads.value = []; messages.value = [];
  selected.value = ""; nextOffset.value = null; nextMessage.value = null;
  error.value = ""; notice.value = ""; loading.value = false; reading.value = false; syncing.value = false;
}
const gate = useQuoteRequestScope(client, () => [mailboxId.value, label.value, search.value], () => {
  clear(); if (!disposed) globalThis.queueMicrotask(() => { if (!disposed) void loadMailboxes(); });
});
function date(value: string | null) { return value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "尚未完成"; }
function fail(status: number) {
  if ([401, 403].includes(status)) { gate.invalidate(); clear(); }
  error.value = [401, 403].includes(status) ? "当前账号无权查看此邮箱" : "邮箱服务暂不可用，请重试";
}
async function loadMailboxes() {
  if (!gate.hasIdentity.value || disposed) return false;
  const op = gate.begin("accounts"); if (!op) return false;
  try {
    const result = await client.GET("/inbox/mailboxes", { signal: op.signal });
    if (!op.valid()) return false;
    if (!result.data || !result.response.ok) { fail(result.response.status); return false; }
    const previous = current.value;
    mailboxes.value = result.data;
    if (!result.data.some(item => item.mailbox_id === mailboxId.value)) mailboxId.value = result.data[0]?.mailbox_id ?? "";
    const updated = current.value;
    return previous !== undefined && updated !== undefined && previous.mailbox_id === updated.mailbox_id
      && (previous.last_attempt_at !== updated.last_attempt_at || previous.last_synced_at !== updated.last_synced_at
        || previous.message_count !== updated.message_count || previous.phase !== updated.phase);
  } catch { if (op.valid()) error.value = "无法连接邮箱服务"; }
  return false;
}
async function loadThreads(page = 0, background = false) {
  if (!mailboxId.value) return;
  const op = gate.begin("threads"); if (!op) return;
  if (!background) loading.value = true;
  error.value = "";
  try {
    const result = await client.GET("/inbox/mailboxes/{mailbox_id}/threads", {
      params: { path: { mailbox_id: mailboxId.value }, query: { search: search.value, label: label.value || undefined, offset: page, limit: 50 } }, signal: op.signal,
    });
    if (!op.valid()) return;
    if (!result.data || !result.response.ok) {
      if (!background) { threads.value = []; messages.value = []; }
      fail(result.response.status); return;
    }
    threads.value = result.data.items; nextOffset.value = result.data.next_offset; offset.value = page;
  } catch { if (op.valid()) error.value = "无法读取邮件列表"; }
  finally { if (op.valid()) loading.value = false; }
}
async function readThread(id: string, page = 0) {
  const op = gate.begin("messages"); if (!op) return;
  if (!page) { messages.value = []; nextMessage.value = null; }
  selected.value = id; reading.value = true; error.value = "";
  try {
    const result = await client.GET("/inbox/mailboxes/{mailbox_id}/threads/{thread_id}", {
      params: { path: { mailbox_id: mailboxId.value, thread_id: id }, query: { offset: page, limit: 50 } }, signal: op.signal,
    });
    if (!op.valid() || selected.value !== id) return;
    if (!result.data || !result.response.ok) { messages.value = []; fail(result.response.status); return; }
    messages.value = page ? [...messages.value, ...result.data.items] : result.data.items;
    nextMessage.value = result.data.next_offset;
  } catch { if (op.valid()) error.value = "无法读取邮件内容"; }
  finally { if (op.valid()) reading.value = false; }
}
async function requestSync() {
  const op = gate.begin("sync"); if (!op || !mailboxId.value) return;
  syncing.value = true; notice.value = "";
  try {
    const result = await client.POST("/inbox/mailboxes/{mailbox_id}/sync", { params: { path: { mailbox_id: mailboxId.value } }, signal: op.signal });
    if (!op.valid()) return;
    if (result.response.status !== 202) { fail(result.response.status); return; }
    notice.value = "已请求同步，等待后台处理。";
    await loadMailboxes();
  } catch { if (op.valid()) error.value = "同步请求未完成"; }
  finally { if (op.valid()) syncing.value = false; }
}
function find() { if (search.value === query.value.trim()) void loadThreads(); else search.value = query.value.trim(); }
async function refresh() { await loadMailboxes(); await loadThreads(offset.value); if (selected.value) await readThread(selected.value); }
async function poll() {
  if (polling || loading.value || reading.value || syncing.value) return;
  polling = true;
  try { if (await loadMailboxes()) await loadThreads(offset.value, true); }
  finally { polling = false; }
}
watch([mailboxId, label, search], () => {
  threads.value = []; messages.value = []; selected.value = ""; nextMessage.value = null; nextOffset.value = null;
  loading.value = false; reading.value = false; syncing.value = false; notice.value = "";
  void loadThreads();
}, { flush: "sync" });
onMounted(() => { void loadMailboxes(); timer = globalThis.setInterval(() => void poll(), 5000); });
onBeforeUnmount(() => { disposed = true; if (timer) globalThis.clearInterval(timer); });
</script>

<template>
  <main class="mailbox-page">
    <header class="mailbox-header">
      <div><h1>我的邮箱</h1><p>查看本人账号下的历史邮件和往来会话</p></div>
      <RouterLink to="/inbox">
        客户回复工作台
      </RouterLink>
    </header>
    <GmailConnectionPanel @connected="refresh" />
    <p
      v-if="error"
      role="alert"
      class="mailbox-error"
    >
      {{ error }}
    </p>
    <section
      v-if="!mailboxes.length && !error"
      class="mailbox-empty"
    >
      <h2>尚未连接邮箱</h2>
      <p>连接 Gmail 并完成授权后，将补齐整个账号的历史邮件。已有邮件不会因为连接大模型而自动出现。</p>
      <p>邮箱仅本人可见，邮件不会自动发送给大模型或转为客户、商机。</p>
    </section>
    <template v-if="mailboxes.length">
      <section
        class="mailbox-status"
        aria-label="邮箱同步状态"
      >
        <label>邮箱账号 <select v-model="mailboxId"><option
          v-for="box in mailboxes"
          :key="box.mailbox_id"
          :value="box.mailbox_id"
        >{{ box.email }}</option></select></label>
        <template v-if="current">
          <strong>{{ phases[current.phase] }}</strong><span>已保存 {{ current.message_count }} 封</span>
          <span>最近同步完成：{{ date(current.last_synced_at) }}</span>
          <span v-if="current.sync_requested">等待后台同步</span>
          <p
            v-if="current.failure_code"
            role="alert"
            class="mailbox-error"
          >
            {{ failures[current.failure_code] ?? "同步未完成，请检查邮箱连接" }}
          </p>
          <p v-if="current.phase !== 'synced'">
            当前仅显示已同步部分，历史邮件尚未全部补齐。
          </p>
        </template>
        <button
          :disabled="syncing"
          @click="requestSync"
        >
          {{ syncing ? "提交中…" : "立即同步" }}
        </button>
        <button @click="refresh">
          刷新列表
        </button>
        <p
          v-if="notice"
          role="status"
        >
          {{ notice }}
        </p>
      </section>
      <form
        class="mailbox-filters"
        @submit.prevent="find"
      >
        <label>范围 <select v-model="label"><option value="">全部邮件</option><option value="INBOX">收件箱</option><option value="SENT">已发送</option><option value="ARCHIVED">归档</option><option value="DRAFT">草稿</option><option value="SPAM">垃圾邮件</option><option value="TRASH">已删除</option><option value="UNREAD">未读</option></select></label>
        <input
          v-model="query"
          aria-label="搜索邮件"
          placeholder="搜索主题、发件人或正文"
          maxlength="200"
        >
        <button type="submit">
          搜索
        </button>
      </form>
      <div class="mailbox-columns">
        <section
          class="mailbox-list"
          aria-label="邮件会话"
        >
          <p
            v-if="loading"
            role="status"
          >
            正在读取邮件…
          </p>
          <p v-else-if="!threads.length">
            当前范围内没有已同步邮件
          </p>
          <button
            v-for="thread in threads"
            :key="thread.thread_id"
            class="mailbox-thread"
            :class="{ selected: selected === thread.thread_id }"
            @click="readThread(thread.thread_id)"
          >
            <span><strong>{{ thread.subject }}</strong><b
              v-if="thread.unread"
              class="unread"
            >未读</b></span>
            <span>{{ thread.sender }}</span><small>{{ date(thread.latest_at) }} · {{ thread.message_count }} 封</small><span>{{ thread.snippet }}</span>
          </button>
          <nav aria-label="会话分页">
            <button
              :disabled="loading || offset === 0"
              @click="loadThreads(Math.max(0, offset - 50))"
            >
              上一页
            </button><button
              :disabled="loading || nextOffset === null"
              @click="loadThreads(nextOffset ?? 0)"
            >
              下一页
            </button>
          </nav>
        </section>
        <section
          class="mailbox-detail"
          aria-label="邮件内容"
        >
          <p v-if="!selected">
            选择一条会话，查看完整往来
          </p>
          <article
            v-for="message in messages"
            :key="message.message_id"
            class="mailbox-message"
          >
            <header><strong>{{ message.labels.includes('DRAFT') ? "草稿" : message.labels.includes('SENT') ? "已发送" : "收到的邮件" }} · {{ message.subject }}</strong><time>{{ date(message.occurred_at) }}</time></header>
            <p>发件人：{{ message.sender }}<br>收件人：{{ message.recipients }}</p>
            <pre>{{ message.body_text || "此邮件没有可显示的正文，请打开 Gmail 查看原件。" }}</pre>
            <ul v-if="message.attachments.length">
              <li
                v-for="(attachment, index) in message.attachments"
                :key="index"
              >
                附件：{{ attachment.filename }}（{{ attachment.size }} 字节）
              </li>
            </ul>
            <a
              :href="message.source_url"
              target="_blank"
              rel="noopener noreferrer"
            >在 Gmail 查看原邮件与附件</a>
          </article>
          <p
            v-if="reading"
            role="status"
          >
            正在读取会话…
          </p>
          <button
            v-if="nextMessage !== null"
            :disabled="reading"
            @click="readThread(selected, nextMessage)"
          >
            继续加载往来邮件
          </button>
        </section>
      </div>
    </template>
  </main>
</template>

<style scoped>
.mailbox-page { flex: 1; min-height: 0; overflow-y: auto; width: 100%; padding: 24px; max-width: 1500px; margin: 0 auto; color: #172a3a; }
.mailbox-header { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.mailbox-header h1 { margin: 0; }.mailbox-header p { color: #61717f; }
.mailbox-status,.mailbox-empty { padding: 18px; background: #f3f7fa; border: 1px solid #dce5ec; border-radius: 10px; margin: 16px 0; }
.mailbox-status { display: flex; gap: 12px; flex-wrap: wrap; align-items: center; }.mailbox-status p { width: 100%; margin: 0; }
.mailbox-error { color: #9d2727; }.mailbox-filters { display: flex; gap: 12px; margin: 20px 0; }.mailbox-filters input { flex: 1; min-width: 100px; }
input,select,button { padding: 9px 12px; border: 1px solid #c9d6e0; border-radius: 6px; background: white; color: inherit; font: inherit; max-width: 100%; }
button { cursor: pointer; }button:disabled { cursor: default; opacity: .5; }
.mailbox-columns { display: grid; grid-template-columns: minmax(240px, 34%) minmax(0, 1fr); gap: 20px; }
.mailbox-list,.mailbox-detail { min-width: 0; }.mailbox-thread { display: flex; flex-direction: column; gap: 7px; text-align: left; width: 100%; padding: 14px; margin-bottom: 8px; overflow-wrap: anywhere; }.mailbox-thread.selected { background: #eef6ff; border-color: #2682cb; }
.mailbox-thread > span:last-child { color: #657586; display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; overflow: hidden; }.unread { font-size: 12px; margin-left: 8px; color: #1266a7; }
nav { display: flex; gap: 10px; }.mailbox-message { border: 1px solid #dce5ec; border-radius: 10px; padding: 18px; margin-bottom: 16px; overflow-wrap: anywhere; }.mailbox-message header { display: grid; gap: 8px; }.mailbox-message time { color: #657586; font-size: 13px; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; font-family: inherit; line-height: 1.65; }a { color: #146ea8; }
@media (max-width: 700px) { .mailbox-page { padding: 12px; }.mailbox-columns { grid-template-columns: 1fr; }.mailbox-header,.mailbox-filters { flex-wrap: wrap; }.mailbox-filters input { width: 100%; flex-basis: 100%; } }
</style>
