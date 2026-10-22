<script setup lang="ts">
import { inject, onBeforeUnmount, onMounted, ref } from "vue";
import BusinessMailFollowup from "./BusinessMailFollowup.vue";
import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";
import { useQuoteRequestScope } from "../costing-quotes/quote-request-scope";

type Item = components["schemas"]["BusinessMailItem"];
type Detail = components["schemas"]["BusinessMailDetail"];
type Category = Exclude<Item["category"], "pending">;
type Bucket = Item["category"] | "all";
const client = inject<ReturnType<typeof createApiClient>>("tradeos-api-client", apiClient);
const items = ref<Item[]>([]);
const detail = ref<Detail | null>(null);
const selectedId = ref<string | null>(null);
const bucket = ref<Bucket>("all");
const offset = ref(0);
const nextOffset = ref<number | null>(null);
const loading = ref(false);
const detailLoading = ref(false);
const saving = ref(false);
const uncertain = ref(false);
const error = ref("");
const detailError = ref("");
const actionMessage = ref("");
const correction = ref<Category>("business");
const note = ref("");
const labels: Record<Item["category"], string> = {
  pending: "待识别", business: "业务邮件", other: "其他邮件", review: "待人工确认",
};
const filters: { key: Bucket; label: string }[] = [
  { key: "all", label: "全部来信" }, { key: "pending", label: labels.pending },
  { key: "business", label: labels.business }, { key: "review", label: labels.review },
  { key: "other", label: labels.other },
];
function failureLabel(code: string | null): string {
  const messages: Record<string, string> = {
    quota: "识别额度不足，邮件已保留，可人工分类",
    permission: "识别权限不可用，邮件已保留",
    configuration: "识别服务尚未配置，邮件已保留",
    credential_content: "正文可能包含敏感信息，请人工确认",
    input_guard: "正文需要人工确认，未提交识别",
    invalid_response: "识别结果无法确认，请人工分类",
    oversize: "邮件超过读取范围，仅保存邮件头，未提交模型识别",
    unknown: "识别结果待核对，邮件已保留",
  };
  return code ? messages[code] ?? "暂未完成识别，邮件已保留，可人工分类" : "";
}
function safeError(status: number): string {
  if ([401, 403].includes(status)) return "当前身份无权访问业务来信";
  if (status === 404) return "邮件不存在或当前不可见";
  if (status === 409) return "邮件分类已更新，请刷新后核对再保存";
  if (status === 503) return "来信服务暂不可用，请稍后重试";
  return "请求未完成，请稍后重试";
}
function clear(): void {
  items.value = []; detail.value = null; selectedId.value = null; nextOffset.value = null;
  note.value = ""; error.value = ""; detailError.value = ""; actionMessage.value = "";
  loading.value = false; detailLoading.value = false; saving.value = false; uncertain.value = false;
}
const gate = useQuoteRequestScope(client, () => [], () => {
  clear(); offset.value = 0;
  globalThis.queueMicrotask(() => { if (gate.hasIdentity.value) void load(); });
});
function revoke(status: number): boolean {
  if (![401, 403].includes(status)) return false;
  gate.invalidate(); clear(); error.value = safeError(status); return true;
}
function date(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "时间未知" : parsed.toLocaleString("zh-CN", { hour12: false });
}
async function load(background = false): Promise<void> {
  if (!gate.hasIdentity.value) return;
  const operation = gate.begin("list"); if (!operation?.valid()) return;
  if (!background) loading.value = true;
  if (!background) error.value = "";
  try {
    const result = await client.GET("/inbox/business-mail", {
      params: { query: { bucket: bucket.value, offset: offset.value, limit: 50 } }, signal: operation.signal,
    });
    if (!operation.valid()) return;
    if (!result.response.ok || !result.data) {
      if (!revoke(result.response.status)) error.value = safeError(result.response.status);
      return;
    }
    items.value = result.data.items; nextOffset.value = result.data.next_offset; error.value = "";
  } catch { if (operation.valid()) error.value = "无法连接来信服务，请稍后重试"; }
  finally { if (operation.valid()) loading.value = false; }
}
async function open(messageId: string, refresh = false): Promise<void> {
  if (selectedId.value !== messageId) { gate.invalidate(); saving.value = false; loading.value = false; }
  const operation = gate.begin("detail"); if (!operation?.valid()) return;
  selectedId.value = messageId; detailLoading.value = true; detailError.value = "";
  if (!refresh) { detail.value = null; note.value = ""; actionMessage.value = ""; uncertain.value = false; }
  try {
    const result = await client.GET("/inbox/business-mail/{message_id}", {
      params: { path: { message_id: messageId } }, signal: operation.signal,
    });
    if (!operation.valid() || selectedId.value !== messageId) return;
    if (!result.response.ok || !result.data) {
      if (!revoke(result.response.status)) { detail.value = null; detailError.value = safeError(result.response.status); }
      return;
    }
    detail.value = result.data; uncertain.value = false;
    correction.value = result.data.category === "pending" ? "business" : result.data.category;
  } catch { if (operation.valid()) detailError.value = "无法读取邮件，请稍后重试"; }
  finally { if (operation.valid()) detailLoading.value = false; }
}
function filter(next: Bucket): void {
  gate.invalidate(); clear(); bucket.value = next; offset.value = 0; void load();
}
function page(next: number): void {
  gate.invalidate(); clear(); offset.value = next; void load();
}
async function save(): Promise<void> {
  const current = detail.value;
  if (!current || saving.value || uncertain.value || !note.value.trim()) return;
  const operation = gate.begin("save"); if (!operation?.valid()) return;
  saving.value = true; actionMessage.value = "";
  try {
    const result = await client.POST("/inbox/business-mail/{message_id}/correct-classification", {
      params: { path: { message_id: current.message_id } },
      body: { expected_revision: current.revision, category: correction.value, note: note.value.trim() },
      signal: operation.signal,
    });
    if (!operation.valid() || selectedId.value !== current.message_id) return;
    if (!result.response.ok || !result.data) {
      if (!revoke(result.response.status)) {
        actionMessage.value = safeError(result.response.status);
        uncertain.value = result.response.status === 409 || result.response.status >= 500;
      }
      return;
    }
    detail.value = result.data; note.value = ""; actionMessage.value = "人工纠正已记录";
    void load(true);
  } catch {
    if (operation.valid()) { uncertain.value = true; actionMessage.value = "保存结果待核对，请刷新邮件查看记录；不会自动重复提交"; }
  } finally { if (operation.valid()) saving.value = false; }
}
let timer: ReturnType<typeof globalThis.setInterval> | undefined;
onMounted(() => { void load(); timer = globalThis.setInterval(() => { if (!loading.value && !saving.value) void load(true); }, 30000); });
onBeforeUnmount(() => { if (timer) globalThis.clearInterval(timer); });
</script>

<template>
  <main class="business-mail-page">
    <header class="mail-heading">
      <div><h1>业务来信</h1><p>先收下每封来信，再识别业务内容。待识别或识别失败的邮件也会保留。</p></div>
      <button
        type="button"
        :disabled="loading"
        @click="load()"
      >
        刷新列表
      </button>
    </header>
    <nav
      class="mail-filters"
      aria-label="来信分类"
    >
      <button
        v-for="choice in filters"
        :key="choice.key"
        type="button"
        :aria-pressed="bucket === choice.key"
        @click="filter(choice.key)"
      >
        {{ choice.label }}
      </button>
    </nav>
    <p
      v-if="error"
      class="mail-error"
      role="alert"
    >
      {{ error }}
    </p>
    <div class="mail-columns">
      <section
        class="mail-list"
        aria-label="业务来信列表"
      >
        <p
          v-if="loading"
          role="status"
        >
          正在读取来信…
        </p>
        <p
          v-else-if="!items.length && !error"
          class="mail-empty"
        >
          当前分类暂无来信
        </p>
        <button
          v-for="item in items"
          :key="item.message_id"
          type="button"
          class="business-mail-item"
          :aria-pressed="selectedId === item.message_id"
          @click="open(item.message_id)"
        >
          <span class="item-top"><strong>{{ item.sender || "发件人待核对" }}</strong><span class="category">{{ labels[item.category] }}</span></span>
          <span class="subject">{{ item.subject || "无主题" }}</span>
          <span class="snippet">{{ item.snippet }}</span>
          <span
            v-if="item.classification_status === 'classifying'"
            class="status"
          >正在识别</span>
          <span
            v-if="item.failure_code"
            class="status failure"
          >{{ failureLabel(item.failure_code) }}</span>
          <time>{{ date(item.received_at) }}</time>
        </button>
        <footer class="pagination">
          <button
            type="button"
            :disabled="offset === 0 || loading"
            @click="page(Math.max(0, offset - 50))"
          >
            上一页
          </button>
          <span>第 {{ Math.floor(offset / 50) + 1 }} 页</span>
          <button
            type="button"
            :disabled="nextOffset === null || loading"
            @click="nextOffset !== null && page(nextOffset)"
          >
            下一页
          </button>
        </footer>
      </section>
      <section
        class="mail-detail"
        aria-label="来信详情"
      >
        <p
          v-if="detailLoading"
          role="status"
        >
          正在读取邮件…
        </p>
        <p
          v-if="detailError"
          class="mail-error"
          role="alert"
        >
          {{ detailError }}
        </p>
        <template v-if="detail">
          <header class="detail-heading">
            <h2>{{ detail.subject || "无主题" }}</h2><button
              type="button"
              :disabled="detailLoading || saving"
              @click="open(detail.message_id, true)"
            >
              刷新邮件
            </button>
          </header>
          <p class="mail-meta">
            {{ detail.sender || "发件人待核对" }} → {{ detail.mailbox_address }}
          </p>
          <p class="mail-meta">
            {{ date(detail.received_at) }} · {{ labels[detail.category] }} · <strong>待确认联系人</strong>
          </p>
          <p
            v-if="detail.failure_code"
            class="mail-error"
          >
            {{ failureLabel(detail.failure_code) }}
          </p>
          <pre class="mail-body">{{ detail.body_text || "暂无可显示的纯文本正文，请核对原始邮件。" }}</pre>
          <details class="source-details">
            <summary>邮件来源</summary>
            <p
              v-if="detail.source.source_kind === 'headers_only'"
              class="mail-error"
            >
              仅保存邮件头，正文和附件尚未读取；未提交模型识别。
            </p>
            <dl><dt>来源邮箱</dt><dd>{{ detail.mailbox_address }} · QQ 收件箱</dd><dt>接收时间</dt><dd>{{ date(detail.source.received_at) }}</dd><dt>来源记录</dt><dd>{{ detail.source.artifact_id }}</dd><dt>已保存内容校验</dt><dd>{{ detail.source.source_sha256 }}</dd></dl>
          </details>
          <BusinessMailFollowup
            :key="detail.message_id"
            :message-id="detail.message_id"
          />
          <section class="classification-history">
            <h3>分类记录</h3>
            <p v-if="!detail.classification_history.length">
              尚无分类记录
            </p>
            <article
              v-for="entry in detail.classification_history"
              :key="entry.classification_id"
            >
              <strong>{{ entry.origin === 'model' ? '模型判断' : entry.origin === 'human' ? '人工纠正' : '处理记录' }} · {{ labels[entry.category] }}</strong>
              <p>{{ entry.reason }}</p><blockquote v-if="entry.evidence_quote">
                {{ entry.evidence_quote }}
              </blockquote>
              <small>{{ date(entry.created_at) }}<template v-if="entry.model"> · {{ entry.model }}</template><template v-if="entry.actor_id"> · {{ entry.actor_id }}</template></small>
            </article>
          </section>
          <form
            class="correction-form"
            @submit.prevent="save"
          >
            <h3>人工分类</h3><p>分类只整理邮件；联系人身份和采购需求仍需确认。</p>
            <label>分类<select
              v-model="correction"
              aria-label="人工分类"
              :disabled="saving"
            ><option value="business">业务邮件</option><option value="review">待人工确认</option><option value="other">其他邮件</option></select></label>
            <label>纠正原因<textarea
              v-model="note"
              aria-label="纠正原因"
              maxlength="1000"
              rows="3"
              required
              :disabled="saving"
              placeholder="说明判断依据，原模型记录会保留"
            /></label>
            <p
              v-if="actionMessage"
              role="status"
            >
              {{ actionMessage }}
            </p>
            <button
              type="submit"
              :disabled="saving || uncertain || !note.trim()"
            >
              {{ saving ? "保存中…" : "保存分类" }}
            </button>
          </form>
        </template>
        <p
          v-else-if="!detailLoading && !detailError"
          class="mail-empty"
        >
          选择一封来信查看正文和分类记录
        </p>
      </section>
    </div>
  </main>
</template>

<style scoped>
.business-mail-page { padding: 24px; color: #1f2937; }
.mail-heading, .detail-heading, .item-top, .pagination { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
h1 { margin: 0; font-size: 24px; } h2 { font-size: 20px; margin: 0; overflow-wrap: anywhere; } h3 { font-size: 15px; margin: 0 0 10px; }
.mail-heading p, .mail-meta, .correction-form p { color: #64748b; font-size: 13px; }
button, select, textarea { font: inherit; } button { border: 1px solid #d6dee8; border-radius: 7px; background: white; color: #334155; padding: 8px 12px; cursor: pointer; } button:disabled { cursor: default; opacity: .5; } button[aria-pressed="true"] { border-color: #2563eb; background: #eff6ff; color: #1d4ed8; }
.mail-filters { display: flex; gap: 8px; flex-wrap: wrap; margin: 20px 0; }
.mail-columns { display: grid; grid-template-columns: minmax(250px, 340px) minmax(0, 1fr); border: 1px solid #e2e8f0; border-radius: 10px; overflow: hidden; background: white; min-height: 440px; }
.mail-list { border-right: 1px solid #e2e8f0; min-width: 0; } .business-mail-item { display: flex; flex-direction: column; align-items: stretch; text-align: left; gap: 7px; width: 100%; border: 0; border-bottom: 1px solid #e2e8f0; border-radius: 0; padding: 16px; }
.item-top strong { min-width: 0; overflow: hidden; text-overflow: ellipsis; font-size: 13px; } .category { flex-shrink: 0; font-size: 11px; background: #f1f5f9; border-radius: 4px; padding: 3px 6px; color: #475569; }
.subject { font-weight: 600; overflow-wrap: anywhere; } .snippet { color: #64748b; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; font-size: 13px; } time, .status { font-size: 12px; color: #64748b; } .failure { color: #92400e; }
.mail-detail { padding: 24px; min-width: 0; } .mail-body { white-space: pre-wrap; overflow-wrap: anywhere; font-family: inherit; line-height: 1.7; margin: 24px 0; font-size: 14px; }
.mail-meta { overflow-wrap: anywhere; } .source-details { padding: 12px 0; border-top: 1px solid #e2e8f0; } summary { cursor: pointer; font-size: 13px; } dl { font-size: 12px; color: #64748b; } dd { margin: 4px 0 12px; overflow-wrap: anywhere; }
.classification-history, .correction-form { border-top: 1px solid #e2e8f0; padding-top: 18px; margin-top: 16px; } .classification-history article { margin: 12px 0; padding: 12px; background: #f8fafc; border-radius: 6px; font-size: 13px; overflow-wrap: anywhere; } article p { margin: 6px 0; } small { color: #64748b; } blockquote { border-left: 2px solid #cbd5e1; margin: 8px 0; padding-left: 10px; }
.correction-form label { display: flex; flex-direction: column; gap: 6px; font-size: 13px; margin: 12px 0; } select, textarea { border: 1px solid #cbd5e1; border-radius: 6px; padding: 8px; background: white; color: #334155; max-width: 100%; } textarea { resize: vertical; }
.mail-empty { padding: 20px; color: #64748b; font-size: 13px; } .mail-error { color: #92400e; padding: 10px 12px; border-radius: 6px; background: #fffbeb; font-size: 13px; } .pagination { padding: 12px; font-size: 12px; }
@media (max-width: 760px) { .business-mail-page { padding: 14px; } .mail-heading { align-items: flex-start; } .mail-heading button, .detail-heading button { flex-shrink: 0; } .mail-columns { grid-template-columns: minmax(0, 1fr); } .mail-list { border-right: 0; border-bottom: 1px solid #e2e8f0; } .mail-detail { padding: 16px; } }
</style>
