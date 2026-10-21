<script setup lang="ts">
import ProspectScreeningPanel from "../../components/ProspectScreeningPanel.vue";
import { computed, inject, onMounted, ref, watch } from "vue";
import { RouterLink, useRoute } from "vue-router";
import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";
import { useQuoteRequestScope } from "../costing-quotes/quote-request-scope";
import { useCustomerAccess } from "../../composables/useCustomerAccess";
import { codeLabel } from "../../components/displayLabels";
import CustomerFollowup from "./CustomerFollowup.vue";
import CustomerPreparation from "./CustomerPreparation.vue";
import CustomerVerification from "./CustomerVerification.vue";
import CustomerTransfer from "./CustomerTransfer.vue";
import { followupRecovery } from "./followupRecovery";
import { blockingLabel, enrollmentLabel, safeLink } from "./customerLabels";
const route = useRoute(),
  allocationId = computed(() => String(route.params.allocationId ?? ""));
const client = inject<ReturnType<typeof createApiClient>>(
  "tradeos-api-client",
  apiClient,
);
const { access } = useCustomerAccess(client);
const detail = ref<components["schemas"]["CustomerDetailView"] | null>(null);
const loading = ref(false),
  busy = ref(false),
  error = ref(""),
  notice = ref(""),
  revision = ref(0);
const gate = useQuoteRequestScope(client, () => [allocationId.value], reset);
function reset() {
  detail.value = null;
  loading.value = false;
  busy.value = false;
  error.value = "";
  notice.value = "";
}
function forbidden() {
  gate.invalidate();
  followupRecovery(client).forget(allocationId.value);
  reset();
  error.value = "当前身份无权读取或操作该客户。";
}
async function load() {
  if (!gate.hasIdentity.value || busy.value) return;
  const operation = gate.begin("detail");
  if (!operation?.valid()) return;
  detail.value = null;
  loading.value = true;
  error.value = "";
  revision.value++;
  try {
    const result = await client.GET("/prospects/customers/{allocation_id}", {
      params: { path: { allocation_id: allocationId.value } },
      signal: operation.signal,
      cache: "no-store",
    });
    if (!operation.valid()) return;
    if (result.response.ok && result.data?.allocation.id === allocationId.value)
      detail.value = result.data;
    else if (result.response.status === 403) forbidden();
    else error.value = "客户详情读取失败，请刷新重试。";
  } catch {
    if (operation.valid()) error.value = "客户详情读取失败，请刷新重试。";
  } finally {
    if (operation.valid()) loading.value = false;
  }
}
const ownerLabel = computed(() => !detail.value?.owner ? "尚未分配" : detail.value.owner === client.identitySnapshot().identity?.employeeId ? "我" : "其他同事");
const isActive = (state: string) => ["enrolled", "in_sequence"].includes(state);
async function stop(enrollmentId: string) {
  if (
    busy.value ||
    !detail.value?.can_stop ||
    !detail.value.enrollments.some(
      (e) => e.enrollment_id === enrollmentId && isActive(e.state),
    )
  )
    return;
  const operation = gate.begin("stop");
  if (!operation?.valid()) return;
  busy.value = true;
  notice.value = "";
  try {
    const result = await client.POST(
      "/prospects/customers/{allocation_id}/follow-up/{enrollment_id}/stop",
      {
        params: {
          path: {
            allocation_id: allocationId.value,
            enrollment_id: enrollmentId,
          },
        },
        signal: operation.signal,
      },
    );
    if (!operation.valid()) return;
    if (result.response.ok && result.data && detail.value) {
      detail.value.enrollments = detail.value.enrollments.map((e) =>
        e.enrollment_id === enrollmentId ? result.data! : e,
      );
      notice.value = "跟进已停止。";
    } else if (result.response.status === 403) forbidden();
    else notice.value = "停止结果待核对，请刷新查看当前状态；不会自动重发。";
  } catch {
    if (operation.valid())
      notice.value = "停止结果待核对，请刷新查看当前状态；不会自动重发。";
  } finally {
    if (operation.valid()) busy.value = false;
  }
}
async function linkAccount() {
  if (
    busy.value ||
    !access.value?.can_assign ||
    !detail.value ||
    detail.value.allocation.account_id ||
    !detail.value.allocation.employee_id
  )
    return;
  const operation = gate.begin("link");
  if (!operation?.valid()) return;
  busy.value = true;
  notice.value = "";
  const body = {
    allocation_ids: [detail.value.allocation.id],
    employee_id: detail.value.allocation.employee_id,
  };
  try {
    const { response } = await client.POST("/prospects/inventory/assignments", {
      body,
      signal: operation.signal,
    });
    if (!operation.valid()) return;
    if (response.ok) {
      busy.value = false;
      await load();
      if (operation.valid()) notice.value =
        "已请求关联原负责人的客户档案；联系方式仍按真实验证状态展示。";
    } else if (response.status === 403) forbidden();
    else
      notice.value =
        "关联未确认，请刷新核对原负责人和档案；重复关联沿用同一负责人。";
  } catch {
    if (operation.valid())
      notice.value = "关联结果待核对，请刷新后显式重试同负责人关联。";
  } finally {
    if (operation.valid()) busy.value = false;
  }
}
function started(value: components["schemas"]["CustomerEnrollmentView"]) {
  if (detail.value)
    detail.value.enrollments = [
      ...detail.value.enrollments.filter(
        (e) => e.enrollment_id !== value.enrollment_id,
      ),
      value,
    ];
}
watch(allocationId, () => {
  reset();
  void load();
});
onMounted(() => void load());
</script>
<template>
  <div class="shell customer-detail-shell">
    <div class="page-head">
      <RouterLink to="/customers">
        返回我的客户
      </RouterLink>
      <h1>客户详情</h1>
      <button
        :disabled="loading || busy"
        @click="load"
      >
        刷新详情
      </button>
    </div>
    <p
      v-if="loading"
      role="status"
    >
      正在读取客户详情…
    </p>
    <p
      v-if="error"
      role="alert"
    >
      {{ error }}
    </p>
    <template v-if="detail">
      <section
        v-if="detail.preparation"
        class="detail-card"
        aria-label="自动准备进度"
      >
        <h2>自动准备进度</h2>
        <p>{{ ({queued: "等待处理", processing: "处理中", blocked: "已阻断，等待核对", enrolled: "已验证并入组"})[detail.preparation.state] }} · {{ ({authorize: "核验批准范围", assign: "关联客户归属", evidence: "整理证据与产品适配", verify: "验证邮箱", enroll: "核验入组条件", completed: "准备完成"})[detail.preparation.stage] }}</p>
        <p>开发范围第 {{ detail.preparation.campaign_version }} 版。入组不代表已经发送；发送仍受当前批准、归属、国家政策及发信开关控制。</p>
        <p
          v-if="detail.preparation.blocked_reason"
          role="status"
        >
          {{ blockingLabel(detail.preparation.blocked_reason) }}
        </p>
        <div class="preparation-links">
          <RouterLink
            v-if="access?.can_assign"
            to="/campaigns"
          >
            核对批准范围
          </RouterLink>
          <RouterLink to="/knowledge">
            核对企业资料
          </RouterLink>
        </div>
      </section>
      <section class="detail-card">
        <h2>{{ detail.allocation.candidate.name }}</h2>
        <p>
          {{ detail.allocation.candidate.country }} ·
          {{ detail.allocation.candidate.canonical_domain }}
        </p>
        <p>负责人：{{ ownerLabel }}</p>
        <ul
          v-if="detail.blocking_reasons.length"
          class="blocking"
        >
          <li
            v-for="reason in detail.blocking_reasons"
            :key="reason"
          >
            {{ blockingLabel(reason) }}
          </li>
        </ul>
        <button
          v-if="
            access?.can_assign &&
              !detail.allocation.account_id &&
              detail.allocation.employee_id
          "
          :disabled="busy"
          @click="linkAccount"
        >
          关联客户档案
        </button>
        <p v-if="detail.account">
          客户类型：{{ codeLabel(detail.account.entity_type, "待确认") }} ·
          {{ detail.account.industry ?? "行业待确认" }}
        </p>
        <p>
          公开邮箱：{{
            detail.allocation.candidate.public_emails.join("；") || "尚未发现"
          }}<span class="unverified">（公开来源记录；实际可达性与入组状态见联系人验证状态）</span>
        </p>
        <ProspectScreeningPanel v-if="access?.can_assign && detail.account" :account-id="detail.account.account_id" />
        <details>
          <summary>公开来源与证据</summary>
          <p>{{ detail.allocation.candidate.evidence_text }}</p>
          <a
            :href="safeLink(detail.allocation.candidate.source_url)"
            target="_blank"
            rel="noopener noreferrer"
          >查看原始来源</a>
          <p>
            观察时间：{{ detail.allocation.candidate.observed_at }} ·
            {{
              new Date(detail.allocation.candidate.expires_at).getTime() <=
                Date.now()
                ? "资料已过期，需复核"
                : "公开资料，需求尚未验证"
            }}
          </p>
        </details>
      </section>
      <section class="detail-card">
        <h2>联系人验证状态</h2>
        <p v-if="!detail.contacts.length">
          尚无联系人；公开邮箱不计为已验证。
        </p>
        <article
          v-for="contact in detail.contacts"
          :key="contact.contact.contact_id"
        >
          <strong>{{ contact.contact.full_name ?? "职务联系人" }}</strong>
          <div
            v-for="point in contact.contact_points"
            :key="point.contact_point.contact_point_id"
            class="contact-facts"
          >
            <p>
              {{ point.contact_point.value }} ·
              {{
                point.contact_point.verification === "verified" &&
                  point.contact_point.verified_at
                  ? "已验证"
                  : "未通过验证，禁止入组"
              }}
            </p>
            <p>
              验证时间：{{ point.contact_point.verified_at ?? "尚未验证" }} ·
              {{
                point.contact_point.verification_provider ?? "验证来源待确认"
              }}
            </p>
            <p>
              法律依据：{{ codeLabel(point.legal_basis) }} ·
              {{ point.legal_basis_source }}
            </p>
            <a
              v-if="safeLink(point.source_url ?? '')"
              :href="safeLink(point.source_url!)"
              target="_blank"
              rel="noopener noreferrer"
            >查看联系方式来源</a>
          </div>
        </article>
      </section>
      <CustomerTransfer
        v-if="access?.can_assign && detail.allocation.account_id && detail.allocation.employee_id"
        :allocation="detail.allocation"
        :active="detail.enrollments.some(e => isActive(e.state))"
        @forbidden="forbidden"
        @transferred="load"
      />
      <CustomerPreparation
        :allocation="detail.allocation"
        :refresh-revision="revision"
        @saved="load"
        @forbidden="forbidden"
      />
      <CustomerVerification
        v-if="detail.can_stop"
        :allocation-id="allocationId"
        :refresh-revision="revision"
        @verified="load"
        @forbidden="forbidden"
      />
      <CustomerFollowup
        :allocation-id="allocationId"
        execution-mode="manual"
        :refresh-revision="revision"
        @forbidden="forbidden"
        @started="started"
      />
      <section class="detail-card">
        <h2>跟进状态</h2>
        <p v-if="!detail.enrollments.length">
          尚未加入跟进
        </p>
        <article
          v-for="e in detail.enrollments"
          :key="e.enrollment_id"
        >
          <p>
            {{ enrollmentLabel(e.state) }} · 开发任务版本
            {{ e.campaign_version }} · 当前步骤 {{ e.current_step }}
          </p>
          <p v-if="e.next_send_at">
            下次计划：{{ e.next_send_at }}
          </p>
          <button
            v-if="detail.can_stop && isActive(e.state)"
            :disabled="busy"
            @click="stop(e.enrollment_id)"
          >
            停止跟进
          </button>
        </article>
      </section>
      <nav
        class="context-links"
        aria-label="客户相关操作"
      >
        <RouterLink to="/inbox">
          查看回复
        </RouterLink><RouterLink to="/crm/handoffs">
          人工接管
        </RouterLink><RouterLink
          v-if="detail.account"
          :to="{
            path: '/crm/opportunities',
            query: { account_id: detail.account.account_id },
          }"
        >
          商机工作台
        </RouterLink><RouterLink
          v-if="detail.account"
          :to="{
            path: '/demand',
            query: { account_id: detail.account.account_id },
          }"
        >
          需求证据工作台
        </RouterLink><RouterLink
          v-if="access?.can_assign"
          to="/campaigns"
        >
          配置开发任务
        </RouterLink>
      </nav>
    </template>
    <p
      v-if="notice"
      role="status"
    >
      {{ notice }}
    </p>
  </div>
</template>
<style scoped>
.preparation-links { display: flex; flex-wrap: wrap; gap: 0.75rem; }
.customer-detail-shell {
  overflow: auto;
  min-width: 0;
}
.detail-card {
  padding: 20px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--surface);
  display: grid;
  gap: 12px;
  min-width: 0;
  overflow-wrap: anywhere;
}
.detail-card h2 {
  font-size: 18px;
}
.detail-card button {
  justify-self: start;
}
.blocking {
  padding-left: 20px;
  color: var(--warning);
}
.unverified {
  color: var(--warning);
}
.contact-facts {
  padding: 12px;
  background: var(--canvas);
  margin-top: 8px;
}
.context-links {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
}
details p {
  margin-top: 10px;
}
article {
  min-width: 0;
}
</style>
