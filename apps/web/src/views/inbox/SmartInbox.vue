<script setup lang="ts">
import { computed, inject, onBeforeUnmount, onMounted, ref, watch } from "vue";

import { useRoute } from "vue-router";
import { useQuoteRequestScope } from "../costing-quotes/quote-request-scope";
import ReplyRetry from "./ReplyRetry.vue";
import { replyWorkRecovery } from "./replyWorkRecovery";
import MessageContent from "../../components/MessageContent.vue";
import MessageEvidenceDownload from "../../components/MessageEvidenceDownload.vue";
import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";

type ApiClient = ReturnType<typeof createApiClient>;
type InboxItem = components["schemas"]["ConversationInboxItem"];
type InboxDetail = components["schemas"]["ConversationInboxDetail"];
type InboxMessage = components["schemas"]["InboxMessageView"];
type ReplyCategory = components["schemas"]["ReplyCategory"];
type GroupKey = "all" | "waiting" | "interest" | "commercial" | "compliance" | "auto" | "ordinary";

const client = inject<ApiClient>("tradeos-api-client", apiClient);
const route = useRoute();
const nextQuestions = ref<components["schemas"]["ReplyNextQuestionsView"] | null>(null);
const questionsError = ref<string | null>(null);
const items = ref<InboxItem[]>([]);
const previews = ref<Record<string, components["schemas"]["MessageContentView"]>>({});
type ReplyWork = components["schemas"]["ReplyWorkActionView"];
const replyWork = ref<ReplyWork[]>([]);
const workNotes = ref<Record<string, string>>({});
const workRecovery = replyWorkRecovery(client);
onBeforeUnmount(workRecovery.dispose);
const workRequests = workRecovery.records;
const expandedEvidence = ref<Record<string, boolean>>({});
const workSections = computed(() => [
  { label: "待处理", items: replyWork.value.filter(w => ["pending", "in_progress"].includes(w.status)) },
  { label: "处理历史", items: replyWork.value.filter(w => ["completed", "cancelled"].includes(w.status)) },
]);
const workStatusLabels = { pending: "待处理", in_progress: "处理中", completed: "已完成本次处理", cancelled: "已取消" };
const handlingLabels: Record<string, string> = { started: "开始处理", waiting_customer: "等待客户补充", handled: "完成本次处理", cancelled: "取消处理" };
const workError = ref<string | null>(null);
const handling = ref<string | null>(null);
const detail = ref<InboxDetail | null>(null);
const selectedId = ref<string | null>(null);
const selectedGroup = ref<GroupKey>("all");
const loading = ref(true);
const detailLoading = ref(false);
const correcting = ref(false);
const error = ref<string | null>(null);
const actionMessage = ref<string | null>(null);
const correctionCategory = ref<ReplyCategory>("clear_interest");
let listVersion = 0;
let detailVersion = 0;

function clearProtected(clearPending = true): void {
  if (clearPending) workRecovery.clear();
  expandedEvidence.value = {};
  replyWork.value = []; workError.value = null; handling.value = null; workNotes.value = {};
  previews.value = {}; items.value = []; detail.value = null; selectedId.value = null; nextQuestions.value = null;
  error.value = null; actionMessage.value = null; questionsError.value = null;
  correcting.value = false; detailLoading.value = false; listVersion++; detailVersion++;
}
const gate = useQuoteRequestScope(client, () => [route.fullPath], () => {
  clearProtected(false); globalThis.queueMicrotask(() => void loadItems());
});
watch(() => route.fullPath, () => { clearProtected(false); void loadItems(); }, { flush: "sync" });

const categoryLabels: Record<ReplyCategory, string> = {
  clear_interest: "明确兴趣",
  willing_to_continue: "愿意继续聊",
  requests_materials: "要求资料",
  requests_quote: "要求报价",
  requests_sample: "要求样品",
  provides_specification: "提供规格",
  no_current_need: "无当前需求",
  future_need_possible: "未来可能需要",
  refers_other_contact: "介绍其他联系人",
  rejection: "拒绝",
  unsubscribe: "退订",
  bounce: "退信",
  auto_reply: "自动回复",
  complaint: "投诉",
  needs_review: "需人工核对",
};

const categoryOptions = Object.entries(categoryLabels) as Array<[ReplyCategory, string]>;

const groupLabels: Array<{ key: GroupKey; label: string }> = [
  { key: "all", label: "全部" },
  { key: "waiting", label: "等待回复" },
  { key: "interest", label: "兴趣与继续沟通" },
  { key: "commercial", label: "报价 / 样品 / 规格" },
  { key: "compliance", label: "退订与投诉" },
  { key: "auto", label: "自动回复" },
  { key: "ordinary", label: "其他回复" },
];

const actionLabels: Record<string, string> = {
  stop_sequence: "停止序列",
  review_reply: "核对回复（信息不足或有矛盾）",
  start_qualification: "开始需求确认",
  handoff: "人工接管",
  extract_need_fields: "提取需求字段",
  mark_future_restart: "标记未来可重启",
  create_follow_up: "创建跟进任务",
  intake_new_contact: "新联系人重新准入",
  suppress: "加入抑制",
  route_bounce: "退信分流",
  record_complaint: "记录投诉",
};

function categoryLabel(value: ReplyCategory | null): string {
  return value ? categoryLabels[value] : "尚未分类";
}

function groupOf(value: ReplyCategory | null, direction?: string | null): GroupKey {
  if (direction === "outbound") return "waiting";
  if (value === "clear_interest" || value === "willing_to_continue") return "interest";
  if (
    value === "requests_materials"
    || value === "requests_quote"
    || value === "requests_sample"
    || value === "provides_specification"
  ) return "commercial";
  if (value === "unsubscribe" || value === "complaint") return "compliance";
  if (value === "auto_reply") return "auto";
  return "ordinary";
}

function formatDate(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleString("zh-CN", { hour12: false });
}

function safeError(status: number): string {
  if (status === 404) return "所选会话不存在或不可见";
  if (status === 401 || status === 403) return "当前身份无权访问智能收件箱";
  if (status === 503) return "会话服务暂不可用";
  return "请求未完成，请稍后重试";
}

const filteredItems = computed(() => {
  if (selectedGroup.value === "all") return items.value;
  return items.value.filter(
    (item) => groupOf(item.effective_category, item.latest_direction) === selectedGroup.value,
  );
});

function groupCount(key: GroupKey): number {
  if (key === "all") return items.value.length;
  return items.value.filter((item) => groupOf(item.effective_category, item.latest_direction) === key).length;
}

const orderedMessages = computed(() => [...(detail.value?.messages ?? [])].sort(
  (a, b) => Date.parse(b.sent_at) - Date.parse(a.sent_at) || b.message_id.localeCompare(a.message_id),
));
const latestInbound = computed<InboxMessage | null>(() =>
  orderedMessages.value.find((message) => message.direction === "inbound") ?? null,
);
function counterparty(messageId: string | null | undefined, direction: string | null | undefined): string | undefined {
  const content = previews.value[messageId ?? ""];
  return direction === "inbound" ? content?.sender : content?.recipients;
}

async function loadItems(): Promise<void> {
  const op = gate.begin("list"); if (!op?.valid()) return;
  const version = ++listVersion;
  loading.value = true;
  error.value = null;
  try {
    const result = await client.GET("/inbox/conversations", {
      params: { query: { limit: 200 } },
    });
    if (!op.valid() || version !== listVersion) return;
    if (result.response.status !== 200 || !result.data) {
      if ([401,403,404].includes(result.response.status)) {
        // 列表撤权同时失效详情、建议与纠正写回，旧 finally 不得结束恢复请求。
        gate.invalidate(); clearProtected(); loading.value = false;
      } else {
        items.value = []; detail.value = null; nextQuestions.value = null;
      }
      error.value = safeError(result.response.status);
      return;
    }
    const accountId = typeof route.query.account_id === "string" ? route.query.account_id : null;
    items.value = accountId ? result.data.filter(item => item.account_id === accountId) : result.data;
    const requested = typeof route.params.conversationId === "string" ? route.params.conversationId : typeof route.query.conversation_id === "string" ? route.query.conversation_id : null;
    const retained = requested ?? (selectedId.value && items.value.some(item => item.conversation_id === selectedId.value)
      ? selectedId.value : items.value[0]?.conversation_id ?? null);
    selectedId.value = retained;
    if (retained) await loadDetail(retained);
    else detail.value = null;
  } catch {
    if (op.valid() && version === listVersion) error.value = "无法连接会话服务";
  } finally {
    if (op.valid() && version === listVersion) loading.value = false;
  }
}

async function loadDetail(conversationId: string, preserveActionMessage = false): Promise<void> {
  const op = gate.begin("detail"); if (!op?.valid()) return;
  const version = ++detailVersion;
  replyWork.value = []; workError.value = null; handling.value = null; workNotes.value = {};
  detail.value = null; nextQuestions.value = null; questionsError.value = null; correcting.value = false; error.value = null;
  selectedId.value = conversationId;
  detailLoading.value = true;
  if (!preserveActionMessage) actionMessage.value = null;
  try {
    const result = await client.GET("/inbox/conversations/{conversation_id}", {
      params: { path: { conversation_id: conversationId } },
    });
    if (!op.valid() || version !== detailVersion) return;
    if (result.response.status === 200 && result.data) {
      detail.value = result.data;
      correctionCategory.value = latestInbound.value?.effective_category ?? "clear_interest";
      await loadReplyWork(result.data.account_id);
      if (!op.valid() || version !== detailVersion) return;
      const messageId = latestInbound.value?.message_id;
      if (messageId) {
        try {
          const next = await client.GET("/inbox/conversations/{conversation_id}/messages/{message_id}/next-questions", {
            params: { path: { conversation_id: conversationId, message_id: messageId } },
          });
          if (!op.valid() || version !== detailVersion) return;
          if (next.response.status === 200 && next.data?.conversation_id === conversationId && next.data.source_message_id === messageId) nextQuestions.value = next.data;
          else {
            questionsError.value = "下一问建议暂不可用";
            if ([401,403,404].includes(next.response.status)) {
              // 建议权限与本人消息权限不同；重查消息权限以区分功能拒绝和当前撤权。
              detail.value = null; replyWork.value = [];
              const current = await client.GET("/inbox/conversations/{conversation_id}", {
                params: { path: { conversation_id: conversationId } },
              });
              if (!op.valid() || version !== detailVersion) return;
              if (current.response.status !== 200 || current.data?.conversation_id !== conversationId) {
                gate.invalidate(); clearProtected(); loading.value = false;
                error.value = safeError(current.response.status);
              } else {
                detail.value = current.data;
                await loadReplyWork(current.data.account_id);
              }
            }
          }
        } catch { if (op.valid() && version === detailVersion) questionsError.value = "下一问建议暂不可用"; }
      }
    } else {
      error.value = safeError(result.response.status);
    }
  } catch {
    if (op.valid() && version === detailVersion) error.value = "会话详情加载失败";
  } finally {
    if (op.valid() && version === detailVersion) detailLoading.value = false;
  }
}

async function submitCorrection(): Promise<void> {
  const message = latestInbound.value;
  if (!message || correcting.value) return;
  const op = gate.begin("correction"); if (!op?.valid()) return;
  const conversationId = detail.value?.conversation_id;
  const version = detailVersion;
  if (!conversationId) return;
  correcting.value = true;
  error.value = null;
  actionMessage.value = null;
  try {
    const result = await client.POST(
      "/inbox/messages/{message_id}/correct-classification",
      {
        params: { path: { message_id: message.message_id } },
        body: { category: correctionCategory.value },
      },
    );
    if (!op.valid() || version !== detailVersion) return;
    if (result.response.status !== 200) {
      if ([401,403,404].includes(result.response.status)) { detail.value = null; nextQuestions.value = null; }
      error.value = safeError(result.response.status);
      return;
    }
    await loadDetail(conversationId, true);
    if (!op.valid() || selectedId.value !== conversationId || !detail.value) return;
    actionMessage.value = "人工纠正已记录；模型原判仍保留用于质量评估。";
  } catch {
    if (op.valid() && version === detailVersion) error.value = "人工纠正结果待核对，请刷新当前会话";
  } finally {
    if (op.valid() && selectedId.value === conversationId) correcting.value = false;
  }
}


async function loadReplyWork(accountId: string): Promise<void> {
  const op = gate.begin("reply-work"); if (!op?.valid()) return;
  const version = detailVersion;
  replyWork.value = []; workError.value = null;
  try {
    const results = await Promise.all((["pending", "in_progress", "completed", "cancelled"] as const).map(status =>
      client.GET("/inbox/reply-work", { params: { query: { account_id: accountId, status, limit: 100 } } }),
    ));
    if (!op.valid() || version !== detailVersion || detail.value?.account_id !== accountId) return;
    if (results.some(result => [401, 403, 404].includes(result.response.status))) {
      gate.invalidate(); clearProtected(); error.value = "回复工作项访问权限已变化"; return;
    }
    if (results.some(result => result.response.status !== 200 || !result.data)) {
      workError.value = "回复工作项暂不可用，请刷新核对"; return;
    }
    replyWork.value = results.flatMap(result => result.data ?? []);
  } catch {
    if (op.valid() && version === detailVersion) workError.value = "回复工作项暂不可用，请刷新核对";
  }
}

async function handleReplyWork(work: ReplyWork, disposition: "started" | "waiting_customer" | "handled"): Promise<void> {
  if (handling.value || !work.can_handle) return;
  const note = disposition === "started" ? "" : (workNotes.value[work.action_id] ?? "").trim();
  if (disposition !== "started" && !note) return;
  const status = disposition === "handled" ? "completed" : "in_progress";
  if (workRequests.value[work.action_id]) return;
  const command: components["schemas"]["ReplyWorkUpdate"] = {
    expected_status: work.status, status, disposition, note, request_id: globalThis.crypto.randomUUID(),
  };
  workRecovery.save(work.action_id, command);
  await submitReplyWork(work, command);
}

async function retryReplyWork(work: ReplyWork): Promise<void> {
  const command = workRequests.value[work.action_id];
  if (!command || handling.value || !work.can_handle) return;
  await submitReplyWork(work, command);
}

async function submitReplyWork(work: ReplyWork, command: components["schemas"]["ReplyWorkUpdate"]): Promise<void> {
  const op = gate.begin("handle-reply-work"); if (!op?.valid()) return;
  const version = detailVersion;
  handling.value = work.action_id; workError.value = null;
  try {
    const result = await client.POST("/inbox/reply-work/{action_id}", {
      params: { path: { action_id: work.action_id } },
      body: command,
    });
    if (!op.valid() || version !== detailVersion) return;
    if ([401, 403, 404].includes(result.response.status)) {
      gate.invalidate(); clearProtected(); error.value = "客户负责人或访问权限已变化"; return;
    }
    if ([400, 422].includes(result.response.status)) {
      workRecovery.forget(work.action_id, command.request_id);
      await loadReplyWork(work.account_id);
      if (op.valid() && version === detailVersion) {
        workError.value = "本次处理未保存；已重新读取当前状态，请核对说明后重新提交";
      }
      return;
    }
    if (result.response.status !== 200 || !result.data) {
      workError.value = "处理结果待核对；刷新读取后可重试原处理，不会自动重试"; return;
    }
    workRecovery.forget(work.action_id, command.request_id); delete workNotes.value[work.action_id];
    await loadReplyWork(work.account_id);
  } catch {
    if (op.valid() && version === detailVersion) workError.value = "处理结果待核对；刷新读取后可重试原处理，不会自动重试";
  } finally {
    if (op.valid() && version === detailVersion) handling.value = null;
  }
}

onMounted(() => void loadItems());
</script>

<template>
  <div class="shell inbox-shell">
    <div class="page-head inbox-head">
      <div>
        <p class="eyebrow">
          邮件往来
        </p>
        <h1>客户邮件</h1>
        <RouterLink to="/inbox/mailbox">
          邮箱连接设置
        </RouterLink>
      </div>
      <button
        type="button"
        :disabled="loading"
        @click="loadItems"
      >
        {{ loading ? "加载中…" : "刷新" }}
      </button>
    </div>

    <div class="safe-banner">
      <span aria-hidden="true">i</span>
      <div>显示通过 TradeOS 发起的邮件与已关联回复。邮件发出后即可查看记录；“等待回复”表示系统尚未关联到客户回信。</div>
    </div>
    <div
      v-if="error"
      class="safe-banner danger"
      role="alert"
    >
      {{ error }}
    </div>

    <div
      class="group-tabs"
      aria-label="回复分类组"
    >
      <button
        v-for="group in groupLabels"
        :key="group.key"
        type="button"
        :class="{ active: selectedGroup === group.key }"
        @click="selectedGroup = group.key"
      >
        {{ group.label }} <span>{{ groupCount(group.key) }}</span>
      </button>
    </div>

    <section class="inbox-layout">
      <aside
        class="conversation-list"
        aria-label="会话列表"
      >
        <div
          v-if="loading"
          class="empty"
        >
          正在读取最近会话…
        </div>
        <button
          v-for="conversation in filteredItems"
          v-else
          :key="conversation.conversation_id"
          type="button"
          :class="{ active: selectedId === conversation.conversation_id }"
          @click="loadDetail(conversation.conversation_id)"
        >
          <span class="row-top">
            <strong>{{ conversation.latest_direction === "outbound" ? "等待回复" : categoryLabel(conversation.effective_category) }}</strong>
            <time>{{ formatDate(conversation.latest_message_at) }}</time>
          </span>
          <span class="account">{{ counterparty(conversation.latest_message_id, conversation.latest_direction) ?? conversation.account_id }}</span>
          <span
            v-if="previews[conversation.latest_message_id ?? '']"
            class="model"
          >{{ previews[conversation.latest_message_id ?? ""]?.subject }}</span>
          <span
            v-if="conversation.original_category !== conversation.effective_category"
            class="corrected"
          >
            模型原判：{{ categoryLabel(conversation.original_category) }}
          </span>
          <span class="model">{{ conversation.latest_direction === "outbound" ? "邮件已发出" : conversation.classified_by ?? "等待分类" }}</span>
        </button>
        <div
          v-if="!loading && !filteredItems.length"
          class="empty"
        >
          该分类组暂无会话
        </div>
      </aside>

      <main
        v-if="detail"
        class="conversation-detail"
        :aria-busy="detailLoading"
      >
        <header>
          <div>
            <span class="account-label">邮件会话</span>
            <h2>{{ counterparty(orderedMessages[0]?.message_id, orderedMessages[0]?.direction) ?? detail.account_id }}</h2>
            <p>{{ detail.conversation_id }} · {{ detail.channel }}</p>
          </div>
          <span>{{ detail.messages.length }} 条消息 · 最新在前</span>
        </header>

        <section
          class="timeline"
          aria-label="消息审计时间线"
        >
          <article
            v-for="message in orderedMessages"
            :key="message.message_id"
            class="message-card"
            :class="message.direction"
          >
            <div class="message-head">
              <strong>{{ message.direction === "inbound" ? "收到回复" : "已发送" }}</strong>
              <time :datetime="message.sent_at">{{ message.direction === "inbound" ? "收到于" : "发送于" }} {{ formatDate(message.sent_at) }}</time>
            </div>
            <MessageContent
              :message-id="message.message_id"
              @loaded="previews[message.message_id] = $event"
              @denied="clearProtected(); error = '当前身份无权读取邮件内容'"
            />
            <ReplyRetry
              v-if="message.direction === 'inbound'"
              :message-id="message.message_id"
              @updated="loadItems"
              @denied="clearProtected(); error = '当前身份无权处理回信'"
            />
            <details>
              <summary>查看发送记录与原件</summary>
              <dl>
                <div><dt>消息编号</dt><dd>{{ message.message_id }}</dd></div>
                <div v-if="message.outbound_message_id">
                  <dt>关联出站编号</dt><dd><code>{{ message.outbound_message_id }}</code></dd>
                </div>
                <div>
                  <dt>原件引用</dt><dd>
                    <code>{{ message.raw_artifact_ref }}</code><MessageEvidenceDownload
                      :message-id="message.message_id"
                      @denied="clearProtected(); error = '邮件证据不存在或当前身份无权读取'"
                    />
                  </dd>
                </div>
                <div v-if="message.direction === 'inbound'">
                  <dt>有效分类</dt><dd>{{ categoryLabel(message.effective_category) }}</dd>
                </div>
                <div v-if="message.classified_by">
                  <dt>分类模型 / 版本</dt><dd>{{ message.classified_by }}</dd>
                </div>
              </dl>
            </details>
            <div
              v-if="message.original_category !== message.effective_category"
              class="classification-audit"
            >
              <strong>模型原判：{{ categoryLabel(message.original_category) }}</strong>
              <span>人工纠正后：{{ categoryLabel(message.effective_category) }}</span>
            </div>
            <ul
              v-if="message.corrections.length"
              class="corrections"
            >
              <li
                v-for="correction in message.corrections"
                :key="`${correction.corrected_at}:${correction.corrected_by}`"
              >
                {{ correction.corrected_by }} 于 {{ formatDate(correction.corrected_at) }} 纠正为
                {{ categoryLabel(correction.corrected_category) }}
              </li>
            </ul>
            <div
              v-if="message.required_actions.length"
              class="required-actions"
            >
              <span>规则要求（不代表已执行）</span>
              <ul>
                <li
                  v-for="action in message.required_actions"
                  :key="action"
                >
                  {{ actionLabels[action] ?? action }}
                </li>
              </ul>
            </div>
          </article>
        </section>

        <section
          v-if="latestInbound"
          class="required-actions"
          aria-label="负责人回复工作项"
        >
          <strong>负责人回复工作项</strong>
          <p>由客户当前负责人处理；愿意继续联系不等于已确认采购。</p>
          <p
            v-if="workError"
            role="alert"
          >
            {{ workError }}
          </p>
          <p v-else-if="!replyWork.length">
            当前没有回复工作项
          </p>
          <template
            v-for="section in workSections"
            :key="section.label"
          >
            <h4 v-if="section.items.length">
              {{ section.label }}
            </h4>
            <p v-if="section.label === '处理历史' && section.items.length">
              已完成和已取消各显示最近 100 项，按处理时间倒序。
            </p>
            <article
              v-for="work in section.items"
              :key="work.action_id"
              class="message-card"
            >
              <strong>{{ actionLabels[work.action] ?? work.action }} · {{ workStatusLabels[work.status] }}</strong>
              <span>{{ categoryLabel(detail.messages.find(message => message.message_id === work.message_id)?.effective_category ?? null) }}</span>
              <details @toggle="expandedEvidence[work.action_id] = ($event.target as HTMLDetailsElement).open">
                <summary>查看本工作项的原邮件依据</summary>
                <MessageContent
                  v-if="expandedEvidence[work.action_id]"
                  :message-id="work.message_id"
                  @denied="gate.invalidate(); clearProtected()"
                />
              </details>
              <p v-if="!work.history?.length">
                尚无处理说明记录；旧记录的处理人和时间未补录。
              </p>
              <ol v-else>
                <li
                  v-for="entry in work.history"
                  :key="entry.sequence"
                >
                  <strong>{{ handlingLabels[entry.disposition] ?? entry.disposition }}</strong>
                  <p
                    v-if="entry.note"
                    class="handling-note"
                  >
                    {{ entry.note }}
                  </p>
                  <small>{{ entry.handled_by }} · {{ formatDate(entry.handled_at) }} · 员工处理记录</small>
                </li>
              </ol>
              <div v-if="work.can_handle && workRequests[work.action_id]">
                <p>原处理请求待核对；请先查看已有记录，需要恢复时使用原请求。</p>
                <p class="handling-note">
                  {{ workRequests[work.action_id]?.note }}
                </p>
                <button
                  type="button"
                  :disabled="handling !== null"
                  @click="retryReplyWork(work)"
                >
                  重试原处理
                </button>
              </div>
              <template v-else-if="work.can_handle && ['pending', 'in_progress'].includes(work.status)">
                <button
                  v-if="work.status === 'pending'"
                  type="button"
                  :disabled="handling !== null"
                  @click="handleReplyWork(work, 'started')"
                >
                  开始处理
                </button>
                <label :for="`note-${work.action_id}`">处理说明</label>
                <textarea
                  :id="`note-${work.action_id}`"
                  v-model="workNotes[work.action_id]"
                  aria-label="处理说明"
                  maxlength="2000"
                  rows="3"
                  :disabled="handling !== null"
                  placeholder="记录已做的处理、仍缺的信息和下一步。"
                />
                <p>这是员工处理记录；完成本次处理不会自动确认采购、生成商机或发送邮件。</p>
                <button
                  type="button"
                  :disabled="handling !== null || !(workNotes[work.action_id] ?? '').trim()"
                  @click="handleReplyWork(work, 'waiting_customer')"
                >
                  等待客户补充
                </button>
                <button
                  type="button"
                  :disabled="handling !== null || !(workNotes[work.action_id] ?? '').trim()"
                  @click="handleReplyWork(work, 'handled')"
                >
                  完成本次处理
                </button>
              </template>
              <span v-else-if="['pending', 'in_progress'].includes(work.status)">由当前负责人处理</span>
            </article>
          </template>
        </section>

        <section
          v-if="nextQuestions"
          class="required-actions"
          aria-label="下一问建议"
        >
          <strong>下一问 · 可审阅建议（未保存为正式草稿，未发送）</strong>
          <p v-if="nextQuestions.state === 'need_unavailable'">
            所选消息没有可关联的当前需求证据，建议暂不可用。
          </p>
          <p v-else-if="nextQuestions.state === 'no_missing_fields'">
            当前建议范围没有缺项。
          </p>
          <template v-else>
            <p>确定性完整度：{{ nextQuestions.completeness ?? "暂不可用" }} · 主题：{{ nextQuestions.topics.join("、") }}</p>
            <ul>
              <li
                v-for="suggestion in nextQuestions.suggestions"
                :key="suggestion"
              >
                {{ suggestion }}
              </li>
            </ul>
          </template>
          <RouterLink
            v-if="nextQuestions.need_id"
            :to="`/demand/needs/${nextQuestions.need_id}`"
          >
            查看所选消息关联需求
          </RouterLink>
        </section>
        <p
          v-if="questionsError"
          role="status"
        >
          {{ questionsError }}
        </p>

        <section
          v-if="latestInbound"
          class="correction-panel"
        >
          <div>
            <span>人工纠正分类</span>
            <small>追加留痕，不覆盖模型原判</small>
          </div>
          <select
            v-model="correctionCategory"
            aria-label="纠正后的分类"
          >
            <option
              v-for="option in categoryOptions"
              :key="option[0]"
              :value="option[0]"
            >
              {{ option[1] }}
            </option>
          </select>
          <button
            class="btn-primary"
            type="button"
            :disabled="correcting"
            @click="submitCorrection"
          >
            {{ correcting ? "提交中…" : "提交纠正" }}
          </button>
        </section>
        <p
          v-if="actionMessage"
          class="success"
          role="status"
        >
          {{ actionMessage }}
        </p>
      </main>
      <main
        v-else
        class="conversation-detail empty-detail"
      >
        请选择一条会话
      </main>
    </section>
  </div>
</template>

<style scoped>
.handling-note { white-space: pre-wrap; overflow-wrap: anywhere; }
.message-card textarea { width: 100%; box-sizing: border-box; }

.inbox-layout, .conversation-list, .conversation-detail, .timeline, .message-card { min-width:0; }
.message-card > *, .conversation-detail > header > * { min-width: 0; }
.message-card dd { margin:0; min-width:0; }
.conversation-detail header p, .account, .model { overflow-wrap:anywhere; }
@media(max-width:600px) { .conversation-detail { padding:12px !important; } .message-card dl div { grid-template-columns:1fr !important; } .message-head, .row-top { flex-wrap:wrap; } }
.inbox-shell { max-width: 1500px; }
.inbox-head { justify-content: space-between; align-items: center; }
.eyebrow { color: var(--fact); font-size: 11px; font-weight: 800; letter-spacing: .12em; }
.group-tabs { display: flex; gap: var(--space2); overflow-x: auto; padding-bottom: 2px; }
.group-tabs button { white-space: nowrap; }
.group-tabs button span { margin-left: 6px; color: var(--text-secondary); }
.group-tabs button.active { color: var(--fact); border-color: var(--fact); background: var(--fact-soft); font-weight: 700; }
.inbox-layout { display: grid; grid-template-columns: minmax(280px, 340px) minmax(0, 1fr); gap: var(--space4); min-height: 0; flex: 1; }
.conversation-list, .conversation-detail { min-height: 0; overflow-y: auto; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); }
.conversation-list { display: flex; flex-direction: column; }
.conversation-list > button { border: 0; border-bottom: 1px solid var(--border); border-radius: 0; padding: var(--space4); text-align: left; display: grid; gap: 5px; }
.conversation-list > button:hover, .conversation-list > button.active { background: var(--fact-soft); box-shadow: inset 3px 0 var(--fact); }
.row-top { display: flex; justify-content: space-between; gap: var(--space3); }
.row-top time, .model, .account { color: var(--text-secondary); font-size: 12px; }
.corrected { color: var(--warning); font-size: 12px; font-weight: 700; }
.conversation-detail { padding: var(--space5); display: flex; flex-direction: column; gap: var(--space4); }
.conversation-detail > header { display: flex; justify-content: space-between; gap: var(--space4); border-bottom: 1px solid var(--border); padding-bottom: var(--space4); }
.conversation-detail header h2 { font-size: 17px; overflow-wrap: anywhere; }
.conversation-detail header p, .conversation-detail header > span { color: var(--text-secondary); font-size: 12px; }
.account-label { color: var(--fact); font-size: 10px; font-weight: 800; letter-spacing: .1em; }
.timeline { display: flex; flex-direction: column; gap: var(--space3); }
.message-card { border: 1px solid var(--border); border-left: 4px solid var(--text-secondary); border-radius: var(--radius); padding: var(--space4); display: grid; gap: var(--space3); }
.message-card.inbound { border-left-color: var(--fact); }
.message-head { display: flex; flex-wrap: wrap; justify-content: space-between; gap: var(--space3); }
.message-head time { color: var(--text-secondary); font-size: 12px; }
.message-card dl { display: grid; gap: 6px; }
.message-card dl div { display: grid; grid-template-columns: 130px 1fr; gap: var(--space3); }
.message-card dt { color: var(--text-secondary); }
.message-card dd, code { overflow-wrap: anywhere; }
code { background: var(--canvas); padding: 2px 5px; border-radius: var(--radius-sm); }
.classification-audit { display: flex; gap: var(--space4); flex-wrap: wrap; background: var(--warning-soft); color: var(--warning); padding: var(--space3); border-radius: var(--radius-sm); }
.corrections { padding-left: 20px; color: var(--text-secondary); font-size: 12px; }
.required-actions { background: var(--canvas); border-radius: var(--radius-sm); padding: var(--space3); }
.required-actions > span { font-weight: 700; }
.required-actions ul { display: flex; flex-wrap: wrap; gap: var(--space2); list-style: none; margin-top: var(--space2); }
.required-actions li { border: 1px solid var(--border); background: var(--surface); border-radius: 999px; padding: 2px 8px; font-size: 12px; }
.correction-panel { display: grid; grid-template-columns: 1fr minmax(180px, 260px) auto; gap: var(--space3); align-items: center; background: #edf4ff; border: 1px solid #b2ccff; border-radius: var(--radius); padding: var(--space4); }
.correction-panel > div { display: grid; }
.correction-panel small { color: var(--text-secondary); }
.correction-panel select { border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface); padding: 7px 9px; }
.success { color: var(--fact); background: var(--fact-soft); border-radius: var(--radius-sm); padding: var(--space3); }
.empty, .empty-detail { color: var(--text-secondary); padding: var(--space5); }
@media (max-width: 900px) {
  .inbox-layout { display: flex; flex-direction: column; overflow-y: auto; }
  .conversation-list { flex: none; max-height: 320px; }
  .conversation-detail { flex: none; overflow: visible; }
  .correction-panel { position: static; grid-template-columns: 1fr; }
}
</style>
