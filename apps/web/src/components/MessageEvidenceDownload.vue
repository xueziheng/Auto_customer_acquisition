<script setup lang="ts">
/* global document */
import { inject, ref, watch } from "vue";
import { apiClient, createApiClient } from "../api/client";
import { useQuoteRequestScope } from "../views/costing-quotes/quote-request-scope";
const props = defineProps<{ messageId: string }>();
const emit = defineEmits<{ denied: [status: number] }>();
const client = inject<ReturnType<typeof createApiClient>>("tradeos-api-client", apiClient);
const busy = ref(false), feedback = ref("");
function reset(): void { busy.value = false; feedback.value = ""; }
const gate = useQuoteRequestScope(client, () => [props.messageId], reset);
watch(() => props.messageId, reset, { flush: "sync" });
async function download(): Promise<void> {
  if (busy.value) return;
  const op = gate.begin("evidence"); if (!op?.valid()) return;
  const messageId = props.messageId;
  busy.value = true; feedback.value = "";
  try {
    const result = await client.GET("/inbox/messages/{message_id}/evidence", {
      params: { path: { message_id: messageId } }, parseAs: "blob", cache: "no-store", signal: op.signal,
    });
    if (!op.valid()) return;
    if (result.response.status !== 200 || !result.data) {
      feedback.value = "原件不存在、无权限或暂不可用";
      if ([401,403,404].includes(result.response.status)) emit("denied", result.response.status);
      return;
    }
    const url = globalThis.URL.createObjectURL(result.data);
    try { const link = document.createElement("a"); link.href = url; link.download = "message.eml"; link.click(); }
    finally { globalThis.URL.revokeObjectURL(url); }
    feedback.value = "邮件原件下载已交给浏览器";
  } catch { if (op.valid()) feedback.value = "邮件原件暂不可用"; }
  finally { if (op.valid()) busy.value = false; }
}
</script>
<template>
  <div class="evidence-download">
    <button
      type="button"
      :disabled="busy"
      @click="download"
    >
      {{ busy ? "读取原件…" : "下载邮件原件" }}
    </button>
    <span
      v-if="feedback"
      role="status"
    >{{ feedback }}</span>
  </div>
</template>
<style scoped>
.evidence-download { display:flex; flex-wrap:wrap; gap:8px; overflow-wrap:anywhere; }
</style>
