<script setup lang="ts">
import { computed, inject, ref } from "vue";
import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";
import { useQuoteRequestScope } from "../costing-quotes/quote-request-scope";
const props = defineProps<{messageId: string; sourceKind: string}>();
const emit = defineEmits<{unauthorized: [status: number]}>();
const client = inject<ReturnType<typeof createApiClient>>("tradeos-api-client", apiClient);
const choices = ref<components["schemas"]["InquiryChoices"] | null>(null);
const contact = ref(""), scope = ref(""), product = ref(""), quote = ref("");
const busy = ref(false), error = ref(""), notice = ref(""), opened = ref(false);
const pending = ref<components["schemas"]["InquiryAdmissionRequest"] | null>(null);
const selectedScope = computed(() => choices.value?.scopes.find(s => `${s.campaign_id}:${s.campaign_version}` === scope.value));
function clear(): void { choices.value = null; contact.value = ""; scope.value = ""; product.value = ""; quote.value = ""; pending.value = null; busy.value = false; notice.value = ""; error.value = ""; }
const gate = useQuoteRequestScope(client, () => [], clear);
function deny(status: number): boolean { if ([401,403,404].includes(status)) { gate.invalidate(); clear(); emit("unauthorized", status); return true; } return false; }
async function load(): Promise<void> {
  opened.value = true;
  const op = gate.begin("choices"); if (!op?.valid()) return;
  busy.value = true; error.value = "";
  try {
    const result = await client.GET("/inbox/business-mail/{message_id}/ordinary-dialogue-options", {params: {path: {message_id: props.messageId}}, signal: op.signal});
    if (!op.valid()) return;
    if (deny(result.response.status)) return;
    if (!result.response.ok || !result.data) { error.value = "普通询盘准入尚不可用，请核对运行配置"; return; }
    choices.value = result.data;
  } catch { if (op.valid()) error.value = "准入选项读取失败，请重试"; }
  finally { if (op.valid()) busy.value = false; }
}
async function admit(): Promise<void> {
  if (busy.value || notice.value) return;
  const chosen = choices.value?.contacts.find(c => c.contact_point_id === contact.value);
  const selected = selectedScope.value;
  if (!pending.value) {
    if (!chosen || !selected || !selected.products.some(p => p.product_id === product.value) || !quote.value.trim()) { error.value = "请选择客户、获批范围和产品，并摘录客户原话"; return; }
    pending.value = {account_id: chosen.account_id, contact_point_id: chosen.contact_point_id,
      campaign_id: selected.campaign_id, campaign_version: selected.campaign_version, product_id: product.value, customer_quote: quote.value};
  }
  const op = gate.begin("admit"); if (!op?.valid()) return;
  busy.value = true; error.value = "";
  try {
    const result = await client.POST("/inbox/business-mail/{message_id}/ordinary-dialogue", {params: {path: {message_id: props.messageId}}, body: pending.value, signal: op.signal});
    if (!op.valid()) return;
    if (deny(result.response.status)) return;
    if (!result.response.ok || !result.data) { error.value = "准入条件未满足或结果待核对，请保留原选择重试"; return; }
    notice.value = "已纳入获批普通沟通范围，后续每轮仍核验当前权限与额度。未建立冷邮件序列。"; pending.value = null;
  } catch { if (op.valid()) error.value = "准入结果待核对，请重试同一请求"; }
  finally { if (op.valid()) busy.value = false; }
}
</script>
<template>
  <section>
    <button v-if="!opened" type="button" :disabled="sourceKind !== 'full_mime'" @click="load">纳入获批普通沟通</button>
    <template v-else>
      <h3>普通询盘准入</h3>
      <p>仅限已验证且有合法联系依据的现有联系人；负责人必须同时拥有此客户和本邮箱。价格、交期、认证等请求交人工处理。</p>
      <p v-if="choices && !choices.contacts.length">未找到本人负责的已验证联系人。请先在客户管理中完成真实关联、联系依据及邮箱验证。</p>
      <p v-if="choices && !choices.scopes.length">暂无委托给本人的获批普通沟通范围，请由老板在现有活动中配置并批准。</p>
      <fieldset v-if="choices?.contacts.length && choices.scopes.length" :disabled="busy || Boolean(pending) || Boolean(notice)">
        <label>现有客户<select v-model="contact" aria-label="询盘现有客户"><option value="">请选择</option><option v-for="c in choices.contacts" :key="c.contact_point_id" :value="c.contact_point_id">{{ c.account_name }}</option></select></label>
        <label>获批沟通范围<select v-model="scope" aria-label="询盘获批范围" @change="product = ''"><option value="">请选择</option><option v-for="s in choices.scopes" :key="s.campaign_id" :value="`${s.campaign_id}:${s.campaign_version}`">{{ s.name }}</option></select></label>
        <label>客户询问的产品<select v-model="product" aria-label="询盘产品"><option value="">请选择</option><option v-for="p in selectedScope?.products ?? []" :key="p.product_id" :value="p.product_id">{{ p.name }}</option></select></label>
        <label>客户原话摘录<textarea v-model="quote" maxlength="500" aria-label="客户原话摘录" placeholder="从本封邮件逐字摘录实际询问；不补写购买意向" /></label>
      </fieldset>
      <button v-if="!notice && choices?.contacts.length && choices.scopes.length" type="button" :disabled="busy" @click="admit">{{ pending ? "核对原准入请求" : "确认纳入普通沟通" }}</button>
      <p v-if="error" role="alert">{{ error }}</p><p v-if="notice" role="status">{{ notice }}</p>
      <p>当前普通线程发送仅支持已连接的 QQ 邮箱；投递结果未知时暂停核对，不自动重发。</p>
    </template>
  </section>
</template>
