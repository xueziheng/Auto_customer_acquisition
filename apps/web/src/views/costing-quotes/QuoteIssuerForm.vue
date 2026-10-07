<script setup lang="ts">
import { inject, reactive, ref } from "vue";
import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";
import { quoteError, useQuoteConfirmation } from "./quote-request-scope";
const emit = defineEmits<{ saved: [] }>();
const client = inject<ReturnType<typeof createApiClient>>("tradeos-api-client", apiClient);
const form = reactive<components["schemas"]["QuoteIssuerCreate"]>({ name: "", address: "", contact: "" });
const issuer = ref<components["schemas"]["QuoteIssuerPublicView"] | null>(null);
const { begin, hasIdentity, confirm, message, key, pending } = useQuoteConfirmation(client, () => [form.name, form.address, form.contact], () => {
  form.name = ""; form.address = ""; form.contact = ""; issuer.value = null;
});
async function save(): Promise<void> {
  const body = { ...form };
  await confirm(body, (id, signal) => client.POST("/costing-quotes/issuer", { params: { header: { "Idempotency-Key": id } }, body, signal }), (data) => { issuer.value = data; emit("saved"); });
}
async function read(): Promise<void> {
  const op = begin("read"); if (!op?.valid()) return;
  try {
    const result = await client.GET("/costing-quotes/issuer", { signal: op.signal });
    if (!op.valid()) return;
    if (result.response.ok) { issuer.value = result.data ?? null; message.value = issuer.value ? "已读取持久记录" : "本公司报价抬头尚未确认"; }
    else message.value = quoteError(result.response.status, result.error);
  } catch { if (op.valid()) message.value = "抬头读取失败"; }
}
</script>
<template>
  <section class="panel">
    <h2>本公司报价抬头</h2><p>客户可见内容请用英文。由老板确认，不能使用样例公司。</p>
    <div class="field-grid">
      <label>公司名称<input
        v-model="form.name"
        name="issuer-name"
      ></label><label>地址<input
        v-model="form.address"
        name="issuer-address"
      ></label><label>联系方式<input
        v-model="form.contact"
        name="issuer-contact"
      ></label>
    </div>
    <button
      :disabled="pending || !hasIdentity"
      @click="save"
    >
      老板确认抬头
    </button><button @click="read">
      核对已保存抬头
    </button>
    <p role="status">
      {{ message }} <code v-if="key">{{ key }}</code>
    </p>
    <dl v-if="issuer">
      <div><dt>抬头</dt><dd>{{ issuer.name }} · {{ issuer.address }} · {{ issuer.contact }}</dd></div><div><dt>确认来源</dt><dd>{{ issuer.issuer_id }} · {{ issuer.source_ref }} · {{ issuer.confirmed_by }} · {{ issuer.confirmed_at }}</dd></div>
    </dl>
  </section>
</template>
