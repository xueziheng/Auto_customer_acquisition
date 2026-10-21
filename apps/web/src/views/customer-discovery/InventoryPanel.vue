<script setup lang="ts">
/* global setInterval, clearInterval, URL, crypto, document, Response */
import { computed, inject, onMounted, onUnmounted, ref } from "vue";
import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";
import { inventoryRecovery } from "./inventoryRecovery";

type Status = components["schemas"]["InventoryStatus"];
type Allocation = components["schemas"]["InventoryAllocationView"];
type Job = components["schemas"]["InventoryJobView"];
type Employee = components["schemas"]["EmployeeView"];
type Assignment = components["schemas"]["InventoryAssignmentRequest"];
const client = inject<ReturnType<typeof createApiClient>>(
  "tradeos-api-client",
  apiClient,
);
const status = ref<Status | null>(null),
  allocations = ref<Allocation[]>([]),
  jobs = ref<Job[]>([]),
  employees = ref<Employee[]>([]);
const selected = ref<string[]>([]),
  target = ref("");
const assigning = ref(false),
  assignmentPending = ref<Assignment | null>(null);
const assignableEmployees = computed(() =>
  employees.value.filter(
    (e) =>
      e.is_active &&
      e.tenant_id === client.identitySnapshot().identity?.tenantId &&
      ["boss", "manager", "sales"].includes(e.role),
  ),
);
type Knowledge = components["schemas"]["KnowledgeDocumentView"];
const knowledge = ref<Knowledge[]>([]), documentIds = ref<string[]>([]);
const intent = ref("");
const knowledgeLoading = ref(false), knowledgeError = ref("");
const regions = ref<NonNullable<components["schemas"]["InventoryQuery"]["regions"]>>([]);
const keywords = ref(""),
  market = ref("all"),
  country = ref(""),
  count = ref(5);
const loading = ref(false),
  claiming = ref(false),
  error = ref(""),
  notice = ref("");
const loadingMore = ref(false),
  nextCursor = ref<string | null>(null);
const pageSize = 100;
const recovery = inventoryRecovery(client);
let pending = recovery.read();
const retrying = ref(Boolean(pending));
if (pending) {
  keywords.value = pending.body.query.product_tags.join(",");
  market.value = pending.body.query.region ?? "all";
  country.value = pending.body.query.country ?? "";
  count.value = pending.body.count;
  intent.value = pending.body.query.intent ?? "";
  regions.value = pending.body.query.regions ?? [];
  country.value = pending.body.query.countries?.join(",") || country.value;
  documentIds.value = pending.body.query.confirmed_refs?.map(ref => ref.document_id) ?? [];
  error.value = recovery.durable()
    ? "请求结果暂未确认，请重试原搜索请求。"
    : "请求结果暂未确认，请重试原搜索请求。浏览器存储不可用，刷新页面无法恢复原请求。";
}
let identityEpoch = 0;
let version = 0,
  disposed = false,
  timer: ReturnType<typeof setInterval> | undefined;
const inProgress = computed(() =>
  jobs.value.some((j) => j.status === "queued" || j.status === "processing"),
);
const jobLabels: Record<string, string> = {
  queued: "等待搜索",
  processing: "正在搜索",
  complete: "已交付，进入待分配",
  failed: "本次搜索已停止",
};
function when(value: string) {
  return new Date(value).toLocaleString("zh-CN", { hour12: false });
}
function safeLink(url: string): string | undefined {
  try {
    const u = new URL(url);
    return ["https:", "http:"].includes(u.protocol) &&
      !u.username &&
      !u.password
      ? u.href
      : undefined;
  } catch {
    return undefined;
  }
}
function stopLabel(reason: string | null | undefined): string {
  return (
    (
      {
        sources_unavailable: "搜索暂不可用，请稍后重试",
        collector_failed: "采集未完成",
        budget_exhausted: "本次采集预算已用完",
        source_unavailable: "来源暂时不可用",
        quota_exhausted: "本公司今日额度已用完",
        model_unavailable: "模型暂时不可用",
        provider_uncertain: "外部请求结果待核对",
        invalid_evidence: "采集证据未通过校验",
      } as Record<string, string>
    )[reason ?? ""] ?? (reason ? "采集未完成，请稍后重新搜索" : "")
  );
}
function clearProtectedData() {
  knowledgeLoading.value = false;
  knowledgeError.value = "";
  knowledge.value = [];
  documentIds.value = [];
  nextCursor.value = null;
  loadingMore.value = false;
  status.value = null;
  allocations.value = [];
  jobs.value = [];
  employees.value = [];
  selected.value = [];
  target.value = "";
  notice.value = "";
  assignmentPending.value = null;
  assigning.value = false;
}
function invalidateProtectedState() {
  identityEpoch++;
  version++;
  if (pending) recovery.clear(pending.key);
  pending = null;
  retrying.value = false;
  clearProtectedData();
  loading.value = false;
  claiming.value = false;
  intent.value = "";
  regions.value = [];
  keywords.value = "";
  market.value = "all";
  country.value = "";
  count.value = 5;
}
function forbidden(message = "当前账号无权访问候选库存") {
  invalidateProtectedState();
  error.value = message;
}
function uncertainNotice() {
  return (
    "请求结果暂未确认，请重试原搜索请求。" +
    (recovery.durable() ? "" : "浏览器存储不可用，刷新页面无法恢复原请求。")
  );
}
async function checked<T extends { response: Response }>(
  request: Promise<T>,
  epoch: number,
  generation: number,
): Promise<T> {
  const result = await request;
  if (
    !disposed && epoch === identityEpoch &&
    generation === client.identitySnapshot().generation &&
    result.response.status === 403
  ) {
    // 同轮其他读取先失败也不能吞掉后来观察到的权限拒绝。
    forbidden();
  }
  return result;
}
async function refresh() {
  if (loading.value) return;
  const current = ++version;
  const epoch = identityEpoch;
  const generation = client.identitySnapshot().generation;
  loading.value = true;
  loadingMore.value = false;
  try {
    const checkedRead = <T extends { response: Response }>(request: Promise<T>) =>
      checked(request, epoch, generation);
    const results = await Promise.all([
      checkedRead(client.GET("/prospects/inventory/status")),
      checkedRead(
        client.GET("/prospects/inventory/allocations", {
          params: { query: { limit: pageSize, unassigned_only: true } },
        }),
      ),
      checkedRead(client.GET("/prospects/inventory/jobs")),
      checkedRead(client.GET("/team/employees")),
    ]);
    if (disposed || current !== version) return;
    const [s, a, j, r] = results;
    if (
      s.response.status !== 200 ||
      a.response.status !== 200 ||
      j.response.status !== 200 ||
      r.response.status !== 200
    ) {
      clearProtectedData();
      error.value = results.some((v) => v.response.status === 403)
        ? "当前账号无权访问候选库存"
        : "候选库存暂不可用，请稍后刷新";
      return;
    }
    status.value = s.data ?? null;
    allocations.value = a.data ?? [];
    jobs.value = j.data ?? [];
    employees.value = r.data ?? [];
    selected.value = [];
    target.value = "";
    nextCursor.value =
      allocations.value.length === pageSize
        ? allocations.value.at(-1)!.id
        : null;
    if (!retrying.value) error.value = "";
  } catch {
    if (!disposed && current === version) {
      clearProtectedData();
      error.value = "候选库存读取失败，请刷新重试";
    }
  } finally {
    if (!disposed && current === version) loading.value = false;
  }
}
async function pollTasks() {
  if (loading.value || loadingMore.value || assigning.value) return;
  const current = ++version;
  const epoch = identityEpoch;
  const generation = client.identitySnapshot().generation;
  loading.value = true;
  try {
    const [s, j] = await Promise.all([
      checked(client.GET("/prospects/inventory/status"), epoch, generation),
      checked(client.GET("/prospects/inventory/jobs"), epoch, generation),
    ]);
    if (disposed || current !== version) return;
    if (s.response.status !== 200 || j.response.status !== 200) {
      error.value = "搜索进度暂不可用，请稍后刷新";
      return;
    }
    // 轮询只读任务/额度，分配窗口在显式刷新或提交后重新核验。
    status.value = s.data ?? null;
    jobs.value = j.data ?? [];
  } catch {
    if (!disposed && current === version)
      error.value = "搜索进度读取失败，请稍后刷新";
  } finally {
    if (!disposed && current === version) loading.value = false;
  }
}
async function loadMore() {
  if (loading.value || loadingMore.value || !nextCursor.value) return;
  const current = ++version;
  const epoch = identityEpoch;
  const generation = client.identitySnapshot().generation;
  loadingMore.value = true;
  try {
    const { data, response } = await client.GET(
      "/prospects/inventory/allocations",
      {
        params: {
          query: {
            limit: pageSize,
            cursor: nextCursor.value,
            unassigned_only: true,
          },
        },
      },
    );
    if (
      disposed ||
      epoch !== identityEpoch ||
      generation !== client.identitySnapshot().generation
    )
      return;
    if (response.status === 403) { forbidden(); return; }
    if (current !== version) return;
    if (response.status !== 200 || !data) {
      clearProtectedData();
      error.value =
        response.status === 403
          ? "当前账号无权访问候选库存"
          : "候选库存暂不可用，请稍后刷新";
      return;
    }
    const known = new Set(allocations.value.map((item) => item.id));
    allocations.value.push(...data.filter((item) => !known.has(item.id)));
    nextCursor.value = data.length === pageSize ? data.at(-1)!.id : null;
    if (!retrying.value) error.value = "";
  } catch {
    if (!disposed && current === version) {
      clearProtectedData();
      error.value = "候选库存读取失败，请刷新重试";
    }
  } finally {
    if (!disposed && current === version) loadingMore.value = false;
  }
}
async function claim() {
  if (claiming.value || !status.value || knowledgeLoading.value || knowledgeError.value) return;
  if (!pending && documentIds.value.length > 5) { error.value = "最多选择5份企业资料"; return; }
  if (!pending) {
    const tags = keywords.value
      .split(/[,，]/)
      .map((v) => v.trim())
      .filter(Boolean);
    if (!tags.length) {
      error.value = "请输入产品关键词";
      return;
    }
    const codes = country.value.split(/[,，\s]+/).filter(Boolean).map(code => code.toUpperCase());
    const code = codes.length === 1 ? codes[0] : "";
    if (codes.some(code => !/^[A-Z]{2}$/.test(code))) {
      error.value = "国家请填写两位代码，如 KE、NG、ZA";
      return;
    }
    pending = {
      key: crypto.randomUUID(),
      body: {
        query: {
          product_tags: tags,
          region: market.value === "africa" ? "africa" : null,
          country: code || null,
          ...(codes.length > 1 ? {countries: codes} : {}),
          ...(regions.value.length ? {regions: regions.value} : {}),
          ...(intent.value.trim() ? {intent: intent.value.trim()} : {}),
          ...(documentIds.value.length ? {confirmed_refs: knowledge.value.filter(doc => documentIds.value.includes(doc.document_id)).map(doc => ({document_id: doc.document_id, revision_id: doc.current_revision_id!}))} : {}),
        },
        count: count.value,
      },
    };
    recovery.save(pending);
  }
  claiming.value = true;
  error.value = "";
  notice.value = "";
  const current = identityEpoch;
  const generation = client.identitySnapshot().generation;
  const request = pending;
  try {
    const { data, response } = await client.POST(
      "/prospects/inventory/requests",
      {
        body: request.body,
        params: { header: { "Idempotency-Key": request.key } },
      },
    );
    if (
      current !== identityEpoch ||
      generation !== client.identitySnapshot().generation
    )
      return;
    if (
      (response.status === 202 && data) ||
      [400, 403, 409, 429].includes(response.status)
    )
      recovery.clear(request.key);
    if (disposed) return;
    if (response.status === 202 && data) {
      pending = null;
      retrying.value = false;
      notice.value = data.job
        ? `提交时交付 ${data.allocations.length} 家客户，进入待分配；其余正在搜索。`
        : `本次交付 ${data.allocations.length} 家客户，已进入待分配。`;
      const deliveredNotice = notice.value;
      await refresh();
      if (!disposed && current === identityEpoch && status.value)
        notice.value = deliveredNotice;
    } else if ([400, 403, 409, 429].includes(response.status)) {
      if (response.status === 403) {
        forbidden("当前账号没有搜索权限");
        return;
      }
      error.value =
        response.status === 403
          ? "当前账号没有搜索权限"
          : response.status === 409
            ? "请求已变化，请刷新后重新搜索"
            : response.status === 429
              ? "今日搜索额度已用完"
              : "请求参数无效，请检查关键词和数量";
      pending = null;
      retrying.value = false;
    } else {
      retrying.value = true;
      error.value = uncertainNotice();
    }
  } catch {
    if (!disposed && current === identityEpoch) {
      retrying.value = true;
      error.value = uncertainNotice();
    }
  } finally {
    if (!disposed && current === identityEpoch) claiming.value = false;
  }
}
function toggle(id: string, checked: boolean) {
  if (assigning.value || assignmentPending.value) return;
  selected.value = checked
    ? [...new Set([...selected.value, id])].slice(0, 100)
    : selected.value.filter((v) => v !== id);
}
function selectLoaded(checked: boolean) {
  if (assigning.value || assignmentPending.value) return;
  selected.value = checked
    ? allocations.value
        .filter((a) => !a.employee_id)
        .slice(0, 100)
        .map((a) => a.id)
    : [];
}
async function assign() {
  if (assigning.value) return;
  if (!assignmentPending.value) {
    if (
      !selected.value.length ||
      !assignableEmployees.value.some((e) => e.employee_id === target.value)
    )
      return;
    assignmentPending.value = {
      allocation_ids: [...selected.value],
      employee_id: target.value,
    };
  }
  const body = assignmentPending.value,
    epoch = identityEpoch,
    generation = client.identitySnapshot().generation;
  assigning.value = true;
  error.value = "";
  notice.value = "";
  try {
    const { response } = await client.POST("/prospects/inventory/assignments", {
      body,
    });
    if (
      disposed ||
      epoch !== identityEpoch ||
      generation !== client.identitySnapshot().generation
    )
      return;
    if (response.ok) {
      assignmentPending.value = null;
      selected.value = [];
      target.value = "";
      await refresh();
      if (!disposed && epoch === identityEpoch && status.value)
        notice.value = `已分配 ${body.allocation_ids.length} 家客户，可在负责人的“我的客户”查看。`;
    } else if ([400, 403, 404, 409].includes(response.status)) {
      assignmentPending.value = null;
      if (response.status === 403) {
        forbidden("当前无法分配，请核对员工权限和客户记录。");
        return;
      }
      await refresh();
      if (disposed || epoch !== identityEpoch || generation !== client.identitySnapshot().generation) return;
      error.value =
        response.status === 409
          ? "客户归属已变化，请核对待分配列表；不能覆盖他人归属。"
          : "当前无法分配，请核对员工权限和客户记录。";
    } else error.value = "分配结果未知，请刷新核对，或显式核对原分配。";
  } catch {
    if (!disposed && epoch === identityEpoch)
      error.value = "分配结果未知，请刷新核对，或显式核对原分配。";
  } finally {
    if (!disposed && epoch === identityEpoch) assigning.value = false;
  }
}
const unsubscribe = client.subscribeIdentity(() => {
  invalidateProtectedState();
  error.value = "";
});
async function loadKnowledge() {
  knowledgeLoading.value = true;
  knowledgeError.value = "";
  const epoch = identityEpoch, generation = client.identitySnapshot().generation;
  try {
    const {data, response} = await client.GET("/knowledge/documents", {params: {query: {limit: 100}}});
    if (disposed || epoch !== identityEpoch || generation !== client.identitySnapshot().generation) return;
    if (response.status === 403) { forbidden(); return; }
    if (!response.ok || !Array.isArray(data?.items)) { knowledgeError.value = "企业资料列表暂不可用，请刷新重试"; return; }
    knowledge.value = response.ok && Array.isArray(data?.items) ? data.items.filter(doc => doc.tenant_id === client.identitySnapshot().identity?.tenantId && doc.status === "confirmed" && doc.current_revision_id) : [];
    if (!pending) documentIds.value = knowledge.value.slice(0,5).map(doc => doc.document_id);
  } catch { if (!disposed && epoch === identityEpoch) { knowledge.value = []; knowledgeError.value = "企业资料列表暂不可用，请刷新重试"; } }
  finally { if (!disposed && epoch === identityEpoch && generation === client.identitySnapshot().generation) knowledgeLoading.value = false; }
}
function refreshAll() { void loadKnowledge(); void refresh(); }
onMounted(() => {
  refreshAll();
  timer = setInterval(() => {
    if (
      inProgress.value &&
      document.visibilityState !== "hidden" &&
      !claiming.value &&
      !assigning.value &&
      !loadingMore.value
    )
      void pollTasks();
  }, 10000);
});
onUnmounted(() => {
  disposed = true;
  version++;
  unsubscribe();
  if (timer) clearInterval(timer);
});
</script>

<template>
  <section
    class="inventory-panel"
    aria-label="候选库存"
  >
    <div class="inventory-heading">
      <div>
        <h2>搜索新客户</h2>
        <p>
          搜索结果进入公司待分配列表，分配后由负责人跟进。公开邮箱仍需验证；分配不重复消耗额度。
        </p>
      </div>
      <button
        type="button"
        :disabled="loading || claiming"
        @click="refreshAll"
      >
        {{ loading ? "刷新中…" : "刷新列表" }}
      </button>
    </div>
    <div
      v-if="status"
      class="inventory-metrics"
      aria-label="公司今日额度"
    >
      <div>
        <strong>{{ status.remaining_today }}</strong><span>今日剩余</span>
      </div>
      <div>
        <strong>{{ status.used_today }} / {{ status.daily_limit }}</strong><span>本公司今日已取得</span>
      </div>
      <p>额度重置：{{ when(status.reset_at) }}</p>
    </div>
    <form
      class="inventory-form"
      @submit.prevent="claim"
    >
      <label class="inventory-field-product">产品关键词<input
        v-model="keywords"
        aria-label="产品关键词"
        placeholder="例如：三轮车、tricycle"
        maxlength="500"
        :disabled="claiming || retrying"
        required
      ></label>
      <label>目标市场<select
        v-model="market"
        aria-label="目标市场"
        :disabled="claiming || retrying"
      >
        <option value="all">不限地区</option>
        <option value="africa">非洲</option>
      </select></label>
      <label>更多目标地区<select
        v-model="regions"
        aria-label="更多目标地区"
        multiple
        :disabled="claiming || retrying"
      >
        <option value="africa">非洲</option><option value="asia">亚洲</option><option value="europe">欧洲</option><option value="americas">美洲</option><option value="oceania">大洋洲</option>
      </select></label>
      <label class="inventory-field-knowledge">已确认企业资料<select
        v-model="documentIds"
        aria-label="已确认企业资料"
        multiple
        :disabled="claiming || retrying"
      >
        <option
          v-for="doc in knowledge"
          :key="doc.document_id"
          :value="doc.document_id"
        >{{ doc.source.filename }}</option>
      </select><small>最多5份，仅作为内部匹配上下文；确认不代表正式产品发布。未选择时按公开关键词搜索。</small></label>
      <label class="inventory-field-intent">搜索要求<textarea
        v-model="intent"
        aria-label="搜索要求"
        maxlength="2000"
        placeholder="例如：优先寻找有维修网络的经销商"
        :disabled="claiming || retrying"
      /></label>
      <label class="inventory-field-country">国家代码（可选，多国用逗号）<input
        v-model="country"
        aria-label="国家代码"
        placeholder="如 KE / NG / ZA"
        maxlength="149"
        :disabled="claiming || retrying"
      ></label>
      <label>搜索数量<input
        v-model.number="count"
        aria-label="搜索数量"
        type="number"
        min="1"
        max="100"
        :disabled="claiming || retrying"
        required
      ></label>
      <button
        type="submit"
        :disabled="
          claiming || knowledgeLoading || !!knowledgeError || !status || (!retrying && status.remaining_today < 1)
        "
      >
        {{ claiming ? "提交中…" : retrying ? "重试原搜索请求" : "开始搜索" }}
      </button>
    </form>
    <p
      v-if="knowledgeError"
      role="alert"
    >
      {{ knowledgeError }}
    </p>
    <p v-if="!knowledgeLoading && !knowledgeError && !knowledge.length">
      当前没有已确认资料，本次按公开关键词搜索；可先在产品资料页上传并确认。
    </p>
    <p
      v-if="error"
      class="inventory-error"
      role="alert"
    >
      {{ error }}
    </p>
    <p
      v-if="notice"
      role="status"
    >
      {{ notice }}
    </p>
    <div
      v-if="inProgress"
      class="inventory-jobs"
      aria-label="当前搜索"
    >
      <h3>当前搜索</h3>
      <article
        v-for="job in jobs.filter((j) =>
          ['queued', 'processing'].includes(j.status),
        )"
        :key="job.id"
      >
        <strong>{{ job.query.product_tags.join("、") }}</strong><span>{{ jobLabels[job.status] }} · 还需 {{ job.remaining_count }} 家</span>
      </article>
    </div>
    <details
      v-if="jobs.some((j) => !['queued', 'processing'].includes(j.status))"
      class="inventory-jobs"
    >
      <summary>最近搜索</summary>
      <article
        v-for="job in jobs.filter(
          (j) => !['queued', 'processing'].includes(j.status),
        )"
        :key="job.id"
      >
        <strong>{{ job.query.product_tags.join("、") }}</strong><span>{{ jobLabels[job.status] }}</span><small v-if="job.failure_reason">{{
          stopLabel(job.failure_reason)
        }}</small>
      </article>
    </details>
    <div
      class="assignment-bar"
      aria-label="批量分配"
    >
      <label><input
        type="checkbox"
        :checked="selected.length > 0"
        :disabled="assigning || !!assignmentPending"
        @change="selectLoaded(($event.target as HTMLInputElement).checked)"
      >选择当前已加载范围（最多100条）</label>
      <span>已选择 {{ selected.length }} 家</span>
      <select
        v-model="target"
        aria-label="分配给员工"
        :disabled="assigning || !!assignmentPending"
      >
        <option value="">
          选择负责人
        </option>
        <option
          v-for="e in assignableEmployees"
          :key="e.employee_id"
          :value="e.employee_id"
        >
          {{ e.name }}
        </option>
      </select>
      <button
        :disabled="
          assigning || (!assignmentPending && (!selected.length || !target))
        "
        @click="assign"
      >
        {{
          assigning ? "分配中…" : assignmentPending ? "核对原分配" : "分配所选"
        }}
      </button>
    </div>
    <div
      class="inventory-results"
      aria-label="待分配客户"
    >
      <h3>
        待分配客户 <small>{{ allocations.length }} 家</small>
      </h3>
      <p v-if="!allocations.length && status">
        暂无待分配客户。输入产品和目标市场后开始搜索。
      </p>
      <article
        v-for="item in allocations"
        :key="item.id"
        class="inventory-candidate"
      >
        <div class="inventory-candidate-head">
          <label><input
                   type="checkbox"
                   :aria-label="`选择 ${item.candidate.name}`"
                   :checked="selected.includes(item.id)"
                   :disabled="
                     !!item.employee_id ||
                       assigning ||
                       !!assignmentPending ||
                       (!selected.includes(item.id) && selected.length >= 100)
                   "
                   @change="
                     toggle(item.id, ($event.target as HTMLInputElement).checked)
                   "
                 >
            <h4>{{ item.candidate.name }}</h4></label><span>{{ item.candidate.country }}</span>
        </div>
        <a
          :href="safeLink('https://' + item.candidate.canonical_domain)"
          target="_blank"
          rel="noopener noreferrer"
        >{{ item.candidate.canonical_domain }}</a>
        <p>{{ item.candidate.product_tags.join(" · ") }}</p>
        <p class="inventory-contact">
          {{ item.candidate.public_emails.join("；") || "尚未发现公开邮箱"
          }}<span
            v-if="item.candidate.public_emails.length"
            class="unverified"
          >未验证可达性</span>
        </p>
        <small>领取于 {{ when(item.allocated_at) }} ·
          {{
            new Date(item.candidate.expires_at).getTime() <= Date.now()
              ? "资料已过期，需复核"
              : "公开资料，需求尚未验证"
          }}</small>
        <details>
          <summary>查看来源证据</summary>
          <p>{{ item.candidate.evidence_text }}</p>
          <a
            :href="safeLink(item.candidate.source_url)"
            target="_blank"
            rel="noopener noreferrer"
          >打开原始来源</a>
          <p>观察时间：{{ when(item.candidate.observed_at) }}</p>
        </details>
      </article>
    </div>
    <button
      v-if="nextCursor"
      type="button"
      :disabled="loading || loadingMore"
      @click="loadMore"
    >
      {{ loadingMore ? "加载中…" : "加载更多待分配客户" }}
    </button>
  </section>
</template>

<style scoped>
.assignment-bar {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  align-items: center;
}
.assignment-bar select {
  max-width: 100%;
  min-width: 0;
}
.inventory-candidate-head label {
  display: flex;
  gap: 8px;
  align-items: start;
}
.inventory-panel {
  display: grid;
  gap: 20px;
  margin-bottom: 28px;
  padding: 24px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  min-width: 0;
}
.inventory-heading {
  display: flex;
  justify-content: space-between;
  align-items: start;
  gap: 16px;
}
.inventory-heading h2 {
  margin: 0 0 8px;
}
.inventory-heading p,
.inventory-metrics p {
  color: var(--muted);
  margin: 0;
  font-size: 13px;
  line-height: 1.6;
}
.inventory-panel button {
  padding: 9px 14px;
  white-space: nowrap;
}
.inventory-metrics {
  display: flex;
  gap: 24px;
  flex-wrap: wrap;
  align-items: center;
  padding: 16px;
  background: var(--surface-soft, #f7f9fb);
  border-radius: 8px;
}
.inventory-metrics div {
  display: grid;
  gap: 5px;
}
.inventory-metrics strong {
  font-size: 23px;
}
.inventory-metrics span,
.inventory-candidate small {
  font-size: 12px;
  color: var(--muted);
}
.inventory-form {
  display: grid;
  grid-template-columns: repeat(12, minmax(0, 1fr));
  gap: 12px;
  align-items: start;
}
.inventory-form label {
  display: grid;
  gap: 7px;
  font-size: 13px;
  min-width: 0;
}
.inventory-form > label,
.inventory-form > button {
  grid-column: span 2;
}
.inventory-form > .inventory-field-product,
.inventory-form > .inventory-field-country {
  grid-column: span 3;
}
.inventory-form > .inventory-field-knowledge,
.inventory-form > .inventory-field-intent {
  grid-column: span 5;
}
.inventory-form select[multiple],
.inventory-form textarea {
  min-height: 112px;
}
.inventory-form > button {
  margin-top: 22px;
}
.inventory-form input,
.inventory-form select,
.inventory-form textarea {
  padding: 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--surface);
  color: inherit;
  width: 100%;
  min-width: 0;
  box-sizing: border-box;
}
.inventory-form button {
  background: var(--action);
  color: white;
  border: 0;
  border-radius: 6px;
  min-height: 39px;
}
.inventory-form button:disabled {
  opacity: 0.5;
}
.inventory-error {
  color: var(--danger, #b42318);
}
.inventory-jobs article {
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
  border-top: 1px solid var(--border);
  padding: 10px 0;
}
.inventory-results {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(100%, 290px), 1fr));
  gap: 12px;
}
.inventory-results > h3,
.inventory-results > p {
  grid-column: 1/-1;
}
.inventory-results h3 {
  margin: 4px 0;
}
.inventory-results h3 small {
  font-weight: 400;
  color: var(--muted);
}
.inventory-candidate {
  padding: 16px;
  border: 1px solid var(--border);
  border-radius: 8px;
  overflow-wrap: anywhere;
  min-width: 0;
}
.inventory-candidate-head {
  display: flex;
  justify-content: space-between;
  gap: 12px;
}
.inventory-candidate h4 {
  margin: 0 0 10px;
  font-size: 16px;
}
.inventory-candidate p {
  font-size: 13px;
  line-height: 1.6;
}
.inventory-candidate details {
  margin-top: 12px;
}
.inventory-contact {
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.unverified {
  color: #985f00;
  font-size: 12px;
}
.inventory-sources {
  border-top: 1px solid var(--border);
  padding-top: 16px;
}
.inventory-panel summary {
  cursor: pointer;
  font-size: 13px;
}
.inventory-source-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 230px), 1fr));
  gap: 12px;
}
.inventory-source-grid article {
  display: grid;
  gap: 7px;
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: 6px;
}
.inventory-source-grid span,
.inventory-source-grid small {
  font-size: 12px;
  color: var(--muted);
}
.inventory-source-grid .available {
  color: #16734b;
}
@media (max-width: 800px) {
  .inventory-form {
    grid-template-columns: minmax(0, 1fr);
  }
  .inventory-form > label,
  .inventory-form > .inventory-field-product,
  .inventory-form > .inventory-field-country,
  .inventory-form > .inventory-field-knowledge,
  .inventory-form > .inventory-field-intent,
  .inventory-form > button {
    grid-column: 1/-1;
  }
  .inventory-form > button {
    margin-top: 0;
  }
  .inventory-form button {
    grid-column: 1/-1;
  }
  .assignment-bar {
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
    align-items: center;
  }
  .assignment-bar select {
    max-width: 100%;
    min-width: 0;
  }
  .inventory-candidate-head label {
    display: flex;
    gap: 8px;
    align-items: start;
  }
  .inventory-panel {
    padding: 16px;
  }
  .inventory-heading {
    flex-wrap: wrap;
  }
  .inventory-metrics {
    gap: 16px;
  }
}
@media (max-width: 420px) {
  .inventory-form {
    grid-template-columns: 1fr;
  }
  .inventory-metrics strong {
    font-size: 20px;
  }
}
</style>
