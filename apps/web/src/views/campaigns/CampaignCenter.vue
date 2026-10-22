<script setup lang="ts">
import { codeLabel } from "../../components/displayLabels";
/* global window, Event, HTMLSelectElement */
import { computed, inject, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import { useRoute, useRouter } from "vue-router";

import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";
import { useQuoteRequestScope } from "../costing-quotes/quote-request-scope";

type ApiClient = ReturnType<typeof createApiClient>;
type Campaign = components["schemas"]["CampaignView"];
type CampaignBody = components["schemas"]["CampaignBoundaryBody"];
type Enrollment = components["schemas"]["EnrollmentView"];
type Identity = components["schemas"]["IdentityView"];
type RuntimeCapability = components["schemas"]["RuntimeCapability"];

const client = inject<ApiClient>("tradeos-api-client", apiClient);
const route = useRoute();
const router = useRouter();
let disposed = false;
onBeforeUnmount(() => { disposed = true; });
const configurationMode = ref<Enrollment["execution_mode"]>(route.query.execution_mode === "manual" ? "manual" : "automatic");
const manual = computed(() => configurationMode.value === "manual");
const campaigns = ref<Campaign[]>([]);
const identities = ref<Identity[]>([]);
const capabilities = ref<RuntimeCapability[]>([]);
const capabilitiesLoading = ref(true);
const capabilityError = ref(false);
const enrollments = ref<Enrollment[]>([]);
const selectedId = ref<string | null>(null);
const loading = ref(true);
const detailLoading = ref(false);
const actionBusy = ref(false);
const error = ref<string | null>(null);
const identityError = ref<string | null>(null), enrollmentError = ref<string | null>(null);
const identitiesLoading = ref(true), commandUnknown = ref(false);
const actionMessage = ref("");
const pendingTransition = ref<{ campaignId: string; state: string } | null>(null);
const editorOpen = ref(false);
const editingCampaignId = ref<string | null>(null);
const agentEnabled = ref(false), agentOwner = ref(""), assessmentRef = ref("");
const agentDocumentIds = ref<string[]>([]);
const canConfigureAgent = ref(false), agentInputsError = ref("");
const agentEmployees = ref<components["schemas"]["EmployeeView"][]>([]);
const agentDocuments = ref<components["schemas"]["KnowledgeDocumentView"][]>([]);
const dialogueEnabled = ref(false), dialogueOwner = ref(""), dialogueBasis = ref("");
const dialogueProducts = ref<components["schemas"]["ProductSupplyCardView"][]>([]);
const dialogueProductIds = ref<string[]>([]), dialogueSenderIds = ref<string[]>([]);
const dialogueExpiry = ref(""), dialogueRounds = ref(3), dialogueError = ref("");
function resetDialogue(): void {
  dialogueEnabled.value = false; dialogueOwner.value = ""; dialogueBasis.value = "";
  dialogueProductIds.value = []; dialogueSenderIds.value = []; dialogueExpiry.value = "";
  dialogueRounds.value = 3; form.ordinary_dialogue_policy = null;
}
const marketsText = ref("");
const entityTypesText = ref("");
const categoriesText = ref("");
const triggersText = ref("quantity_provided, materials_requested, sample_requested, quote_requested, specification_file_received, payment_or_contract_terms");
const form = reactive<CampaignBody>({
  name: "",
  markets: [],
  target_entity_types: [],
  allowed_categories: [],
  sender_identity_ids: [],
  steps: [
    { step_number: 1, intent: "discovery", wait_days: 0 },
  ],
  daily_new_contact_limit: 3,
  daily_total_message_limit: 3,
  handoff_triggers: [],
  stop_on_reply: true,
});

function reset(): void {
  resetDialogue(); dialogueProducts.value = []; dialogueError.value = "";
  configurationMode.value = "automatic";
  if (!disposed && route.query.execution_mode !== undefined) {
    const query = { ...route.query }; delete query.execution_mode;
    void router.replace({ query });
  }
  canConfigureAgent.value = false; agentEnabled.value = false; agentOwner.value = ""; assessmentRef.value = ""; agentDocumentIds.value = []; agentEmployees.value = []; agentDocuments.value = []; agentInputsError.value = ""; form.customer_agent_policy = null;
  campaigns.value = []; identities.value = []; enrollments.value = []; selectedId.value = null;
  loading.value = false; detailLoading.value = false; identitiesLoading.value = false; capabilitiesLoading.value = false; actionBusy.value = false;
  capabilities.value = []; capabilityError.value = false;
  error.value = null; identityError.value = null; enrollmentError.value = null; actionMessage.value = "";
  editorOpen.value = false; editingCampaignId.value = null; commandUnknown.value = false; pendingTransition.value = null;
  marketsText.value = ""; entityTypesText.value = ""; categoriesText.value = ""; triggersText.value = "";
  Object.assign(form, { name: "", markets: [], target_entity_types: [], allowed_categories: [], sender_identity_ids: [], steps: [], handoff_triggers: [], daily_new_contact_limit: 3, daily_total_message_limit: 3, stop_on_reply: true });
}
const gate = useQuoteRequestScope(client, () => [], reset);
function protectedFailure(status: number): void {
  if ([401, 403, 404].includes(status)) { gate.invalidate(); reset(); }
}
const selected = computed(
  () => campaigns.value.find((item) => item.campaign_id === selectedId.value) ?? null,
);
const hasUsableSender = computed(() =>
  identities.value.some((identity) => identity.usable_for_cold_outreach),
);
const requiredCapabilities = computed(() => manual.value ? ["campaign"] : ["research", "contacts", "campaign", "reply"]);
const realFlowComposed = computed(() =>
  !capabilitiesLoading.value && !capabilityError.value && requiredCapabilities.value.every(
    (name) => capabilities.value.some((item) => item.name === name && item.status === "enabled"),
  ),
);
function usableSenders(ids: string[]): boolean {
  return ids.length > 0 && ids.every(id => identities.value.some(identity => identity.identity_id === id && identity.usable_for_cold_outreach));
}
const formSendersUsable = computed(() => usableSenders(form.sender_identity_ids));
const selectedSendersUsable = computed(() => usableSenders(selected.value?.boundary.sender_identity_ids ?? []));
const modeLocked = computed(() => editorOpen.value || actionBusy.value || commandUnknown.value);
function changeConfigurationMode(event: Event): void {
  if (modeLocked.value) return;
  const value = (event.target as HTMLSelectElement).value;
  if (value !== "manual" && value !== "automatic") return;
  configurationMode.value = value;
  actionMessage.value = ""; error.value = null;
}
const selectedSenderAddress = computed(() => {
  const identityId = selected.value?.boundary.sender_identity_ids[0];
  return identities.value.find((item) => item.identity_id === identityId)?.address ?? "已配置";
});

const stateLabels = computed<Record<string, string>>(() => ({
  draft: "草稿",
  pending_approval: manual.value ? "等待批准" : "准备启动",
  active: manual.value || !realFlowComposed.value ? "范围已批准" : "运行中",
  paused: "已暂停",
  completed: "已完成",
  cancelled: "已取消",
}));

function safeError(status: number): string {
  if (status === 401) return "登录身份已失效，请重新选择有效身份";
  if (status === 404) return "活动不存在或当前身份不可见";
  if (status === 403) return "当前身份没有执行此操作的权限";
  if (status === 409) return "活动状态已变化，请刷新后重试";
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
  if (actionBusy.value || commandUnknown.value) return;
  editingCampaignId.value = null;
  resetDialogue();
  agentEnabled.value = false; agentOwner.value = ""; assessmentRef.value = ""; agentDocumentIds.value = []; form.customer_agent_policy = null;
  form.name = "";
  marketsText.value = "";
  entityTypesText.value = manual.value ? "" : "importer, distributor, fleet_operator";
  categoriesText.value = "";
  triggersText.value = "quantity_provided, materials_requested, sample_requested, quote_requested, specification_file_received, payment_or_contract_terms";
  form.sender_identity_ids = [];
  form.daily_new_contact_limit = manual.value ? 1 : 3;
  form.daily_total_message_limit = manual.value ? 1 : 3;
  form.stop_on_reply = true;
  form.steps = [
    { step_number: 1, intent: "discovery", wait_days: 0 },
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
  const dialogue = campaign.boundary.ordinary_dialogue_policy;
  dialogueEnabled.value = Boolean(dialogue); dialogueOwner.value = dialogue?.owner_employee_id ?? "";
  dialogueBasis.value = dialogue?.assessment_ref ?? ""; dialogueProductIds.value = dialogue?.product_refs.map(ref => ref.product_id) ?? [];
  dialogueSenderIds.value = [...(dialogue?.reply_sender_identity_ids ?? [])]; dialogueRounds.value = dialogue?.max_rounds ?? 3;
  dialogueExpiry.value = dialogue?.expires_at ? new Date(new Date(dialogue.expires_at).getTime() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0,16) : "";
  const policy = campaign.boundary.customer_agent_policy;
  agentEnabled.value = Boolean(policy); agentOwner.value = policy?.owner_employee_id ?? "";
  assessmentRef.value = policy?.assessment_ref ?? ""; agentDocumentIds.value = policy?.confirmed_refs?.map(ref => ref.document_id) ?? [];
  form.customer_agent_policy = null;
  editorOpen.value = true;
}

async function loadCampaigns(): Promise<void> {
  const op = gate.begin("list"); if (!op?.valid()) return;
  loading.value = true; error.value = null;
  try {
    const result = await client.GET("/crm/campaigns", { params: { query: { limit: 100 } }, signal: op.signal });
    if (!op.valid()) return;
    if (result.response.status !== 200 || !result.data) {
      campaigns.value = []; enrollments.value = []; selectedId.value = null;
      protectedFailure(result.response.status); error.value = safeError(result.response.status); return;
    }
    campaigns.value = result.data;
    const expected = pendingTransition.value;
    if (commandUnknown.value && expected && result.data.some(item => item.campaign_id === expected.campaignId && item.state === expected.state)) {
      commandUnknown.value = false; pendingTransition.value = null; actionMessage.value = "已核对精确活动当前状态；在途事实与额度保持不变。";
    } else if (commandUnknown.value && expected) {
      error.value = "操作结果仍待核对；不会重复提交。";
    }
    const retained = selectedId.value;
    if (retained && !result.data.some(item => item.campaign_id === retained)) {
      selectedId.value = null; enrollments.value = []; enrollmentError.value = "原活动不存在或当前身份不可见"; return;
    }
    const next = retained ?? result.data[0]?.campaign_id;
    if (next) await loadEnrollments(next); else enrollments.value = [];
  } catch {
    if (!op.valid()) return;
    campaigns.value = []; enrollments.value = []; selectedId.value = null; error.value = "活动读取失败，请刷新核对";
  } finally { if (op.valid()) loading.value = false; }
}
async function loadIdentities(): Promise<void> {
  const op = gate.begin("identities"); if (!op?.valid()) return;
  identitiesLoading.value = true; identityError.value = null; identities.value = [];
  try {
    const result = await client.GET("/crm/sending-identities", { params: { query: { limit: 100 } }, signal: op.signal });
    if (!op.valid()) return;
    if (result.response.status === 200 && result.data) identities.value = result.data;
    else { protectedFailure(result.response.status); form.sender_identity_ids = []; identityError.value = `发件身份读取失败：${safeError(result.response.status)}`; }
  } catch { if (op.valid()) { form.sender_identity_ids = []; identityError.value = "发件身份读取失败，请刷新核对"; } }
  finally { if (op.valid()) identitiesLoading.value = false; }
}
async function loadCapabilities(): Promise<void> {
  const op = gate.begin("capabilities"); if (!op?.valid()) return;
  capabilitiesLoading.value = true; capabilityError.value = false; capabilities.value = [];
  try {
    const result = await client.GET("/health/capabilities", { signal: op.signal });
    if (!op.valid()) return;
    if (result.response.status === 200 && result.data) capabilities.value = result.data;
    else { protectedFailure(result.response.status); capabilityError.value = true; }
  } catch { if (op.valid()) capabilityError.value = true; }
  finally { if (op.valid()) capabilitiesLoading.value = false; }
}
async function loadEnrollments(campaignId: string): Promise<void> {
  const op = gate.begin("enrollments"); if (!op?.valid()) return;
  selectedId.value = campaignId; detailLoading.value = true; enrollments.value = []; enrollmentError.value = null;
  try {
    const result = await client.GET("/crm/campaigns/{campaign_id}/enrollments", { params: { path: { campaign_id: campaignId }, query: { limit: 200 } }, signal: op.signal });
    if (!op.valid() || selectedId.value !== campaignId) return;
    if (result.response.status === 200 && result.data) enrollments.value = result.data;
    else { protectedFailure(result.response.status); enrollmentError.value = `入组进度读取失败：${safeError(result.response.status)}`; }
  } catch { if (op.valid() && selectedId.value === campaignId) enrollmentError.value = "入组进度读取失败，请刷新核对"; }
  finally { if (op.valid() && selectedId.value === campaignId) detailLoading.value = false; }
}
async function saveBoundary(): Promise<void> {
  if (actionBusy.value || commandUnknown.value || identityError.value || identitiesLoading.value || (!realFlowComposed.value && !((agentEnabled.value || dialogueEnabled.value) && canConfigureAgent.value)) || !formSendersUsable.value) return;
  syncFormCollections();
  if (dialogueEnabled.value) {
    if (!dialogueOwner.value || !dialogueBasis.value.trim() || !dialogueProductIds.value.length || !dialogueSenderIds.value.length || !dialogueExpiry.value || dialogueError.value) {
      error.value = "请选择普通沟通的负责人、产品、回复邮箱和授权期限，并填写依据引用"; return;
    }
    form.ordinary_dialogue_policy = {owner_employee_id: dialogueOwner.value, assessment_ref: dialogueBasis.value.trim(),
      product_ids: [...dialogueProductIds.value], reply_sender_identity_ids: [...dialogueSenderIds.value],
      expires_at: new Date(dialogueExpiry.value).toISOString(), max_rounds: dialogueRounds.value};
  } else { form.ordinary_dialogue_policy = campaigns.value.find(c => c.campaign_id === editingCampaignId.value)?.boundary.ordinary_dialogue_policy ? null : undefined; }
  if (agentEnabled.value) {
    const docs = agentDocuments.value.filter(doc => agentDocumentIds.value.includes(doc.document_id));
    if (!canConfigureAgent.value || agentInputsError.value || !agentEmployees.value.some(e => e.employee_id === agentOwner.value) || !assessmentRef.value.trim() || docs.length < 1 || docs.length > 5 || docs.length !== agentDocumentIds.value.length) {
      error.value = "请选择当前在职负责人、1至5份已确认资料，并填写处理依据引用"; return;
    }
    form.customer_agent_policy = {prepare_and_enroll: true, owner_employee_id: agentOwner.value,
      assessment_ref: assessmentRef.value.trim(), basis: "legitimate_interest", subject_type: "legal_entity", contact_type: "role_based",
      confirmed_refs: docs.map(doc => ({document_id: doc.document_id, revision_id: doc.current_revision_id!}))};
  } else { form.customer_agent_policy = campaigns.value.find(c => c.campaign_id === editingCampaignId.value)?.boundary.customer_agent_policy ? null : undefined; }
  if (!form.markets.length || !form.allowed_categories.length || !form.sender_identity_ids.length || (manual.value && !form.target_entity_types.length)) {
    error.value = manual.value ? "请填写目标市场、客户类型、产品并选择发件邮箱" : "请填写目标市场、产品并选择发件邮箱";
    return;
  }
  if (!form.name.trim()) form.name = `${form.markets[0]} · ${form.allowed_categories[0]}`;
  const op = gate.begin("command"); if (!op?.valid()) return;
  const editingId = editingCampaignId.value; pendingTransition.value = null;
  actionBusy.value = true;
  error.value = null;
  try {
    if (editingCampaignId.value) {
      const result = await client.POST("/crm/campaigns/{campaign_id}/revise", {
        params: { path: { campaign_id: editingCampaignId.value } },
        body: { ...form },
      });
      if (!op.valid() || editingCampaignId.value !== editingId) return;
      if (result.response.status === 200 && result.data) {
        const started = await client.POST("/crm/campaigns/{campaign_id}/start", {
          params: { path: { campaign_id: editingCampaignId.value } },
        });
        if (!op.valid() || editingCampaignId.value !== editingId) return;
        if (started.response.status !== 200) {
          protectedFailure(started.response.status);
          commandUnknown.value = started.response.status >= 500;
          error.value = commandUnknown.value ? "启动结果待核对，请刷新当前活动；不自动重发" : safeError(started.response.status);
          return;
        }
        actionMessage.value = manual.value ? "新版本联系范围已批准，本页尚未发信；请在客户详情手动入组后逐封核对发送。" : "新版本已启动；真实搜索、联系人验证和发信仍以运行门禁状态为准。";
        editorOpen.value = false;
        await loadCampaigns();
        return;
      }
      protectedFailure(result.response.status);
      commandUnknown.value = result.response.status >= 500;
      error.value = commandUnknown.value ? "提交结果待核对，请刷新当前活动；不自动重新提交" : safeError(result.response.status);
    } else {
      const result = await client.POST("/crm/campaigns", {
        body: { ...form },
      });
      if (!op.valid() || editingCampaignId.value !== editingId) return;
      if (result.response.status === 200 && result.data) {
        selectedId.value = result.data.campaign_id;
        const started = await client.POST("/crm/campaigns/{campaign_id}/start", {
          params: { path: { campaign_id: result.data.campaign_id } },
        });
        if (!op.valid()) return;
        if (started.response.status !== 200) {
          protectedFailure(started.response.status);
          commandUnknown.value = started.response.status >= 500;
          error.value = commandUnknown.value ? "启动结果待核对，请刷新当前活动；不自动重发" : safeError(started.response.status);
          return;
        }
        actionMessage.value = manual.value ? "手动联系范围已批准，本页尚未发信；请在客户详情手动入组后逐封核对发送。" : "活动边界已启动；尚需确认真实搜索、邮箱验证和发信门禁就绪。";
        editorOpen.value = false;
        await loadCampaigns();
        return;
      }
      protectedFailure(result.response.status);
      commandUnknown.value = result.response.status >= 500;
      error.value = commandUnknown.value ? "提交结果待核对，请刷新当前活动；不自动重新提交" : safeError(result.response.status);
    }
  } catch {
    if (op.valid()) { commandUnknown.value = true; error.value = "提交结果待核对，请刷新当前活动；不自动重新提交"; }
  } finally {
    if (op.valid()) actionBusy.value = false;
  }
}

async function transition(action: "start" | "activate" | "pause" | "cancel"): Promise<void> {
  if (!selected.value || actionBusy.value || commandUnknown.value) return;
  if ((action === "start" || action === "activate") && ((!realFlowComposed.value && !(canConfigureAgent.value && (selected.value?.boundary.customer_agent_policy || selected.value?.boundary.ordinary_dialogue_policy))) || !selectedSendersUsable.value || identitiesLoading.value || identityError.value)) return;
  const op = gate.begin("command"); if (!op?.valid()) return;
  actionBusy.value = true;
  error.value = null;
  const campaignId = selected.value.campaign_id;
  pendingTransition.value = { campaignId, state: ({ pause: "paused", activate: "active", start: "active", cancel: "cancelled" })[action] };
  try {
    if (action === "pause") {
      const reason = window.prompt("填写暂停原因（暂停只阻止新发送，回复仍继续处理）");
      if (!reason?.trim()) return;
      const result = await client.POST("/crm/campaigns/{campaign_id}/pause", {
        params: { path: { campaign_id: campaignId } },
        body: { reason: reason.trim() },
      });
      if (!op.valid() || selectedId.value !== campaignId) return;
      if (result.response.status !== 200) { protectedFailure(result.response.status); commandUnknown.value = result.response.status >= 500; error.value = commandUnknown.value ? "操作结果待核对，请刷新当前活动，不自动重发" : safeError(result.response.status); return; }
      actionMessage.value = "活动已暂停新发送；入站回复处理保持运行。";
    } else if (action === "start") {
      const result = await client.POST("/crm/campaigns/{campaign_id}/start", {
        params: { path: { campaign_id: campaignId } },
      });
      if (!op.valid() || selectedId.value !== campaignId) return;
      if (result.response.status !== 200 || !result.data) { protectedFailure(result.response.status); commandUnknown.value = result.response.status >= 500; error.value = commandUnknown.value ? "启动结果待核对，请刷新当前活动，不自动重发" : safeError(result.response.status); return; }
      actionMessage.value = manual.value ? "手动联系范围已批准，本页尚未发信；实际发送仍须手动入组并逐封核对。" : "活动边界已启动；尚需确认真实搜索、邮箱验证和发信门禁就绪。";
    } else if (action === "activate") {
      const result = await client.POST("/crm/campaigns/{campaign_id}/activate", {
        params: { path: { campaign_id: campaignId } },
      });
      if (!op.valid() || selectedId.value !== campaignId) return;
      if (result.response.status !== 200) { protectedFailure(result.response.status); commandUnknown.value = result.response.status >= 500; error.value = commandUnknown.value ? "操作结果待核对，请刷新当前活动，不自动重发" : safeError(result.response.status); return; }
      actionMessage.value = manual.value ? "已恢复批准范围；现有入组模式不会改变，手动联系须从客户详情手动入组。" : "活动已恢复；新发送仍须通过联系人、邮箱和退订门禁。";
    } else {
      const result = await client.POST("/crm/campaigns/{campaign_id}/cancel", {
        params: { path: { campaign_id: campaignId } },
      });
      if (!op.valid() || selectedId.value !== campaignId) return;
      if (result.response.status !== 200) { protectedFailure(result.response.status); commandUnknown.value = result.response.status >= 500; error.value = commandUnknown.value ? "操作结果待核对，请刷新当前活动，不自动重发" : safeError(result.response.status); return; }
      actionMessage.value = "活动已取消，不能恢复。";
    }
    await loadCampaigns();
  } catch {
    if (op.valid() && selectedId.value === campaignId) { commandUnknown.value = true; error.value = "操作结果待核对，请刷新当前活动；不自动重发"; }
  } finally {
    if (op.valid()) actionBusy.value = false;
  }
}

async function loadAgentInputs(): Promise<void> {
  const op = gate.begin("agent-inputs"); if (!op?.valid()) return;
  try {
    const access = await client.GET("/prospects/customers/access", {signal: op.signal});
    if (!op.valid()) return;
    if (!access.response.ok || !access.data?.can_assign) { canConfigureAgent.value = false; return; }
    canConfigureAgent.value = true;
    const [employees, documents] = await Promise.all([
      client.GET("/team/employees", {signal: op.signal}),
      client.GET("/knowledge/documents", {params: {query: {limit: 100}}, signal: op.signal}),
    ]);
    if (!op.valid()) return;
    if (!employees.response.ok || !documents.response.ok || !employees.data || !documents.data) {
      agentEmployees.value = []; agentDocuments.value = [];
      if ([401,403,404].includes(employees.response.status)) protectedFailure(employees.response.status);
      if ([401,403,404].includes(documents.response.status)) protectedFailure(documents.response.status);
      agentInputsError.value = "负责人或确认资料读取失败，请刷新核对"; return;
    }
    const tenant = client.identitySnapshot().identity?.tenantId;
    agentEmployees.value = employees.data.filter(e => e.tenant_id === tenant && e.is_active && ["boss","manager","sales"].includes(e.role));
    agentDocuments.value = documents.data.items.filter(d => d.tenant_id === tenant && d.status === "confirmed" && d.current_revision_id);
    agentInputsError.value = "";
  } catch { if (op.valid()) { agentEmployees.value = []; agentDocuments.value = []; agentInputsError.value = "负责人或确认资料读取失败，请刷新核对"; } }
}
async function loadDialogueProducts(): Promise<void> {
  const op = gate.begin("dialogue-products"); if (!op?.valid()) return;
  try {
    const result = await client.GET("/products", {params: {query: {limit: 50}}, signal: op.signal});
    if (!op.valid()) return;
    if (!result.response.ok || !result.data) { dialogueError.value = "产品读取失败，请刷新"; return; }
    dialogueProducts.value = result.data.filter(p => p.pool === "formal" && p.candidate_status === null && !p.source_only);
    dialogueError.value = "";
  } catch { if (op.valid()) dialogueError.value = "产品读取失败，请刷新"; }
}
onMounted(() => {
  void Promise.all([loadCampaigns(), loadIdentities(), loadCapabilities(), loadAgentInputs(), loadDialogueProducts()]);
});
</script>

<template>
  <div class="shell campaign-shell">
    <div class="page-head campaign-head">
      <div>
        <p class="eyebrow">
          客户开发
        </p><h1>{{ manual ? "手动联系范围" : "自动发邮件" }}</h1>
      </div>
      <button
        class="btn-primary"
        type="button"
        :disabled="actionBusy || commandUnknown"
        @click="newCampaign"
      >
        {{ manual ? "设置手动联系范围" : "设置自动任务" }}
      </button>
    </div>

    <label class="purpose-picker">配置用途<select
      :value="configurationMode"
      aria-label="配置用途"
      :disabled="modeLocked"
      @change="changeConfigurationMode"
    ><option value="manual">单封手动联系</option><option value="automatic">自动开发任务</option></select></label>
    <div
      v-if="manual"
      class="principle"
    >
      <strong>逐封核对后发送。</strong><span>这里只配置和批准联系范围，开发任务本身不会保存为手动模式，现有入组模式不会改变。请从客户详情手动入组；手动入组不会被自动发送调度选中。</span>
    </div>
    <div
      v-else
      class="principle"
    >
      <strong>首轮仅发第一封。</strong><span>按本企业设置的产品与目标市场寻找客户；真实搜索与邮箱验证就绪后才可发送，明确购买意向转人工。</span>
    </div>
    <p
      v-if="!identitiesLoading && !identityError && !hasUsableSender"
      class="safe-banner"
      role="status"
    >
      当前没有通过认证并可用于冷开发的发件邮箱，{{ manual ? "手动联系范围尚不能批准。" : "自动发信尚不能启动。" }}
    </p>
    <p
      v-if="!capabilitiesLoading && !realFlowComposed"
      class="safe-banner"
      role="status"
    >
      {{ manual ? "手动发送能力尚未接通，当前不能批准手动联系范围。" : "真实搜索、联系人验证、发信或回复处理尚未完整接通；当前不能启动外发活动。" }}{{ capabilityError ? "能力状态读取失败，请刷新页面核对。" : "" }}
    </p>
    <div
      v-if="error"
      class="safe-banner danger"
      role="alert"
    >
      <span>{{ error }}</span>
      <button
        v-if="commandUnknown"
        type="button"
        @click="loadCampaigns"
      >
        核对状态
      </button>
    </div>
    <div
      v-if="actionMessage"
      class="action-note"
      role="status"
    >
      {{ actionMessage }}
    </div>

    <section
      v-if="editorOpen"
      class="boundary-editor"
      aria-label="活动边界编辑器"
    >
      <header>
        <div><h2>{{ manual ? (editingCampaignId ? "修订联系范围" : "创建手动联系范围") : (editingCampaignId ? "修改活动边界" : "创建客户开发任务") }}</h2><p>{{ manual ? "新范围默认只发首封了解需求的邮件，每日新增联系人及总邮件上限均为1；已有边界修订保留原值，批准本身不发送。" : "只保存首封邮件边界；活动启动不代表外部搜索和发信已经就绪。" }}</p></div><button
          type="button"
          @click="editorOpen = false"
        >
          关闭
        </button>
      </header>
      <form @submit.prevent="saveBoundary">
        <label>目标市场<input
          v-model="marketsText"
          aria-label="目标市场"
          placeholder="填写本次开发的目标市场"
        ></label>
        <label>产品<input
          v-model="categoriesText"
          aria-label="产品类别"
          placeholder="填写本企业要开发的产品类别"
        ></label>
        <label v-if="manual">客户类型<input
          v-model="entityTypesText"
          aria-label="客户类型"
          placeholder="填写客户类型，如 distributor；多项用逗号分隔"
        ></label>
        <fieldset class="sender-picker">
          <legend>发件邮箱</legend><label
            v-for="identity in identities"
            :key="identity.identity_id"
          ><input
            v-model="form.sender_identity_ids"
            type="checkbox"
            :value="identity.identity_id"
            :disabled="!identity.usable_for_cold_outreach"
          ><span>{{ identity.address }}</span><small>{{ codeLabel(identity.state) }} · 今日余量 {{ identity.remaining_today }}</small></label><p
            v-if="identityError"
            role="alert"
          >
            {{ identityError }} <button
              type="button"
              @click="loadIdentities"
            >
              刷新发件身份
            </button>
          </p><p v-else-if="identitiesLoading">
            正在读取发件身份…
          </p><p v-else-if="!identities.length">
            没有可用发件邮箱。
          </p>
        </fieldset>
        <fieldset
          v-if="canConfigureAgent"
          class="sender-picker"
        >
          <legend>搜索后自动准备</legend>
          <label><input
            v-model="agentEnabled"
            type="checkbox"
            aria-label="批准自动准备并入组"
          >在本次范围批准中包含：分配、整理证据、验证官网职务邮箱并入组</label>
          <template v-if="agentEnabled">
            <label>客户负责人<select
              v-model="agentOwner"
              aria-label="自动准备负责人"
            ><option value="">请选择</option><option
              v-for="e in agentEmployees"
              :key="e.employee_id"
              :value="e.employee_id"
            >{{ e.name }}</option></select></label>
            <label>内部产品适配资料<select
              v-model="agentDocumentIds"
              multiple
              aria-label="自动准备确认资料"
            ><option
              v-for="d in agentDocuments"
              :key="d.document_id"
              :value="d.document_id"
            >{{ d.source.filename }}</option></select></label>
            <label>合法利益评估引用<input
              v-model="assessmentRef"
              aria-label="处理依据引用"
              maxlength="512"
              placeholder="填写实际评估记录的编号或引用"
            ></label>
            <p>仅限企业法人、官网明确公开的职务邮箱。选择1至5份已确认资料供内部适配；此确认不代表法律评估已完成，也不授予价格、交期或报价承诺。修订后必须重新批准。</p>
            <p>准备和入组可在发送关闭时完成；缺真实发件身份或验证配置会明确阻断。发送继续遵守全局开关与既有审批。</p>
          </template>
          <p
            v-if="agentInputsError"
            role="alert"
          >
            {{ agentInputsError }}
          </p>
        </fieldset>
        <fieldset v-if="canConfigureAgent">
          <legend>普通邮件持续沟通</legend>
          <label><input v-model="dialogueEnabled" type="checkbox" aria-label="批准普通邮件持续沟通">在本次精确范围内批准普通产品说明和需求追问</label>
          <template v-if="dialogueEnabled">
            <label>客户及邮箱负责人<select v-model="dialogueOwner" aria-label="普通沟通负责人"><option value="">请选择</option><option v-for="e in agentEmployees" :key="e.employee_id" :value="e.employee_id">{{ e.name }}</option></select></label>
            <label>已确认产品<select v-model="dialogueProductIds" multiple aria-label="普通沟通产品"><option v-for="p in dialogueProducts" :key="p.product_id" :value="p.product_id">{{ p.name_zh }}</option></select></label>
            <label>普通回复邮箱<select v-model="dialogueSenderIds" multiple aria-label="普通回复邮箱"><option v-for="identity in identities.filter(i => ['cold_outreach','primary_business'].includes(i.role))" :key="identity.identity_id" :value="identity.identity_id">{{ identity.address }}</option></select></label>
            <label>授权截止时间<input v-model="dialogueExpiry" type="datetime-local" aria-label="普通沟通授权期限"></label>
            <label>每个对话最多回复轮数<input v-model.number="dialogueRounds" type="number" min="1" max="10" aria-label="普通沟通轮数"></label>
            <p>该范围最多调用模型 {{ Math.min(40, dialogueRounds * 4) }} 次，包含分类、需求提取和回复规划；失败或结果未知也计入上限。</p>
            <label>处理依据引用<input v-model="dialogueBasis" maxlength="512" aria-label="普通沟通依据"></label>
            <p>只使用当前获准对外展示的产品资料；内部确认资料不因此公开。首次询盘须再选择本范围并核验联系人、客户归属和本人邮箱权限，不建立冷邮件序列。回复同时占用本活动每日总发送量；两轮无进展即暂停。价格、交期、认证等商业承诺仍逐次交人工。</p>
            <p v-if="dialogueError" role="alert">{{ dialogueError }}</p>
          </template>
        </fieldset>
        <div class="editor-actions">
          <button
            type="button"
            @click="editorOpen = false"
          >
            取消
          </button><button
            class="btn-primary"
            type="submit"
            :disabled="actionBusy || commandUnknown || identitiesLoading || Boolean(identityError) || (!realFlowComposed && !((agentEnabled || dialogueEnabled) && canConfigureAgent)) || !formSendersUsable"
          >
            {{ actionBusy ? (manual ? "批准中…" : "启动中…") : agentEnabled ? "保存并提交范围批准" : manual ? (editingCampaignId ? "保存并批准联系范围" : "创建并批准手动联系范围") : editingCampaignId ? "保存并继续自动发邮件" : "创建并启动自动发邮件" }}
          </button>
        </div>
      </form>
    </section>

    <section class="campaign-layout">
      <aside class="campaign-list">
        <header>
          <h2>{{ manual ? "已有开发任务范围" : "自动任务" }}</h2>
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
          v-if="!loading && !error && !campaigns.length"
          class="empty"
        >
          {{ manual ? "暂无已配置范围" : "暂无自动任务" }}
        </div>
      </aside>

      <main
        v-if="selected"
        class="campaign-detail"
      >
        <header>
          <div>
            <span class="version">{{ stateLabels[selected.state] }}</span><h2>{{ selected.name }}</h2><p
              v-if="selected.paused_reason"
            >
              暂停原因：{{ codeLabel(selected.paused_reason) }}
            </p>
          </div><div class="detail-actions">
            <button
              v-if="selected.state === 'draft' || selected.state === 'pending_approval'"
              type="button"
              :disabled="actionBusy || commandUnknown || identitiesLoading || Boolean(identityError) || (!realFlowComposed && !(canConfigureAgent && (selected?.boundary.customer_agent_policy || selected?.boundary.ordinary_dialogue_policy))) || !selectedSendersUsable"
              @click="transition('start')"
            >
              {{ selected.boundary.customer_agent_policy ? "批准客户准备范围" : manual ? "批准手动联系范围" : "启动自动发邮件" }}
            </button><button
              v-if="selected.state === 'paused'"
              class="btn-primary"
              type="button"
              :disabled="actionBusy || commandUnknown || identitiesLoading || Boolean(identityError) || (!realFlowComposed && !(canConfigureAgent && (selected?.boundary.customer_agent_policy || selected?.boundary.ordinary_dialogue_policy))) || !selectedSendersUsable"
              @click="transition('activate')"
            >
              {{ selected.boundary.customer_agent_policy ? "恢复已批准准备范围" : manual ? "恢复已批准范围" : "启动自动安全发送" }}
            </button><button
              v-if="selected.state === 'active'"
              type="button"
              :disabled="actionBusy || commandUnknown"
              @click="transition('pause')"
            >
              暂停
            </button><button
              v-if="!['completed', 'cancelled'].includes(selected.state)"
              type="button"
              :disabled="actionBusy || commandUnknown"
              @click="reviseCampaign(selected)"
            >
              修订边界
            </button>
          </div>
        </header>
        <div
          v-if="selected.state === 'active'"
          class="automation-running"
          role="status"
        >
          <strong>{{ selected.boundary.customer_agent_policy ? "客户准备范围已批准" : manual ? "联系范围已批准" : "活动已启动" }}</strong><span>{{ selected.boundary.customer_agent_policy ? "搜索时引用本版本，才会按该范围自动整理证据、验证并入组。批准本身不发信，发送仍受全局开关与当前条件控制。" : manual ? "本页尚未发信。实际联系必须从客户详情手动入组，再逐封核对发送；此视图不改变已有自动入组，自动发送也未因此启用。" : "这只表示边界已获准；真实搜索、已验证联系人、可用发件身份和公网退订入口都就绪后才会外发。" }}</span>
        </div>
        <dl class="task-summary">
          <div><dt>目标市场</dt><dd>{{ selected.boundary.markets.join("、") }}</dd></div>
          <div><dt>产品</dt><dd>{{ selected.boundary.allowed_categories.join("、") }}</dd></div>
          <div v-if="manual">
            <dt>客户类型</dt><dd>{{ selected.boundary.target_entity_types.map(value => codeLabel(value)).join("、") }}</dd>
          </div>
          <div><dt>发件邮箱</dt><dd>{{ selectedSenderAddress }}</dd></div>
          <div><dt>今日已安排</dt><dd>{{ selected.today_messages_reserved }} / {{ selected.boundary.daily_total_message_limit }}</dd></div>
        </dl>
        <article class="enrollment-panel">
          <header><h3>发送进度</h3><span v-if="!detailLoading && !enrollmentError">{{ enrollments.length }} 个已验证客户</span></header><div
            v-if="detailLoading"
            class="empty"
          >
            加载中…
          </div><div
            v-else-if="enrollmentError"
            role="alert"
          >
            {{ enrollmentError }} <button
              type="button"
              @click="loadEnrollments(selected.campaign_id)"
            >
              刷新入组进度
            </button>
          </div><div
            v-else
            class="enrollment-summary"
          />
          <table v-if="enrollments.length">
            <thead><tr><th>客户</th><th>状态</th><th>当前步骤</th><th>下次发送</th></tr></thead><tbody>
              <tr
                v-for="item in enrollments"
                :key="item.enrollment_id"
              >
                <td>{{ item.account_id }}</td><td>{{ codeLabel(item.state) }}</td><td>{{ item.current_step }}</td><td>{{ item.next_send_at ? new Date(item.next_send_at).toLocaleString("zh-CN", { hour12: false }) : "—" }}</td>
              </tr>
            </tbody>
          </table><div
            v-else-if="!detailLoading && !enrollmentError"
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
        {{ manual ? "发件邮箱就绪后，设置并批准手动联系范围" : "发件邮箱就绪后，设置自动任务即可开始" }}
      </div>
    </section>
  </div>
</template>

<style scoped>
.campaign-shell { overflow: auto; }
.campaign-head { justify-content: space-between; padding-top: var(--space3); }
.campaign-head > div { display: flex; align-items: baseline; gap: var(--space3); }
.purpose-picker { max-width: 300px; margin-bottom: var(--space3); }
.eyebrow { color: var(--fact); font-size: 11px; font-weight: 800; letter-spacing: .16em; }
.principle { display: flex; gap: var(--space3); border-left: 4px solid var(--fact); background: var(--fact-soft); padding: var(--space3) var(--space4); }
.principle span, .action-note { color: var(--text-secondary); }
.pause-semantics { display: flex; gap: var(--space3); border-left: 4px solid var(--warning); background: var(--warning-soft); color: var(--warning); padding: var(--space3) var(--space4); }
.safe-defaults, .automation-running { display: flex; gap: var(--space3); border-left: 4px solid var(--action); background: #edf4ff; padding: var(--space3) var(--space4); }
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
.sender-picker, .sequence, .wide, .stop-rule, .editor-actions, .advanced-settings { grid-column: 1 / -1; }
.sender-picker { grid-template-columns: repeat(2, 1fr); }
.sender-picker legend, .sender-picker p { grid-column: 1 / -1; }
.sender-picker label { display: grid; grid-template-columns: auto 1fr; align-items: center; column-gap: var(--space2); }
.sender-picker input, .stop-rule input { width: auto; }
.sender-picker small { grid-column: 2; }
.limit-row { display: grid; grid-template-columns: 1fr 1fr; gap: var(--space3); }
.advanced-settings { border: 1px solid var(--border); border-radius: var(--radius); padding: var(--space3); }
.advanced-settings summary { cursor: pointer; font-weight: 700; }
.advanced-settings > p { color: var(--text-secondary); font-size: 12px; }
.advanced-settings[open] { display: grid; gap: var(--space3); }
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
.automation-running { margin-top: var(--space4); }
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
@media (max-width: 600px) { .campaign-head > div, .campaign-detail > header, .principle, .pause-semantics { flex-direction: column; } .sequence article, .limit-row { grid-template-columns: 1fr; } .enrollment-panel { overflow-x: auto; } .campaign-detail { overflow-wrap: anywhere; padding: var(--space3); } }
</style>
