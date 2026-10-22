<script setup lang="ts">
import { computed, inject, onBeforeUnmount, onMounted, ref } from "vue";
import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";
import { useQuoteRequestScope } from "../costing-quotes/quote-request-scope";

type View = components["schemas"]["BusinessMailFollowupView"];
type Command = components["schemas"]["BusinessMailHandlingCommand"];
const props = defineProps<{ messageId: string }>();
const client = inject<ReturnType<typeof createApiClient>>("tradeos-api-client", apiClient);
const view = ref<View | null>(null);
const note = ref("");
const error = ref("");
const loading = ref(false);
const saving = ref(false);
const pending = ref<{ key: string; body: Command } | null>(null);
const labels = { pending: "待处理", in_progress: "处理中", waiting_customer: "等待客户回复", completed: "本次处理已完成" };
const gate = useQuoteRequestScope(client, () => [props.messageId], () => {
  view.value = null; note.value = ""; pending.value = null; error.value = "";
  loading.value = false; saving.value = false;
});
const sent = computed(() => view.value?.history.some(e => /^(inquiry-sent:|business-reply-sent:)/.test(e.request_key)) ?? false);
const visibleHistory = computed(() => view.value?.history.filter(e => !/^(inquiry-claim:|inquiry-scope:)/.test(e.request_key)) ?? []);
function time(value: string): string { return new Date(value).toLocaleString("zh-CN", { hour12: false }); }
function failure(status: number): string {
  if (status === 401 || status === 403) { gate.invalidate(); view.value = null; note.value = ""; pending.value = null; return "当前身份无权查看此处理记录"; }
  if (status === 409) return "处理记录已更新，请刷新核对后再操作";
  return "处理服务暂不可用，请刷新核对";
}
function analysis(noteText: string): { reason: string; fields: { name: string; value: string; quote: string }[]; source: string } | null {
  try {
    const raw = JSON.parse(noteText);
    if (!raw || !raw.analysis || !Array.isArray(raw.analysis.fields) || typeof raw.source_id !== "string") return null;
    const fieldLabels: Record<string, string> = { product: "产品", application: "用途", quantity: "数量" };
    return { reason: String(raw.analysis.reason ?? "待人工核对"), source: raw.source_id,
      fields: raw.analysis.fields.filter((f: unknown) => f && typeof f === "object" && "name" in f && "value" in f && "evidence_quote" in f)
        .map((f: { name: string; value: string; evidence_quote: string }) => ({ name: fieldLabels[f.name] ?? f.name, value: String(f.value), quote: String(f.evidence_quote) })) };
  } catch { return null; }
}
async function load(): Promise<void> {
  const op = gate.begin("followup-read"); if (!op?.valid() || saving.value) return;
  loading.value = true; error.value = "";
  try {
    const result = await client.GET("/inbox/business-mail/{message_id}/followup", { params: { path: { message_id: props.messageId } }, signal: op.signal });
    if (!op.valid()) return;
    if (!result.response.ok || !result.data || !Array.isArray(result.data.history)) { error.value = failure(result.response.status); return; }
    view.value = result.data;
    if (pending.value && result.data.history.some(e => e.request_key === pending.value?.key)) {
      pending.value = null; note.value = "";
    }
  } catch { if (op.valid()) error.value = "无法读取处理记录，请稍后刷新"; }
  finally { if (op.valid()) loading.value = false; }
}
async function handle(status: Command["status"], retry = false): Promise<void> {
  if (!view.value || saving.value || (!retry && (pending.value || !note.value.trim()))) return;
  const op = gate.begin("followup-handle"); if (!op?.valid()) return;
  if (!retry) pending.value = { key: `business-owner:${globalThis.crypto.randomUUID()}`, body: { expected_revision: view.value.revision, status, note: note.value.trim() } };
  const request = pending.value; if (!request) return;
  saving.value = true; error.value = "";
  try {
    const result = await client.POST("/inbox/business-mail/{message_id}/handling", {
      params: { path: { message_id: props.messageId } }, headers: { "Idempotency-Key": request.key }, body: request.body, signal: op.signal,
    });
    if (!op.valid()) return;
    if (!result.response.ok || !result.data) {
      error.value = failure(result.response.status);
      if (result.response.status === 400 || result.response.status === 409) pending.value = null;
      return;
    }
    view.value = result.data; pending.value = null; note.value = "";
  } catch { if (op.valid()) error.value = "保存结果待核对，请先刷新处理记录；需要恢复时使用原处理请求"; }
  finally { if (op.valid()) saving.value = false; }
}
let timer: ReturnType<typeof globalThis.setInterval> | undefined;
onMounted(() => { void load(); timer = globalThis.setInterval(() => { if (!saving.value && !loading.value && !note.value && !pending.value) void load(); }, 30000); });
onBeforeUnmount(() => { if (timer) globalThis.clearInterval(timer); });
</script>

<template>
  <section
    class="followup"
    aria-label="询盘跟进与负责人处理"
  >
    <header>
      <h3>询盘跟进与负责人处理</h3><button
        type="button"
        :disabled="loading || saving"
        @click="load"
      >
        刷新处理记录
      </button>
    </header>
    <p
      v-if="error"
      role="alert"
    >
      {{ error }}
    </p>
    <template v-if="view">
      <p><strong>{{ labels[view.status] }}</strong> · 当前邮箱负责人处理</p>
      <p class="hint">
        模型提取需要人工核对；测试数据不计入真实采购需求或商机。
      </p>
      <details v-if="view.draft">
        <summary>回复内容 · {{ sent ? "邮件服务已接受发送" : "草稿，尚无成功发送记录" }}</summary>
        <strong>{{ view.draft.subject }}</strong><pre>{{ view.draft.body_text }}</pre>
      </details>
      <p
        v-if="!visibleHistory.length"
        class="hint"
      >
        尚无处理记录
      </p>
      <article
        v-for="entry in visibleHistory"
        :key="entry.event_id"
      >
        <template v-if="analysis(entry.note)">
          <strong>模型提取 · 待负责人核对</strong><p>{{ analysis(entry.note)?.reason }}</p>
          <dl
            v-for="field in analysis(entry.note)?.fields"
            :key="field.name"
          >
            <dt>{{ field.name }}：{{ field.value }}</dt><dd>客户原话：“{{ field.quote }}”</dd>
          </dl>
          <details><summary>来源依据</summary><pre>{{ entry.note }}</pre></details>
        </template>
        <template v-else>
          <strong>{{ entry.kind === "draft_saved" ? "回复草稿已保存" : labels[entry.status] }}</strong>
          <p>{{ entry.note }}</p>
        </template>
        <small>{{ time(entry.created_at) }}</small>
      </article>
      <template v-if="view.status !== 'completed'">
        <label>处理说明<textarea
          v-model="note"
          aria-label="负责人处理说明"
          rows="4"
          maxlength="2000"
          :disabled="saving || !!pending"
          placeholder="写明已核对的信息、仍缺少的依据及下一步"
        /></label>
        <p class="hint">
          完成本次处理只保存处理结果，不自动确认采购、生成商机或发送邮件。
        </p>
        <div class="actions">
          <button
            type="button"
            :disabled="saving || !!pending || !note.trim()"
            @click="handle('waiting_customer')"
          >
            等待客户补充
          </button>
          <button
            type="button"
            :disabled="saving || !!pending || !note.trim()"
            @click="handle('completed')"
          >
            完成本次处理
          </button>
          <button
            v-if="pending"
            type="button"
            :disabled="saving || loading"
            @click="handle(pending.body.status, true)"
          >
            恢复原处理请求
          </button>
        </div>
      </template>
    </template>
  </section>
</template>

<style scoped>
.followup { margin-top: 24px; border-top: 1px solid #d6dee8; padding-top: 18px; overflow-wrap: anywhere; }
header, .actions { display: flex; gap: 10px; align-items: center; justify-content: space-between; flex-wrap: wrap; }
h3 { font-size: 16px; margin: 0; } p, dt, dd { font-size: 13px; line-height: 1.6; } .hint, small { color: #64748b; }
article { padding: 12px; background: #f8fafc; margin: 12px 0; border-radius: 6px; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; font-family: inherit; font-size: 12px; line-height: 1.6; }
label { display: grid; gap: 8px; font-size: 13px; } textarea { width: 100%; box-sizing: border-box; padding: 8px; border: 1px solid #cbd5e1; border-radius: 6px; font: inherit; }
button { padding: 8px 12px; background: white; border: 1px solid #cbd5e1; border-radius: 6px; cursor: pointer; font: inherit; }
button:disabled { opacity: .5; cursor: default; } summary { cursor: pointer; font-size: 13px; } [role=alert] { color: #92400e; }
</style>
