<script setup lang="ts">
import { computed, inject, onMounted, ref, watch } from "vue";
import { RouterLink } from "vue-router";
import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";
import { useQuoteRequestScope } from "../costing-quotes/quote-request-scope";
import { blockingLabel } from "./customerLabels";
import { followupRecovery } from "./followupRecovery";
const props = defineProps<{
  allocationId: string;
  refreshRevision?: number;
  locked?: boolean;
  executionMode?: components["schemas"]["CustomerFollowupCommand"]["execution_mode"];
}>();
const emit = defineEmits<{
  forbidden: [];
  started: [value: components["schemas"]["CustomerEnrollmentView"]];
}>();
const client = inject<ReturnType<typeof createApiClient>>(
  "tradeos-api-client",
  apiClient,
);
const recovery = followupRecovery(client);
const options = ref<components["schemas"]["CustomerFollowupOptions"] | null>(
  null,
);
const campaignId = ref(""),
  contactId = ref(""),
  hypothesisId = ref("");
const loading = ref(false),
  busy = ref(false),
  message = ref(""),
  readError = ref(""),
  completed = ref(false);
const pending = computed(() => recovery.find(props.allocationId));
const startedEnrollment = ref<components["schemas"]["CustomerEnrollmentView"] | null>(null);
const executionMode = computed(() =>
  startedEnrollment.value
    ? startedEnrollment.value.execution_mode ?? "automatic"
    : pending.value
      ? pending.value.body.execution_mode ?? "automatic"
      : props.executionMode ?? "automatic",
);
const manual = computed(() => executionMode.value === "manual");
const gate = useQuoteRequestScope(
  client,
  () => [props.allocationId, props.refreshRevision, executionMode.value],
  reset,
);
function reset() {
  options.value = null;
  campaignId.value = "";
  contactId.value = "";
  hypothesisId.value = "";
  loading.value = false;
  busy.value = false;
  message.value = "";
  readError.value = "";
  completed.value = false;
  startedEnrollment.value = null;
}
const campaign = computed(() =>
  options.value?.campaigns.find((c) => c.campaign_id === campaignId.value),
);
const eligiblePoints = computed(
  () =>
    options.value?.contacts
      .flatMap((c) => c.contact_points)
      .filter(
        (p) =>
          p.contact_point.kind === "email" &&
          p.contact_point.verification === "verified" &&
          p.contact_point.verified_at &&
          campaign.value?.eligible_contact_point_ids.includes(
            p.contact_point.contact_point_id,
          ),
      ) ?? [],
);
const hypotheses = computed(
  () =>
    options.value?.hypotheses.filter(
      (h) =>
        campaign.value?.allowed_categories.includes(h.category) &&
        h.is_inference &&
        ["inferred", "contacting"].includes(h.status),
    ) ?? [],
);
const blockers = computed(() => [
  ...new Set([
    ...(options.value?.blocking_reasons ?? []),
    ...(campaign.value?.blocking_reasons ?? []),
  ]),
]);
const ready = computed(
  () =>
    options.value !== null &&
    !blockers.value.length &&
    campaign.value?.can_start === true &&
    eligiblePoints.value.some(
      (p) => p.contact_point.contact_point_id === contactId.value,
    ) &&
    hypotheses.value.some((h) => h.hypothesis_id === hypothesisId.value),
);
const canRecover = computed(
  () =>
    !blockers.value.some((b) =>
      ["not_customer_owner", "ownership_conflict"].includes(b),
    ),
);
watch(campaignId, () => {
  contactId.value = "";
  hypothesisId.value = "";
});
async function load() {
  if (!gate.hasIdentity.value) return;
  const operation = gate.begin("options");
  if (!operation?.valid()) return;
  options.value = null;
  campaignId.value = "";
  contactId.value = "";
  hypothesisId.value = "";
  loading.value = true;
  readError.value = "";
  try {
    const result = await client.GET(
      "/prospects/customers/{allocation_id}/follow-up",
      {
        params: {
          path: { allocation_id: props.allocationId },
          query: { execution_mode: executionMode.value },
        },
        signal: operation.signal,
        cache: "no-store",
      },
    );
    if (!operation.valid()) return;
    if (
      result.response.ok && result.data?.allocation_id === props.allocationId &&
      (result.data.execution_mode ?? "automatic") === executionMode.value
    )
      options.value = result.data;
    else {
      readError.value =
        result.response.status === 403
          ? "当前身份无权读取客户跟进"
          : "跟进条件读取失败，请刷新核对";
      if (result.response.status === 403) {
        recovery.forget(props.allocationId);
        gate.invalidate();
        reset();
        emit("forbidden");
      }
    }
  } catch {
    if (operation.valid()) readError.value = "跟进条件读取失败，请刷新核对";
  } finally {
    if (operation.valid()) loading.value = false;
  }
}
async function start() {
  if (
    busy.value ||
    !gate.hasIdentity.value ||
    (!pending.value && (!ready.value || completed.value)) ||
    (pending.value && !canRecover.value)
  )
    return;
  const operation = gate.begin("start");
  if (!operation?.valid()) return;
  const recovering = Boolean(pending.value);
  const request = pending.value ?? {
    allocationId: props.allocationId,
    key: globalThis.crypto.randomUUID(),
    body: {
      campaign_id: campaign.value!.campaign_id,
      campaign_version: campaign.value!.campaign_version,
      contact_point_id: contactId.value,
      source_hypothesis_id: hypothesisId.value,
      execution_mode: executionMode.value,
    },
  };
  try {
    recovery.save(request);
  } catch {
    message.value = "无法保留新的跟进请求，未提交。请先核对已有未决跟进，再重试。";
    return;
  }
  busy.value = true;
  message.value = "正在提交跟进…";
  try {
    const result = await client.POST(
      "/prospects/customers/{allocation_id}/follow-up",
      {
        params: {
          path: { allocation_id: request.allocationId },
          header: { "Idempotency-Key": request.key },
        },
        body: request.body,
        signal: operation.signal,
      },
    );
    if (!operation.valid()) return;
    if (result.response.ok && result.data &&
      (result.data.execution_mode ?? "automatic") === (request.body.execution_mode ?? "automatic")) {
      startedEnrollment.value = result.data;
      recovery.clear(request.allocationId, request.key);
      completed.value = true;
      message.value = manual.value
        ? "已准备手动联系，尚未发信。请进入单封发送工作台核对并发送。"
        : "已加入跟进，邮件发送状态请查看跟进记录";
      emit("started", result.data);
    } else if (
      [400, 403, 404, 409, 422, 429].includes(result.response.status)
    ) {
      if (recovering && result.response.status !== 403) {
        message.value =
          "原请求仍待核对，当前条件未允许确认。已保留原请求，请核对跟进记录后再次核对。";
        await load();
        return;
      }
      if (result.response.status === 403) recovery.forget(request.allocationId);
      else recovery.clear(request.allocationId, request.key);
      message.value =
        result.response.status === 403
          ? "当前身份无权跟进该客户"
          : blockingLabel(result.error?.code ?? "");
      if (result.response.status === 403) {
        gate.invalidate();
        reset();
        emit("forbidden");
      } else {
        await load();
      }
    } else message.value = "结果未知，保留原请求供核对；不会自动重发";
  } catch {
    if (operation.valid())
      message.value = "结果未知，保留原请求供核对；不会自动重发";
  } finally {
    if (operation.valid()) busy.value = false;
  }
}
onMounted(() => void load());
watch(() => props.executionMode, () => {
  if (pending.value || busy.value || completed.value) return;
  reset();
  void load();
});
watch(
  () => [props.allocationId, props.refreshRevision],
  () => {
    reset();
    void load();
  },
);
defineExpose({ start });
</script>
<template>
  <section
    class="followup-panel"
    :aria-label="manual ? '客户单封手动联系' : '客户自动跟进'"
  >
    <h3>{{ manual ? "单封手动联系" : "自动跟进" }}</h3>
    <p
      v-if="manual"
      class="meta"
    >
      逐封核对内容后发送，不会自动发送后续邮件。
    </p>
    <p
      v-if="loading"
      role="status"
    >
      正在核对跟进条件…
    </p>
    <p
      v-if="readError"
      role="alert"
    >
      {{ readError }}
    </p>
    <template v-if="options || pending">
      <ul
        v-if="options && blockers.length"
        class="blocking"
      >
        <li
          v-for="reason in blockers"
          :key="reason"
        >
          {{ blockingLabel(reason) }}
        </li>
      </ul>
      <p
        v-if="
          options && !options.campaigns.length && !blockers.includes('no_active_campaign')
        "
      >
        没有可用的开发任务，请管理员确认开发边界。
      </p>
      <template v-if="options && !pending">
        <label>已批准范围内的开发任务<select
          v-model="campaignId"
          data-followup-field="campaign"
          aria-label="开发任务"
          :disabled="busy || completed || locked"
        >
          <option value="">选择开发任务</option>
          <option
            v-for="c in options.campaigns"
            :key="c.campaign_id"
            :value="c.campaign_id"
          >
            {{ c.name }} · 版本 {{ c.campaign_version
            }}{{ c.can_start ? "" : " · 未就绪" }}
          </option>
        </select></label>
        <p
          v-if="campaign"
          class="meta"
        >
          国家 {{ campaign.markets.join("、") }} · 客户类型
          {{ campaign.target_entity_types.join("、") }} · 品类
          {{ campaign.allowed_categories.join("、") }}
        </p>
        <label>已验证联系方式<select
          v-model="contactId"
          data-followup-field="contact"
          aria-label="已验证联系方式"
          :disabled="busy || completed || locked"
        >
          <option value="">选择已验证邮箱</option>
          <option
            v-for="p in eligiblePoints"
            :key="p.contact_point.contact_point_id"
            :value="p.contact_point.contact_point_id"
          >
            {{ p.contact_point.value }}
          </option>
        </select></label>
        <label>来源需求假设（推断）<select
          v-model="hypothesisId"
          data-followup-field="hypothesis"
          aria-label="来源需求假设"
          :disabled="busy || completed || locked"
        >
          <option value="">选择该客户的假设</option>
          <option
            v-for="hyp in hypotheses"
            :key="hyp.hypothesis_id"
            :value="hyp.hypothesis_id"
          >
            {{ hyp.category }} · 推断 · {{ hyp.hypothesis_id }}
          </option>
        </select></label>
        <p class="meta">
          公开邮箱不能替代验证。加入跟进不表示邮件已发送；客户表达需求前，假设仍是推断。
        </p>
      </template>
      <p
        v-else
        role="status"
      >
        结果未知，原请求已保留，{{ manual ? "手动" : "自动" }}模式已锁定。刷新只读取状态；请明确核对原跟进请求。{{
          recovery.durable() ? "" : "浏览器存储不可用，刷新页面无法恢复原请求。"
        }}
      </p>
      <button
        :disabled="
          locked || busy || (pending ? !canRecover : !ready || completed)
        "
        @click="start"
      >
        {{ busy ? "提交中…" : pending ? "核对原跟进请求" : manual ? "准备单封手动联系" : "启动自动跟进" }}
      </button>
    </template>
    <p
      v-if="message"
      role="status"
    >
      {{ message }}
    </p>
    <RouterLink
      v-if="startedEnrollment && manual"
      :to="{ path: '/crm/outreach', query: { enrollment_id: startedEnrollment.enrollment_id } }"
    >
      进入单封发送工作台
    </RouterLink>
  </section>
</template>
<style scoped>
.followup-panel {
  display: grid;
  gap: 12px;
  min-width: 0;
  padding: 16px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--surface);
}
label {
  display: grid;
  gap: 6px;
  min-width: 0;
}
select {
  width: 100%;
  min-width: 0;
  padding: 8px;
  border: 1px solid var(--border);
  background: var(--surface);
  border-radius: 4px;
}
.blocking {
  padding-left: 20px;
  color: var(--warning);
  overflow-wrap: anywhere;
}
.followup-panel p {
  overflow-wrap: anywhere;
}
.followup-panel button {
  justify-self: start;
  max-width: 100%;
}
</style>
