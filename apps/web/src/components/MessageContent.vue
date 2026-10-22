<script setup lang="ts">
import { computed, inject, onMounted, ref, watch } from "vue";
import type { components } from "../api/api";
import { apiClient, createApiClient } from "../api/client";
import { useQuoteRequestScope } from "../views/costing-quotes/quote-request-scope";
const props = defineProps<{ messageId: string }>();
const emit = defineEmits<{ denied: []; loaded: [content: components["schemas"]["MessageContentView"]] }>();
const client = inject<ReturnType<typeof createApiClient>>("tradeos-api-client", apiClient);
const content = ref<components["schemas"]["MessageContentView"] | null>(null);
const readableBody = computed(() => content.value?.body_text?.replace(/\r\n/g, "\n").replace(/\n[\t \u00a0]*\n(?:[\t \u00a0]*\n)+/g, "\n\n").trim() ?? null);
const error = ref("");
function clear() { content.value = null; error.value = ""; }
const gate = useQuoteRequestScope(client, () => [props.messageId], clear);
async function load() {
  clear();
  const op = gate.begin("content"); if (!op?.valid()) return;
  try {
    const result = await client.GET("/inbox/messages/{message_id}/content", {
      params: { path: { message_id: props.messageId } }, cache: "no-store", signal: op.signal,
    });
    if (!op.valid()) return;
    if (!result.response.ok || !result.data) {
      error.value = "邮件内容暂不可用，请重试";
      if ([401,403].includes(result.response.status)) emit("denied");
      return;
    }
    content.value = result.data; emit("loaded", result.data);
  } catch { if (op.valid()) error.value = "邮件内容暂不可用，请重试"; }
}
watch(() => props.messageId, () => void load(), { flush: "sync" });
onMounted(() => void load());
</script>
<template>
  <section
    class="message-content"
    aria-label="邮件内容"
  >
    <template v-if="content">
      <h3>{{ content.subject || "（无主题）" }}</h3>
      <p>发件人：{{ content.sender }}<br>收件人：{{ content.recipients }}</p>
      <pre v-if="readableBody !== null">{{ readableBody }}</pre>
      <p v-else>
        此邮件没有纯文本正文，可下载邮件原件查看。
      </p>
      <p v-if="content.truncated">
        正文较长，完整内容请下载邮件原件。
      </p>
    </template>
    <p
      v-else-if="error"
      role="status"
    >
      {{ error }} <button
        type="button"
        @click="load"
      >
        重试读取
      </button>
    </p>
    <p v-else>
      正在读取邮件内容…
    </p>
  </section>
</template>
<style scoped>
.message-content { overflow-wrap: anywhere; }
.message-content h3 { margin: 0 0 12px; }
.message-content p { line-height: 1.7; }
.message-content pre { font-family: inherit; white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.7; padding: 16px; background: var(--surface); border-radius: 8px; }
</style>
