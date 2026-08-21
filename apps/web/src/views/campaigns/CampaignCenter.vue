<script setup lang="ts">
/* global window */
import { computed, inject, onMounted, reactive, ref } from "vue";

import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";

type ApiClient = ReturnType<typeof createApiClient>;
type Campaign = components["schemas"]["CampaignView"];
type CampaignBody = components["schemas"]["CampaignBoundaryBody"];
type Enrollment = components["schemas"]["EnrollmentView"];
type Identity = components["schemas"]["IdentityView"];

const client = inject<ApiClient>("tradeos-api-client", apiClient);
const campaigns = ref<Campaign[]>([]);
const identities = ref<Identity[]>([]);
const enrollments = ref<Enrollment[]>([]);
const selectedId = ref<string | null>(null);
const loading = ref(true);
const detailLoading = ref(false);
const actionBusy = ref(false);
const error = ref<string | null>(null);
const actionMessage = ref("所有边界修改都会形成新版本并重新审批。");
const editorOpen = ref(false);
const editingCampaignId = ref<string | null>(null);
const marketsText = ref("");
const entityTypesText = ref("");
const categoriesText = ref("");
const triggersText = ref("quantity_provided, sample_requested, quote_requested");
const form = reactive<CampaignBody>({
  name: "",
  markets: [],
  target_entity_types: [],
  allowed_categories: [],
  sender_identity_ids: [],
  steps: [
    { step_number: 1, intent: "discovery", wait_days: 0 },
    { step_number: 2, intent: "presentation", wait_days: 3 },
    { step_number: 3, intent: "presentation", wait_days: 5 },
  ],
  daily_new_contact_limit: 20,
  daily_total_message_limit: 50,
  handoff_triggers: [],
  stop_on_reply: true,
});

const selected = computed(
  () => campaigns.value.find((item) => item.campaign_id === selectedId.value) ?? null,
);

const stateLabels: Record<string, string> = {
  draft: "草稿",
  pending_approval: "待审批",
  active: "运行中",
  paused: "已暂停",
  completed: "已完成",
  cancelled: "已取消",
};

const enrollmentSummary = computed(() => {
  const result: Record<string, number> = {};
  for (const item of enrollments.value) result[item.state] = (result[item.state] ?? 0) + 1;
  return result;
});

function requestHeaders(): Record<string, string> {
  const headers: Record<string, string> = {};
  const tenantId = import.meta.env.VITE_TENANT_ID;
  const employeeId = import.meta.env.VITE_EMPLOYEE_ID;
  if (tenantId) headers["X-Tenant-Id"] = tenantId;
  if (employeeId) headers["X-Employee-Id"] = employeeId;
  return headers;
}

function safeError(status: number): string {
  if (status === 403) return "当前身份没有执行此操作的权限";
  if (status === 409) return "Campaign 状态已变化，请刷新后重试";
  if (status === 503) return "服务暂不可用，请稍后重试";
  return "请求未完成，请检查边界内容";
}

function values(text: string): string[] {
  return Array.from(new Set(text.split(",").map((item) => item.trim()).filter(Boolean)));
}

function syncFormCollections(): void {
  form.markets = values(marketsText.value);
  form.target_entity_types = values(entityTypesText.value);
  form.allowed_categories = values(categoriesText.value);
  form.handoff_triggers = values(triggersText.value);
  form.steps = form.steps.map((step, index) => ({ ...step, step_number: index + 1 }));
}

function newCampaign(): void {
  editingCampaignId.value = null;
  form.name = "";
  marketsText.value = "";
  entityTypesText.value = "importer, manufacturer, distributor";
  categoriesText.value = "";
  triggersText.value = "quantity_provided, sample_requested, quote_requested";
  form.sender_identity_ids = [];
  form.daily_new_contact_limit = 20;
  form.daily_total_message_limit = 50;
  form.stop_on_reply = true;
  form.steps = [
    { step_number: 1, intent: "discovery", wait_days: 0 },
    { step_number: 2, intent: "presentation", wait_days: 3 },
    { step_number: 3, intent: "presentation", wait_days: 5 },
  ];
  editorOpen.value = true;
}

function reviseCampaign(campaign: Campaign): void {
  editingCampaignId.value = campaign.campaign_id;
  form.name = campaign.name;
  marketsText.value = campaign.boundary.markets.join(", ");
  entityTypesText.value = campaign.boundary.target_entity_types.join(", ");
  categoriesText.value = campaign.boundary.allowed_categories.join(", ");
  triggersText.value = campaign.boundary.handoff_triggers.join(", ");
  form.sender_identity_ids = [...campaign.boundary.sender_identity_ids];
  form.steps = campaign.boundary.steps.map((step) => ({
    step_number: step.step_number,
    intent: step.intent,
    wait_days: step.wait_days,
  }));
  form.daily_new_contact_limit = campaign.boundary.daily_new_contact_limit;
  form.daily_total_message_limit = campaign.boundary.daily_total_message_limit;
  form.stop_on_reply = campaign.boundary.stop_on_reply;
  editorOpen.value = true;
}

function addStep(): void {
  if (form.steps.length >= 5) return;
  form.steps.push({ step_number: form.steps.length + 1, intent: "presentation", wait_days: 3 });
}

function removeStep(index: number): void {
  if (form.steps.length <= 1) return;
  form.steps.splice(index, 1);
  syncFormCollections();
}

async function loadCampaigns(): Promise<void> {
  loading.value = true;
  error.value = null;
  try {
    const result = await client.GET("/crm/campaigns", {
      params: { query: { limit: 100 } },
      headers: requestHeaders(),
    });
    if (result.response.status !== 200 || !result.data) {
      error.value = safeError(result.response.status);
      return;
    }
    campaigns.value = result.data;
    const retained = selectedId.value && result.data.some((item) => item.campaign_id === selectedId.value)
      ? selectedId.value
      : result.data[0]?.campaign_id ?? null;
    selectedId.value = retained;
    if (retained) await loadEnrollments(retained);
    else enrollments.value = [];
  } catch {
    error.value = "无法连接服务，请稍后重试";
  } finally {
    loading.value = false;
  }
}

async function loadIdentities(): Promise<void> {
  try {
    const result = await client.GET("/crm/sending-identities", {
      params: { query: { limit: 100 } },
      headers: requestHeaders(),
    });
    if (result.response.status === 200 && result.data) identities.value = result.data;
  } catch {
    identities.value = [];
  }
}

async function loadEnrollments(campaignId: string): Promise<void> {
  selectedId.value = campaignId;
  detailLoading.value = true;
  try {
    const result = await client.GET("/crm/campaigns/{campaign_id}/enrollments", {
      params: { path: { campaign_id: campaignId }, query: { limit: 200 } },
      headers: requestHeaders(),
    });
    enrollments.value = result.response.status === 200 && result.data ? result.data : [];
  } finally {
    detailLoading.value = false;
  }
}

async function saveBoundary(): Promise<void> {
  if (actionBusy.value) return;
  syncFormCollections();
  if (!form.name.trim() || !form.markets.length || !form.allowed_categories.length || !form.sender_identity_ids.length) {
    error.value = "请填写名称、市场、品类并选择至少一个冷开发发件身份";
    return;
  }
  actionBusy.value = true;
  error.value = null;
  try {
    if (editingCampaignId.value) {
      const result = await client.POST("/crm/campaigns/{campaign_id}/revise", {
        params: { path: { campaign_id: editingCampaignId.value } },
        body: { ...form },
        headers: requestHeaders(),
      });
      if (result.response.status === 200 && result.data) {
        actionMessage.value = `新版本已提交审批：${result.data.approval_id}`;
        editorOpen.value = false;
        await loadCampaigns();
        return;
      }
      error.value = safeError(result.response.status);
    } else {
      const result = await client.POST("/crm/campaigns", {
        body: { ...form },
        headers: requestHeaders(),
      });
      if (result.response.status === 200 && result.data) {
        actionMessage.value = "Campaign 草稿已创建；提交审批前不会发送。";
        editorOpen.value = false;
        selectedId.value = result.data.campaign_id;
        await loadCampaigns();
        return;
      }
      error.value = safeError(result.response.status);
    }
  } catch {
    error.value = "无法连接服务，请稍后重试";
  } finally {
    actionBusy.value = false;
  }
}

async function transition(action: "submit" | "activate" | "pause" | "cancel"): Promise<void> {
  if (!selected.value || actionBusy.value) return;
  actionBusy.value = true;
  error.value = null;
  const campaignId = selected.value.campaign_id;
  try {
    if (action === "pause") {
      const reason = window.prompt("填写暂停原因（暂停只阻止新发送，回复仍继续处理）");
      if (!reason?.trim()) return;
      const result = await client.POST("/crm/campaigns/{campaign_id}/pause", {
        params: { path: { campaign_id: campaignId } },
        body: { reason: reason.trim() },
        headers: requestHeaders(),
      });
      if (result.response.status !== 200) throw new Error(safeError(result.response.status));
      actionMessage.value = "Campaign 已暂停新发送；入站回复处理保持运行。";
    } else if (action === "submit") {
      const result = await client.POST("/crm/campaigns/{campaign_id}/submit", {
        params: { path: { campaign_id: campaignId } },
        headers: requestHeaders(),
      });
      if (result.response.status !== 200 || !result.data) throw new Error(safeError(result.response.status));
      actionMessage.value = `已提交审批：${result.data.approval_id}`;
    } else if (action === "activate") {
      const result = await client.POST("/crm/campaigns/{campaign_id}/activate", {
        params: { path: { campaign_id: campaignId } },
        headers: requestHeaders(),
      });
      if (result.response.status !== 200) throw new Error(safeError(result.response.status));
      actionMessage.value = "Campaign 精确版本已激活。";
    } else {
      const result = await client.POST("/crm/campaigns/{campaign_id}/cancel", {
        params: { path: { campaign_id: campaignId } },
        headers: requestHeaders(),
      });
      if (result.response.status !== 200) throw new Error(safeError(result.response.status));
      actionMessage.value = "Campaign 已取消，不能恢复。";
    }
    await loadCampaigns();
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : "操作未完成";
  } finally {
    actionBusy.value = false;
  }
}

onMounted(() => {
  void Promise.all([loadCampaigns(), loadIdentities()]);
});
</script>

<template>
  <div class="shell campaign-shell">
    <div class="page-head campaign-head">
      <div>
        <p class="eyebrow">
          AUTHORIZED OUTREACH
        </p><h1>Campaign Center</h1>
      </div>
      <button
        class="btn-primary"
        type="button"
        @click="newCampaign"
      >
        新建 Campaign
      </button>
    </div>

    <div class="principle">
      <strong>Campaign 是授权书，不是发送队列。</strong><span>边界内自主运行；改边界 = 新版本 + 重新审批。包含价格或承诺的内容仍逐次审批。</span>
    </div>
    <div
      v-if="error"
      class="safe-banner danger"
      role="alert"
    >
      {{ error }}
    </div>
    <div
      class="action-note"
      role="status"
    >
      {{ actionMessage }}
    </div>

    <section
      v-if="editorOpen"
      class="boundary-editor"
      aria-label="Campaign 边界编辑器"
    >
      <header>
        <div><h2>{{ editingCampaignId ? "修订 Campaign 边界" : "创建 Campaign 草稿" }}</h2><p>{{ editingCampaignId ? "保存会追加新版本并立即进入重新审批。" : "草稿不会触发发送。" }}</p></div><button
          type="button"
          @click="editorOpen = false"
        >
          关闭
        </button>
      </header>
      <form @submit.prevent="saveBoundary">
        <label>Campaign 名称<input
          v-model="form.name"
          autocomplete="off"
        ></label>
        <label>目标市场<input
          v-model="marketsText"
          placeholder="Germany, Netherlands"
        ><small>英文逗号分隔</small></label>
        <label>企业类型<input
          v-model="entityTypesText"
          placeholder="importer, manufacturer"
        ></label>
        <label>允许品类<input
          v-model="categoriesText"
          placeholder="hardware, packaging"
        ></label>
        <fieldset class="sender-picker">
          <legend>冷开发发件身份</legend><label
            v-for="identity in identities"
            :key="identity.identity_id"
          ><input
            v-model="form.sender_identity_ids"
            type="checkbox"
            :value="identity.identity_id"
            :disabled="!identity.usable_for_cold_outreach"
          ><span>{{ identity.address }}</span><small>{{ identity.state }} · 今日余量 {{ identity.remaining_today }}</small></label><p v-if="!identities.length">
            没有可用发件身份。请先完成独立域名认证与预热。
          </p>
        </fieldset>
        <div class="limit-row">
          <label>每日新联系人上限<input
            v-model.number="form.daily_new_contact_limit"
            type="number"
            min="1"
          ></label><label>每日总消息上限<input
            v-model.number="form.daily_total_message_limit"
            type="number"
            min="1"
          ></label>
        </div>
        <fieldset class="sequence">
          <legend>邮件序列</legend><article
            v-for="(step, index) in form.steps"
            :key="index"
          >
            <strong>第 {{ index + 1 }} 步</strong><select
              v-model="step.intent"
              :disabled="index === 0"
            >
              <option value="discovery">
                需求发现
              </option><option value="presentation">
                能力介绍
              </option><option value="follow_up">
                跟进
              </option>
            </select><label>等待天数<input
              v-model.number="step.wait_days"
              type="number"
              min="0"
              max="90"
            ></label><button
              type="button"
              :disabled="form.steps.length === 1"
              @click="removeStep(index)"
            >
              移除
            </button>
          </article><button
            type="button"
            :disabled="form.steps.length >= 5"
            @click="addStep"
          >
            + 增加步骤
          </button><small>第一步固定为需求发现；最多 5 步。</small>
        </fieldset>
        <label class="wide">接管触发条件<input v-model="triggersText"></label>
        <label class="stop-rule"><input
          v-model="form.stop_on_reply"
          type="checkbox"
          disabled
        ><span>收到回复立即停序列（Phase 1 强制开启，发送前会再次检查）</span></label>
        <div class="editor-actions">
          <button
            type="button"
            @click="editorOpen = false"
          >
            取消
          </button><button
            class="btn-primary"
            type="submit"
            :disabled="actionBusy"
          >
            {{ actionBusy ? "保存中…" : editingCampaignId ? "保存新版本并提交审批" : "创建草稿" }}
          </button>
        </div>
      </form>
    </section>

    <section class="campaign-layout">
      <aside class="campaign-list">
        <header>
          <h2>Campaign</h2><button
            type="button"
            @click="loadCampaigns"
          >
            刷新
          </button>
        </header>
        <div
          v-if="loading"
          class="empty"
        >
          加载中…
        </div>
        <button
          v-for="campaign in campaigns"
          v-else
          :key="campaign.campaign_id"
          type="button"
          class="campaign-row"
          :class="{ active: selectedId === campaign.campaign_id }"
          @click="loadEnrollments(campaign.campaign_id)"
        >
          <span><strong>{{ campaign.name }}</strong><small>v{{ campaign.version }} · {{ campaign.boundary.markets.join(" / ") }}</small></span><em :class="`state-${campaign.state}`">{{ stateLabels[campaign.state] }}</em>
        </button>
        <div
          v-if="!loading && !campaigns.length"
          class="empty"
        >
          暂无 Campaign
        </div>
      </aside>

      <main
        v-if="selected"
        class="campaign-detail"
      >
        <header>
          <div><span class="version">VERSION {{ selected.version }}</span><h2>{{ selected.name }}</h2><p>{{ selected.campaign_id }}</p></div><div class="detail-actions">
            <button
              v-if="selected.state === 'draft'"
              type="button"
              :disabled="actionBusy"
              @click="transition('submit')"
            >
              提交审批
            </button><button
              v-if="selected.state === 'pending_approval' || selected.state === 'paused'"
              class="btn-primary"
              type="button"
              :disabled="actionBusy"
              @click="transition('activate')"
            >
              激活已批准版本
            </button><button
              v-if="selected.state === 'active'"
              type="button"
              :disabled="actionBusy"
              @click="transition('pause')"
            >
              暂停
            </button><button
              v-if="!['completed', 'cancelled'].includes(selected.state)"
              type="button"
              :disabled="actionBusy"
              @click="reviseCampaign(selected)"
            >
              修订边界
            </button><button
              v-if="!['completed', 'cancelled'].includes(selected.state)"
              class="danger-button"
              type="button"
              :disabled="actionBusy"
              @click="transition('cancel')"
            >
              取消
            </button>
          </div>
        </header>
        <div class="quota-grid">
          <article>
            <span>今日新联系人占用</span><strong>{{ selected.today_new_contacts_reserved }} / {{ selected.boundary.daily_new_contact_limit }}</strong><progress
              :value="selected.today_new_contacts_reserved"
              :max="selected.boundary.daily_new_contact_limit"
            />
          </article><article>
            <span>今日消息占用</span><strong>{{ selected.today_messages_reserved }} / {{ selected.boundary.daily_total_message_limit }}</strong><progress
              :value="selected.today_messages_reserved"
              :max="selected.boundary.daily_total_message_limit"
            />
          </article>
        </div>
        <div class="boundary-grid">
          <article><h3>授权范围</h3><dl><div><dt>市场</dt><dd>{{ selected.boundary.markets.join("、") }}</dd></div><div><dt>企业类型</dt><dd>{{ selected.boundary.target_entity_types.join("、") }}</dd></div><div><dt>允许品类</dt><dd>{{ selected.boundary.allowed_categories.join("、") }}</dd></div><div><dt>接管触发</dt><dd>{{ selected.boundary.handoff_triggers.join("、") || "未设置" }}</dd></div></dl></article><article>
            <h3>发送身份</h3><ul>
              <li
                v-for="identityId in selected.boundary.sender_identity_ids"
                :key="identityId"
              >
                <strong>{{ identities.find((item) => item.identity_id === identityId)?.address ?? identityId }}</strong><span>{{ identities.find((item) => item.identity_id === identityId)?.state ?? "状态需刷新" }}</span>
              </li>
            </ul>
          </article>
        </div>
        <article class="sequence-view">
          <h3>获批序列</h3><ol>
            <li
              v-for="step in selected.boundary.steps"
              :key="step.step_number"
            >
              <span>{{ step.step_number }}</span><div><strong>{{ step.intent === "discovery" ? "需求发现" : step.intent === "follow_up" ? "跟进" : "能力介绍" }}</strong><small>{{ step.step_number === 1 ? "立即开始" : `等待 ${step.wait_days} 天` }}</small></div>
            </li>
          </ol><p>Stop on reply：{{ selected.boundary.stop_on_reply ? "开启" : "关闭" }}</p>
        </article>
        <article class="enrollment-panel">
          <header><h3>入组进度</h3><span>{{ enrollments.length }} 个已验证联系人</span></header><div
            v-if="detailLoading"
            class="empty"
          >
            加载中…
          </div><div
            v-else
            class="enrollment-summary"
          >
            <span
              v-for="(count, state) in enrollmentSummary"
              :key="state"
            ><strong>{{ count }}</strong>{{ state }}</span>
          </div><table v-if="enrollments.length">
            <thead><tr><th>Enrollment</th><th>状态</th><th>当前步骤</th><th>下次发送</th><th>发件身份</th></tr></thead><tbody>
              <tr
                v-for="item in enrollments"
                :key="item.enrollment_id"
              >
                <td>{{ item.enrollment_id }}</td><td>{{ item.state }}</td><td>{{ item.current_step }}</td><td>{{ item.next_send_at ? new Date(item.next_send_at).toLocaleString("zh-CN", { hour12: false }) : "—" }}</td><td>{{ item.sending_identity_id }}</td>
              </tr>
            </tbody>
          </table><div
            v-else-if="!detailLoading"
            class="empty"
          >
            暂无已验证联系人入组
          </div>
        </article>
      </main>
      <div
        v-else
        class="campaign-detail empty"
      >
        选择或创建一个 Campaign
      </div>
    </section>
  </div>
</template>

<style scoped>
.campaign-shell { overflow: auto; }
.campaign-head { justify-content: space-between; padding-top: var(--space3); }
.campaign-head > div { display: flex; align-items: baseline; gap: var(--space3); }
.eyebrow { color: var(--fact); font-size: 11px; font-weight: 800; letter-spacing: .16em; }
.principle { display: flex; gap: var(--space3); border-left: 4px solid var(--fact); background: var(--fact-soft); padding: var(--space3) var(--space4); }
.principle span, .action-note { color: var(--text-secondary); }
.action-note { font-size: 12px; }
.boundary-editor, .campaign-list, .campaign-detail { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; }
.boundary-editor { padding: var(--space5); }
.boundary-editor > header, .campaign-list > header, .campaign-detail > header, .enrollment-panel > header { display: flex; justify-content: space-between; gap: var(--space3); align-items: flex-start; }
.boundary-editor header p { color: var(--text-secondary); font-size: 12px; }
.boundary-editor form { display: grid; grid-template-columns: repeat(2, 1fr); gap: var(--space3); margin-top: var(--space4); }
label, fieldset { display: grid; gap: var(--space1); font-size: 12px; font-weight: 700; }
input, select { width: 100%; border: 1px solid var(--border); border-radius: var(--radius-sm); background: white; padding: 7px 9px; }
small { color: var(--text-secondary); font-weight: 400; }
fieldset { border: 1px solid var(--border); border-radius: var(--radius); padding: var(--space3); }
legend { padding: 0 var(--space2); }
.sender-picker, .sequence, .wide, .stop-rule, .editor-actions { grid-column: 1 / -1; }
.sender-picker { grid-template-columns: repeat(2, 1fr); }
.sender-picker legend, .sender-picker p { grid-column: 1 / -1; }
.sender-picker label { display: grid; grid-template-columns: auto 1fr; align-items: center; column-gap: var(--space2); }
.sender-picker input, .stop-rule input { width: auto; }
.sender-picker small { grid-column: 2; }
.limit-row { display: grid; grid-template-columns: 1fr 1fr; gap: var(--space3); }
.sequence article { display: grid; grid-template-columns: 70px 1fr 1fr auto; align-items: end; gap: var(--space2); }
.stop-rule { display: flex; flex-direction: row; align-items: center; }
.editor-actions { display: flex; justify-content: flex-end; gap: var(--space2); }
.campaign-layout { display: grid; grid-template-columns: 300px 1fr; gap: var(--space3); min-height: 500px; }
.campaign-list { padding: var(--space3); }
.campaign-list header { align-items: center; margin-bottom: var(--space2); }
.campaign-row { width: 100%; display: flex; justify-content: space-between; gap: var(--space2); text-align: left; padding: var(--space3); margin-top: var(--space2); border-left: 3px solid transparent; }
.campaign-row.active { border-left-color: var(--action); background: #edf4ff; }
.campaign-row span { display: grid; min-width: 0; }
.campaign-row small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.campaign-row em { align-self: flex-start; border-radius: 999px; padding: 2px 7px; font-size: 10px; font-style: normal; white-space: nowrap; }
.state-active { color: var(--fact); background: var(--fact-soft); }
.state-pending_approval, .state-paused { color: var(--warning); background: var(--warning-soft); }
.state-cancelled { color: var(--danger); background: var(--danger-soft); }
.campaign-detail { padding: var(--space5); min-width: 0; }
.campaign-detail > header p { color: var(--text-secondary); font-family: ui-monospace, monospace; font-size: 11px; }
.version { color: var(--action); font-size: 10px; font-weight: 800; letter-spacing: .1em; }
.detail-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: var(--space2); }
.danger-button { color: var(--danger); border-color: var(--danger); }
.quota-grid, .boundary-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: var(--space3); margin-top: var(--space4); }
.quota-grid article, .boundary-grid article, .sequence-view, .enrollment-panel { border: 1px solid var(--border); border-radius: var(--radius); padding: var(--space4); }
.quota-grid article { display: grid; gap: var(--space2); }
.quota-grid span, dt { color: var(--text-secondary); font-size: 11px; }
.quota-grid strong { font-size: 18px; }
progress { width: 100%; accent-color: var(--fact); }
dl, .boundary-grid ul { display: grid; gap: var(--space2); margin-top: var(--space3); }
dl div { display: grid; grid-template-columns: 90px 1fr; border-top: 1px solid var(--border); padding-top: var(--space2); }
.boundary-grid ul { list-style: none; }
.boundary-grid li { display: flex; justify-content: space-between; gap: var(--space2); border-top: 1px solid var(--border); padding-top: var(--space2); }
.boundary-grid li span { color: var(--text-secondary); }
.sequence-view, .enrollment-panel { margin-top: var(--space3); }
.sequence-view ol { display: flex; list-style: none; margin-top: var(--space3); }
.sequence-view li { display: flex; flex: 1; gap: var(--space2); position: relative; }
.sequence-view li > span { display: grid; place-items: center; width: 28px; height: 28px; border-radius: 50%; background: var(--fact); color: white; font-weight: 800; }
.sequence-view li div { display: grid; }
.sequence-view p { color: var(--fact); margin-top: var(--space3); font-size: 12px; font-weight: 700; }
.enrollment-summary { display: flex; flex-wrap: wrap; gap: var(--space2); margin-top: var(--space3); }
.enrollment-summary span { border-radius: var(--radius-sm); background: #f3f6f5; padding: var(--space2) var(--space3); }
.enrollment-summary strong { margin-right: 5px; }
table { width: 100%; border-collapse: collapse; margin-top: var(--space3); font-size: 11px; }
th, td { border-bottom: 1px solid var(--border); padding: var(--space2); text-align: left; }
td { font-family: ui-monospace, monospace; }
.empty { display: grid; place-items: center; min-height: 90px; color: var(--text-secondary); }
@media (max-width: 980px) { .campaign-layout, .boundary-editor form, .quota-grid, .boundary-grid { grid-template-columns: 1fr; } .sender-picker { grid-template-columns: 1fr; } .campaign-list { max-height: 320px; overflow: auto; } }
</style>
