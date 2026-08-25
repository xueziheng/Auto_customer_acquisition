<script setup lang="ts">
import { computed, inject, onMounted, ref } from "vue";

import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";

type ApiClient = ReturnType<typeof createApiClient>;
type InboxItem = components["schemas"]["ConversationInboxItem"];
type InboxDetail = components["schemas"]["ConversationInboxDetail"];
type InboxMessage = components["schemas"]["InboxMessageView"];
type ReplyCategory = components["schemas"]["ReplyCategory"];
type GroupKey = "all" | "interest" | "commercial" | "compliance" | "auto" | "ordinary";

const client = inject<ApiClient>("tradeos-api-client", apiClient);
const items = ref<InboxItem[]>([]);
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
};

const categoryOptions = Object.entries(categoryLabels) as Array<[ReplyCategory, string]>;

const groupLabels: Array<{ key: GroupKey; label: string }> = [
  { key: "all", label: "全部" },
  { key: "interest", label: "兴趣与继续沟通" },
  { key: "commercial", label: "报价 / 样品 / 规格" },
  { key: "compliance", label: "退订与投诉" },
  { key: "auto", label: "自动回复" },
  { key: "ordinary", label: "其他回复" },
];

const actionLabels: Record<string, string> = {
  stop_sequence: "停止序列",
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

function groupOf(value: ReplyCategory | null): GroupKey {
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
  if (status === 403) return "当前身份无权访问 Smart Inbox";
  if (status === 503) return "会话服务暂不可用";
  return "请求未完成，请稍后重试";
}

const filteredItems = computed(() => {
  if (selectedGroup.value === "all") return items.value;
  return items.value.filter(
    (item) => groupOf(item.effective_category) === selectedGroup.value,
  );
});

function groupCount(key: GroupKey): number {
  if (key === "all") return items.value.length;
  return items.value.filter((item) => groupOf(item.effective_category) === key).length;
}

const latestInbound = computed<InboxMessage | null>(() => {
  const messages = detail.value?.messages ?? [];
  return [...messages].reverse().find((message) => message.direction === "inbound") ?? null;
});

async function loadItems(): Promise<void> {
  const version = ++listVersion;
  loading.value = true;
  error.value = null;
  try {
    const result = await client.GET("/inbox/conversations", {
      params: { query: { limit: 100 } },
    });
    if (version !== listVersion) return;
    if (result.response.status !== 200 || !result.data) {
      error.value = safeError(result.response.status);
      return;
    }
    items.value = result.data;
    const retained = selectedId.value
      && result.data.some((item) => item.conversation_id === selectedId.value)
      ? selectedId.value
      : result.data[0]?.conversation_id ?? null;
    selectedId.value = retained;
    if (retained) await loadDetail(retained);
    else detail.value = null;
  } catch {
    if (version === listVersion) error.value = "无法连接会话服务";
  } finally {
    if (version === listVersion) loading.value = false;
  }
}

async function loadDetail(conversationId: string, preserveActionMessage = false): Promise<void> {
  const version = ++detailVersion;
  selectedId.value = conversationId;
  detailLoading.value = true;
  if (!preserveActionMessage) actionMessage.value = null;
  try {
    const result = await client.GET("/inbox/conversations/{conversation_id}", {
      params: { path: { conversation_id: conversationId } },
    });
    if (version !== detailVersion) return;
    if (result.response.status === 200 && result.data) {
      detail.value = result.data;
      correctionCategory.value = latestInbound.value?.effective_category ?? "clear_interest";
    } else {
      error.value = safeError(result.response.status);
    }
  } catch {
    if (version === detailVersion) error.value = "会话详情加载失败";
  } finally {
    if (version === detailVersion) detailLoading.value = false;
  }
}

async function submitCorrection(): Promise<void> {
  const message = latestInbound.value;
  if (!message || correcting.value) return;
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
    if (result.response.status !== 200) {
      error.value = safeError(result.response.status);
      return;
    }
    const conversationId = detail.value?.conversation_id ?? selectedId.value;
    if (!conversationId) throw new Error("当前未选择可纠正的会话");
    await loadDetail(conversationId, true);
    actionMessage.value = "人工纠正已记录；模型原判仍保留用于质量评估。";
  } catch {
    error.value = "人工纠正未提交，请稍后重试";
  } finally {
    correcting.value = false;
  }
}

onMounted(() => void loadItems());
</script>

<template>
  <div class="shell inbox-shell">
    <div class="page-head inbox-head">
      <div>
        <p class="eyebrow">
          REPLY CONTROL PLANE
        </p>
        <h1>Smart Inbox</h1>
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
      <div>页面只展示消息元数据与不可变原件引用；动作区是规则要求，不代表工作流已经执行成功。</div>
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
            <strong>{{ categoryLabel(conversation.effective_category) }}</strong>
            <time>{{ formatDate(conversation.latest_message_at) }}</time>
          </span>
          <span class="account">{{ conversation.account_id }}</span>
          <span
            v-if="conversation.original_category !== conversation.effective_category"
            class="corrected"
          >
            模型原判：{{ categoryLabel(conversation.original_category) }}
          </span>
          <span class="model">{{ conversation.classified_by ?? "等待分类" }}</span>
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
            <span class="account-label">ACCOUNT</span>
            <h2>{{ detail.account_id }}</h2>
            <p>{{ detail.conversation_id }} · {{ detail.channel }}</p>
          </div>
          <span>{{ detail.messages.length }} 条消息</span>
        </header>

        <section
          class="timeline"
          aria-label="消息审计时间线"
        >
          <article
            v-for="message in detail.messages"
            :key="message.message_id"
            class="message-card"
            :class="message.direction"
          >
            <div class="message-head">
              <strong>{{ message.direction === "inbound" ? "客户入站" : "系统出站" }}</strong>
              <time>{{ formatDate(message.sent_at) }}</time>
            </div>
            <dl>
              <div><dt>消息 ID</dt><dd>{{ message.message_id }}</dd></div>
              <div v-if="message.outbound_message_id">
                <dt>关联出站 ID</dt><dd><code>{{ message.outbound_message_id }}</code></dd>
              </div>
              <div><dt>原件引用</dt><dd><code>{{ message.raw_artifact_ref }}</code></dd></div>
              <div v-if="message.direction === 'inbound'">
                <dt>有效分类</dt><dd>{{ categoryLabel(message.effective_category) }}</dd>
              </div>
              <div v-if="message.classified_by">
                <dt>分类模型 / 版本</dt><dd>{{ message.classified_by }}</dd>
              </div>
            </dl>
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
.inbox-shell { max-width: 1500px; }
.inbox-head { justify-content: space-between; align-items: center; }
.eyebrow { color: var(--fact); font-size: 11px; font-weight: 800; letter-spacing: .12em; }
.group-tabs { display: flex; gap: var(--space2); overflow-x: auto; padding-bottom: 2px; }
.group-tabs button { white-space: nowrap; }
.group-tabs button span { margin-left: 6px; color: var(--text-secondary); }
.group-tabs button.active { color: var(--fact); border-color: var(--fact); background: var(--fact-soft); font-weight: 700; }
.inbox-layout { display: grid; grid-template-columns: minmax(300px, 380px) 1fr; gap: var(--space4); min-height: 0; flex: 1; }
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
.message-head { display: flex; justify-content: space-between; gap: var(--space3); }
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
.correction-panel { position: sticky; bottom: 0; display: grid; grid-template-columns: 1fr minmax(180px, 260px) auto; gap: var(--space3); align-items: center; background: #edf4ff; border: 1px solid #b2ccff; border-radius: var(--radius); padding: var(--space4); }
.correction-panel > div { display: grid; }
.correction-panel small { color: var(--text-secondary); }
.correction-panel select { border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface); padding: 7px 9px; }
.success { color: var(--fact); background: var(--fact-soft); border-radius: var(--radius-sm); padding: var(--space3); }
.empty, .empty-detail { color: var(--text-secondary); padding: var(--space5); }
@media (max-width: 900px) {
  .inbox-layout { grid-template-columns: 1fr; overflow-y: auto; }
  .conversation-list { max-height: 320px; }
  .correction-panel { grid-template-columns: 1fr; }
}
</style>
