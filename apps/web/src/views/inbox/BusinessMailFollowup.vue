<script setup lang="ts">
import { computed, inject, onMounted, ref } from "vue";
import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";
import { useQuoteRequestScope } from "../costing-quotes/quote-request-scope";

type View = components["schemas"]["BusinessMailFollowupView"];
type DraftCommand = components["schemas"]["BusinessMailDraftCommand"];
type HandlingCommand = components["schemas"]["BusinessMailHandlingCommand"];
type Intent = { key: string; kind: "draft"; body: DraftCommand } | { key: string; kind: "handling"; body: HandlingCommand };
const props = defineProps<{ messageId: string; subject: string; sourceKind: "full_mime" | "headers_only" }>();
const emit = defineEmits<{ unauthorized: [status: number] }>();
const client = inject<ReturnType<typeof createApiClient>>("tradeos-api-client", apiClient);
const view = ref<View | null>(null);
const loading = ref(false);
const saving = ref(false);
const error = ref("");
const notice = ref("");
const draftSubject = ref("");
const bodyText = ref("");
const status = ref<HandlingCommand["status"]>("in_progress");
const handlingBaseline = ref<HandlingCommand["status"]>("in_progress");
const note = ref("");
const unresolved = ref<Intent | null>(null);
const labels: Record<View["status"], string> = { pending: "待处理", in_progress: "处理中", waiting_customer: "等待客户回复", completed: "处理完成" };
const disabled = computed(() => loading.value || saving.value || !view.value || unresolved.value !== null || !gate.hasIdentity.value);
const draftDisabled = computed(() => disabled.value || props.sourceKind !== "full_mime");
const draftDirty = computed(() => draftSubject.value !== (view.value?.draft?.subject ?? "") || bodyText.value !== (view.value?.draft?.body_text ?? ""));
function reset(): void {
  view.value = null; draftSubject.value = ""; bodyText.value = ""; note.value = "";
  status.value = handlingBaseline.value = "in_progress"; error.value = ""; notice.value = ""; unresolved.value = null;
  loading.value = false; saving.value = false;
}
const gate = useQuoteRequestScope(client, () => [props.messageId], reset);
function date(value: string): string { return new Date(value).toLocaleString("zh-CN", { hour12: false }); }
function responseError(code: number): string {
  if (code === 409) return "处理记录已更新，请刷新核对后再保存";
  if (code === 400 || code === 422) return "填写内容未通过校验，请检查主题、正文或处理说明";
  if (code === 404) return "邮件处理记录当前不可用";
  return "处理服务暂不可用，请稍后核对";
}
function revoke(code: number): boolean {
  if (![401, 403].includes(code)) return false;
  gate.invalidate(); reset(); emit("unauthorized", code); return true;
}
function validView(data: View | undefined): data is View {
  return data?.message_id === props.messageId && Number.isInteger(data.revision)
    && Array.isArray(data.history) && data.status in labels;
}
function apply(data: View, mode: "initial" | "read" | "draft" | "handling"): void {
  const wasDirty = draftDirty.value;
  const handlingDirty = status.value !== handlingBaseline.value;
  const receipt = unresolved.value && data.history.find(entry => entry.request_key === unresolved.value?.key);
  const completedKind = receipt ? unresolved.value?.kind : null;
  view.value = data;
  if (mode === "initial" || mode === "draft" || completedKind === "draft" || !wasDirty) {
    draftSubject.value = data.draft?.subject ?? ""; bodyText.value = data.draft?.body_text ?? "";
  }
  if (mode === "handling" || completedKind === "handling") note.value = "";
  const nextStatus = data.status === "pending" ? "in_progress" : data.status;
  if (mode === "initial" || mode === "handling" || completedKind === "handling" || !handlingDirty) status.value = nextStatus;
  handlingBaseline.value = nextStatus;
  if (receipt) unresolved.value = null;
  if (mode === "draft" || completedKind === "draft") notice.value = "草稿已保存，尚未发送";
  else if (mode === "handling" || completedKind === "handling") notice.value = "处理记录已保存";
  else if (unresolved.value) notice.value = "保存结果待核对；尚未查到原请求记录，可重试同一保存请求";
}
async function load(): Promise<void> {
  if (!gate.hasIdentity.value || saving.value) return;
  const operation = gate.begin("read"); if (!operation?.valid()) return;
  loading.value = true; error.value = "";
  try {
    const result = await client.GET("/inbox/business-mail/{message_id}/followup", {
      params: { path: { message_id: props.messageId } }, signal: operation.signal,
    });
    if (!operation.valid()) return;
    if (!result.response.ok || !validView(result.data)) {
      if (!revoke(result.response.status)) error.value = responseError(result.response.status);
      return;
    }
    apply(result.data, view.value ? "read" : "initial");
  } catch { if (operation.valid()) error.value = "无法读取处理记录，请稍后刷新"; }
  finally { if (operation.valid()) loading.value = false; }
}
function useTemplate(): void {
  if (draftDisabled.value) return;
  draftSubject.value = (/^re:/i.test(props.subject) ? props.subject : `Re: ${props.subject || "Your inquiry"}`).slice(0, 500);
  bodyText.value = "Hello,\n\nThank you for your inquiry. Could you share your estimated order quantity and destination? This will help us check the details for your request.\n\nBest regards";
  notice.value = "已填写通用询盘模板，可编辑后保存";
}
async function submit(intent: Intent): Promise<void> {
  if (saving.value || loading.value || !view.value || !gate.hasIdentity.value) return;
  const operation = gate.begin("save"); if (!operation?.valid()) return;
  saving.value = true; notice.value = ""; error.value = ""; unresolved.value = intent;
  try {
    const params = { path: { message_id: props.messageId } };
    const headers = { "Idempotency-Key": intent.key };
    const result = intent.kind === "draft"
      ? await client.POST("/inbox/business-mail/{message_id}/reply-draft", { params, headers, body: intent.body, signal: operation.signal })
      : await client.POST("/inbox/business-mail/{message_id}/handling", { params, headers, body: intent.body, signal: operation.signal });
    if (!operation.valid()) return;
    if (!result.response.ok || !validView(result.data)) {
      if (revoke(result.response.status)) return;
      if (result.response.status >= 400 && result.response.status < 500) unresolved.value = null;
      notice.value = unresolved.value ? "保存结果待核对，请刷新处理记录；不会自动重复提交" : responseError(result.response.status);
      return;
    }
    if (!result.data.history.some(entry => entry.request_key === intent.key)) {
      notice.value = "保存结果待核对，请刷新处理记录"; return;
    }
    apply(result.data, intent.kind); unresolved.value = null;
  } catch { if (operation.valid()) notice.value = "保存结果待核对，请刷新处理记录；不会自动重复提交"; }
  finally { if (operation.valid()) saving.value = false; }
}
function saveDraft(): void {
  if (draftDisabled.value || !draftSubject.value.trim() || !bodyText.value.trim()) return;
  void submit({ kind: "draft", key: globalThis.crypto.randomUUID(), body: { expected_revision: view.value!.revision, subject: draftSubject.value.trim(), body_text: bodyText.value.trim() } });
}
function saveHandling(): void {
  if (disabled.value || !note.value.trim()) return;
  void submit({ kind: "handling", key: globalThis.crypto.randomUUID(), body: { expected_revision: view.value!.revision, status: status.value, note: note.value.trim() } });
}
onMounted(() => { void load(); });
</script>

<template>
  <section
    class="followup"
    aria-label="询盘处理"
  >
    <header>
      <h3>继续处理</h3><button
        type="button"
        :disabled="loading || saving"
        @click="load"
      >
        刷新处理记录
      </button>
    </header>
    <p
      v-if="loading"
      role="status"
    >
      正在读取处理记录…
    </p>
    <p
      v-if="error"
      class="warning"
      role="alert"
    >
      {{ error }}
    </p>
    <template v-if="view">
      <p class="meta">
        负责人：{{ view.owner_id }} · {{ labels[view.status] }}
      </p>
      <form @submit.prevent="saveDraft">
        <header>
          <h4>回复草稿</h4><button
            type="button"
            :disabled="draftDisabled"
            @click="useTemplate"
          >
            使用询盘模板
          </button>
        </header>
        <p class="meta">
          编辑并保存英文回复。草稿尚未发送。
        </p>
        <p
          v-if="sourceKind !== 'full_mime'"
          class="warning"
        >
          正文未完整读取，请先核对原始邮件，暂不能准备回复。
        </p>
        <label>主题<input
          v-model="draftSubject"
          aria-label="回复主题"
          maxlength="500"
          required
          :disabled="draftDisabled"
        ></label>
        <label>正文<textarea
          v-model="bodyText"
          aria-label="回复正文"
          maxlength="10000"
          rows="8"
          required
          :disabled="draftDisabled"
        /></label>
        <p
          v-if="view.draft"
          class="meta"
        >
          草稿已保存，尚未发送 · {{ date(view.draft.created_at) }}
        </p>
        <button
          type="submit"
          :disabled="draftDisabled || !draftSubject.trim() || !bodyText.trim()"
        >
          保存草稿
        </button>
      </form>
      <form @submit.prevent="saveHandling">
        <h4>负责人处理记录</h4>
        <label>处理状态<select
          v-model="status"
          aria-label="处理状态"
          :disabled="disabled"
        ><option value="in_progress">处理中</option><option value="waiting_customer">等待客户回复</option><option value="completed">处理完成</option></select></label>
        <label>处理说明<textarea
          v-model="note"
          aria-label="处理说明"
          maxlength="2000"
          rows="3"
          required
          :disabled="disabled"
          placeholder="记录已做的处理和下一步安排"
        /></label>
        <p class="meta">
          处理状态不代表邮件已发出，也不代表需求或商机已确认。
        </p>
        <button
          type="submit"
          :disabled="disabled || !note.trim()"
        >
          保存处理记录
        </button>
      </form>
      <p
        v-if="notice"
        class="notice"
        role="status"
      >
        {{ notice }}
      </p>
      <button
        v-if="unresolved"
        type="button"
        :disabled="saving || loading"
        @click="submit(unresolved)"
      >
        重试原保存请求
      </button>
      <section
        class="history"
        aria-label="处理历史"
      >
        <h4>处理历史</h4><p
          v-if="!view.history.length"
          class="meta"
        >
          尚无处理记录
        </p>
        <article
          v-for="entry in view.history"
          :key="entry.event_id"
        >
          <strong>{{ entry.kind === 'draft_saved' ? '保存回复草稿' : labels[entry.status] }}</strong>
          <p>{{ entry.note }}</p><small>{{ entry.actor_id }} · {{ date(entry.created_at) }}</small>
        </article>
      </section>
    </template>
  </section>
</template>

<style scoped>
.followup { margin-top: 20px; padding-top: 20px; border-top: 1px solid #e2e8f0; }
header { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
h3, h4 { margin: 0; font-size: 15px; } h4 { font-size: 14px; }
form, .history { margin-top: 16px; border: 1px solid #e2e8f0; border-radius: 8px; padding: 16px; }
label { display: flex; flex-direction: column; gap: 6px; margin: 12px 0; font-size: 13px; }
input, select, textarea, button { font: inherit; } input, select, textarea { border: 1px solid #cbd5e1; border-radius: 6px; padding: 8px; background: white; color: #334155; min-width: 0; } textarea { resize: vertical; }
button { border: 1px solid #d6dee8; border-radius: 7px; background: white; color: #334155; padding: 8px 12px; cursor: pointer; font-size: 13px; } button:disabled { opacity: .5; cursor: default; }
.meta, small { font-size: 12px; color: #64748b; overflow-wrap: anywhere; }
.warning { padding: 10px; color: #92400e; background: #fffbeb; font-size: 13px; } .notice { font-size: 13px; color: #334155; }
article { border-top: 1px solid #e2e8f0; padding-top: 12px; margin-top: 12px; font-size: 13px; overflow-wrap: anywhere; } article p { white-space: pre-wrap; }
@media (max-width: 760px) { form, .history { padding: 12px; } header { align-items: flex-start; } header button { flex-shrink: 0; } }
</style>
