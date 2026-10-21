<script setup lang="ts">
import { inject, onMounted, ref, watch } from "vue";
import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";
import { useQuoteRequestScope } from "../costing-quotes/quote-request-scope";

type View = components["schemas"]["CustomerVerificationView"];
type Result = components["schemas"]["CustomerVerificationResult"];
const props = defineProps<{ allocationId: string; refreshRevision?: number }>();
const emit = defineEmits<{ verified: [result: Result]; forbidden: [] }>();
const client = inject<ReturnType<typeof createApiClient>>("tradeos-api-client", apiClient);
const state = ref<View | null>(null), loading = ref(false), busy = ref(false), notice = ref("");
const pendingPoints = ref<Record<string, string | null>>({}), authorizationBlocked = ref(false);
const labels: Record<string, string> = { verified: "已验证", invalid: "无效", risky: "有风险", unverified: "未验证" };
const blockers: Record<string, string> = {
  provider_not_configured: "邮箱验证服务尚未配置", provider_configuration_changed: "验证服务配置已变更，请联系管理员核对",
  validation_not_run: "验证服务账户尚未通过检查", validation_failed: "验证服务账户检查未通过",
  validation_inconclusive: "验证服务账户检查结果待核对", runtime_not_composed: "邮箱验证服务尚未接入当前运行环境",
  verification_policy_not_configured: "联系方式处理依据检查尚未配置", verification_policy_denied: "联系方式处理依据不完整，请核对评估记录",
  verification_policy_unknown: "当前无法核对联系方式处理依据",
};
function reset() { state.value = null; loading.value = false; busy.value = false; notice.value = ""; pendingPoints.value = {}; authorizationBlocked.value = false; }
function erasurePending(option: View["contacts"][number]) { return option.contact.contact_point.verification_cost_note === "hunter.email_verifier.privacy_refused"; }
const gate = useQuoteRequestScope(client, () => [props.allocationId], reset);
function forbidden() { gate.invalidate(); reset(); authorizationBlocked.value = true; emit("forbidden"); notice.value = "当前身份无权验证该客户邮箱。"; }
function applyView(view: View) {
  state.value = view;
  for (const option of view.contacts) {
    const point = option.contact.contact_point, before = pendingPoints.value[point.contact_point_id];
    if (erasurePending(option) && point.contact_point_id in pendingPoints.value) {
      delete pendingPoints.value[point.contact_point_id];
      notice.value = "已记录隐私删除要求，可继续清除联系方式；不会再次调用邮箱验证服务。";
      continue;
    }
    if (!(point.contact_point_id in pendingPoints.value) || !point.verification_checked_at) continue;
    const observed = Date.parse(point.verification_checked_at);
    if (Number.isFinite(observed) && (before === null || observed > Date.parse(before ?? ""))) {
      delete pendingPoints.value[point.contact_point_id];
      notice.value = "已读取到新的验证观察，请核对当前邮箱状态。";
    }
  }
}
async function load() {
  if (!gate.hasIdentity.value || busy.value || authorizationBlocked.value) return;
  const op = gate.begin("read"); if (!op?.valid()) return;
  loading.value = true;
  try {
    const response = await client.GET("/prospects/customers/{allocation_id}/verification", {
      params: { path: { allocation_id: props.allocationId } }, signal: op.signal, cache: "no-store" });
    if (!op.valid()) return;
    if (response.response.ok && response.data?.allocation_id === props.allocationId) applyView(response.data);
    else if ([401, 403].includes(response.response.status)) forbidden();
    else { state.value = null; notice.value = "当前验证状态读取失败，请重新读取。"; }
  } catch { if (op.valid()) { state.value = null; notice.value = "当前验证状态读取失败，请重新读取。"; } }
  finally { if (op.valid()) loading.value = false; }
}
async function verify(pointId: string) {
  const selected = state.value?.contacts.find(option => option.can_verify && option.contact.contact_point.contact_point_id === pointId);
  if (busy.value || loading.value || authorizationBlocked.value || !selected || (pointId in pendingPoints.value && !erasurePending(selected))) return;
  const op = gate.begin("verify"); if (!op?.valid()) return;
  busy.value = true; notice.value = erasurePending(selected) ? "正在清除已提出隐私删除要求的联系方式…" : "正在核验所选邮箱…";
  pendingPoints.value[pointId] = selected.contact.contact_point.verification_checked_at ?? null;
  try {
    const response = await client.POST("/prospects/customers/{allocation_id}/verification", {
      params: { path: { allocation_id: props.allocationId } }, body: { contact_point_id: pointId }, signal: op.signal });
    if (!op.valid()) return;
    if (response.response.ok && response.data?.allocation_id === props.allocationId && response.data.contact_point_id === pointId) {
      delete pendingPoints.value[pointId];
      notice.value = response.data.privacy_erased ? "服务返回隐私删除要求，联系方式已按要求清除。"
        : `核验结果：${labels[response.data.result] ?? "结果未知"}${response.data.cache_hit ? "（复用有效期内的结果）" : ""}。`;
      emit("verified", response.data);
    } else if ([401, 403].includes(response.response.status)) forbidden();
    else if (response.response.status === 422 || (response.response.status === 400 && response.error?.code === "request_rejected")) {
      delete pendingPoints.value[pointId]; notice.value = response.error?.message ?? "本次核验被拒绝，请核对当前条件。";
    } else { notice.value = "验证结果待核对，请勿重复调用。请重新读取当前状态。"; }
  } catch { if (op.valid()) notice.value = "验证结果待核对，请勿重复调用。请重新读取当前状态。"; }
  finally { if (op.valid()) { busy.value = false; void load(); } }
}
watch(() => props.allocationId, () => { reset(); void load(); });
watch(() => props.refreshRevision, () => void load());
onMounted(() => void load());
</script>

<template>
  <section class="verification-card">
    <div class="verification-heading"><h2>验证客户邮箱</h2><button :disabled="busy || loading || authorizationBlocked" @click="load">重新读取</button></div>
    <p>选择已录入的邮箱核验可达性。新查询会使用已配置的验证额度，结果写入当前客户记录。</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <p v-if="loading">正在读取当前状态…</p>
    <template v-if="state">
      <p v-for="reason in state.blocking_reasons" :key="reason" class="verification-blocked">{{ blockers[reason] ?? "验证条件尚未满足，请联系管理员核对。" }}</p>
      <p v-if="!state.contacts.length">尚无可核验的已录入邮箱，请先准备客户资料与处理依据。</p>
      <div v-for="option in state.contacts" :key="option.contact.contact_point.contact_point_id" class="verification-contact">
        <div><strong>{{ option.contact.contact_point.value }}</strong><p>{{ labels[option.contact.contact_point.verification] ?? "状态未知" }}</p>
          <p v-if="option.contact.contact_point.verification_checked_at">上次核验：{{ new Date(option.contact.contact_point.verification_checked_at).toLocaleString() }}</p>
          <p v-if="erasurePending(option)">已记录隐私删除要求；继续清除不会再次调用邮箱验证服务。</p>
          <p v-for="reason in option.blocking_reasons.filter(value => !state!.blocking_reasons.includes(value))" :key="reason" class="verification-blocked">{{ blockers[reason] ?? "当前邮箱不可核验。" }}</p></div>
        <button data-action="verify" :disabled="busy || loading || !option.can_verify || (option.contact.contact_point.contact_point_id in pendingPoints && !erasurePending(option))" @click="verify(option.contact.contact_point.contact_point_id)">
          {{ erasurePending(option) ? "继续清除联系方式" : option.contact.contact_point.contact_point_id in pendingPoints ? "验证结果待核对" : "核验此邮箱" }}
        </button>
      </div>
    </template>
  </section>
</template>

<style scoped>
.verification-card{display:grid;gap:.75rem;padding:1.25rem;border:1px solid var(--color-border,#d8e0e6);border-radius:12px;background:var(--color-surface,#fff)}
.verification-heading,.verification-contact{display:flex;align-items:center;justify-content:space-between;gap:1rem}.verification-heading h2{margin:0}.verification-contact{border-top:1px solid #e5e7eb;padding-top:1rem}.verification-contact strong{overflow-wrap:anywhere}.verification-card p{margin:.25rem 0;color:#526170}.verification-blocked{color:#9a5c13!important}.verification-card button{flex-shrink:0;padding:.55rem .85rem}
@media(max-width:520px){.verification-card{padding:1rem}.verification-contact{align-items:stretch;flex-direction:column}.verification-heading{align-items:flex-start}.verification-contact button{align-self:flex-start}}
</style>
