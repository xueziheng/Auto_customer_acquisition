<script setup lang="ts">
import { inject, onMounted, ref } from "vue";
import { apiClient, createApiClient } from "../api/client";
import type { components } from "../api/api";
import { useQuoteRequestScope } from "../views/costing-quotes/quote-request-scope";
const client = inject<ReturnType<typeof createApiClient>>("tradeos-api-client", apiClient);
const emit = defineEmits<{ prepared: [enrollmentId: string] }>();
const options = ref<components["schemas"]["PrivateReplyTestOptions"] | null>(null);
const selected = ref("");
const confirmed = ref(false);
const busy = ref(false);
const notice = ref("");
function reset(): void {
  options.value = null; selected.value = ""; confirmed.value = false;
  busy.value = false; notice.value = "";
}
const gate = useQuoteRequestScope(client, () => [], reset);
onMounted(async () => {
  const op = gate.begin("options"); if (!op?.valid()) return;
  try {
    const result = await client.GET("/inbox/private-reply-test/options", { signal: op.signal });
    if (!op.valid()) return;
    if (result.response.status === 200) options.value = result.data ?? null;
  } catch { /* 可选测试入口不可用不影响普通撰写 */ }
});
async function prepare(): Promise<void> {
  const option = options.value?.options.find(item => item.campaign_id === selected.value);
  if (!option || !confirmed.value || busy.value) return;
  const op = gate.begin("prepare"); if (!op?.valid()) return;
  busy.value = true; notice.value = "";
  try {
    const result = await client.POST("/inbox/private-reply-test/prepare", { body: {
      campaign_id: option.campaign_id, sending_identity_id: option.sending_identity_id,
      confirm_controlled_recipient: true,
    } });
    if (!op.valid()) return;
    if (result.response.status === 200 && result.data) {
      emit("prepared", result.data.enrollment_id);
      notice.value = "测试记录已准备，请在下方撰写并手动发送。尚未发送邮件。";
    } else if ([401, 403, 404].includes(result.response.status)) {
      gate.invalidate(); reset();
    } else {
      notice.value = "测试记录未准备完成，请核对任务审批、邮箱与额度后刷新。";
    }
  } catch {
    if (op.valid()) notice.value = "准备结果待核对，请刷新；不会自动重试。";
  } finally { if (op.valid()) busy.value = false; }
}
</script>
<template>
  <section v-if="options?.enabled" class="safe-banner" aria-label="私有回信测试">
    <div>
      <strong>私有回信测试</strong>
      <p>1249583590@qq.com → 986647123@qq.com。测试回信由当前负责人处理，不计为正式采购需求或商机。</p>
      <template v-if="options.options.length">
        <label>单封开发任务
          <select v-model="selected" :disabled="busy" aria-label="测试开发任务">
            <option value="">请选择已批准的单封任务</option>
            <option v-for="item in options.options" :key="item.campaign_id" :value="item.campaign_id">{{ item.name }}</option>
          </select>
        </label>
        <label><input v-model="confirmed" :disabled="busy" type="checkbox" />我确认收件邮箱由本人控制，同意本次测试联系</label>
        <button type="button" :disabled="busy || !selected || !confirmed" @click="prepare">{{ busy ? "准备中…" : "准备私有测试邮件" }}</button>
      </template>
      <p v-else>暂无使用此QQ发件邮箱的已批准单封任务，请先由老板配置。</p>
      <p v-if="notice" role="status">{{ notice }}</p>
    </div>
  </section>
</template>
