<script setup lang="ts">
/* global HTMLElement, HTMLInputElement, HTMLTextAreaElement, HTMLButtonElement, KeyboardEvent, URL, document, setInterval, clearInterval */
import { computed, nextTick, onMounted, onUnmounted, ref } from "vue";
import { platformClient, PlatformDataSourceError, type PlatformDataSource, type PlatformDataSources as SourcesView } from "../api/platform-auth";
import DataSourceConnection from "./DataSourceConnection.vue";
import DataSourceUsage from "./DataSourceUsage.vue";

const view = ref<SourcesView | null>(null);
const loading = ref(false), saving = ref(false);
const error = ref(""), notice = ref("");
const checking = ref<string | null>(null);
const browserFailures = ref<Record<string, string>>({});
const usageFailures = ref<Record<string, string>>({});
let usageTimer: ReturnType<typeof setInterval> | undefined;
const usageAlerts = computed(() => (view.value?.sources ?? []).filter(source => source.configured && source.can_check_usage
  && ["low", "partial_exhausted", "exhausted", "payment_required", "credential_invalid", "rate_limited"].includes(source.usage?.status ?? "")));
const query = ref(""), category = ref(""), selection = ref("");
const editing = ref<PlatformDataSource | null>(null);
const deleting = ref<PlatformDataSource | null>(null);
const noteEditing = ref<PlatformDataSource | null>(null);
const noteText = ref(""), noteError = ref("");
const credentials = ref<Record<string, string>>({});
const dialog = ref<HTMLElement | null>(null);
let generation = 0;
let focusBeforeEdit: HTMLElement | null = null;
let identityRevision = platformClient.snapshot().revision;
const integrationLabels = { available: "已有接入", partial: "部分已接入", planned: "接口待接入" };
const selectionLabels = { selected: "已选来源", backup: "补充备选", example: "目录示例" };
const credentialModeLabels = { public: "公开访问", api_key: "API Key", oauth_client: "客户端授权", access_token: "访问令牌", commercial_pending: "待商务开通", managed_session: "部署会话", authentication_pending: "认证方式待确认" };
const categories = computed(() => [...new Set(view.value?.sources.map(source => source.definition.category) ?? [])]);
const filtered = computed(() => (view.value?.sources ?? []).filter(source => {
  const item = source.definition;
  const text = [item.name, item.category, item.coverage, item.description, source.note?.text, ...item.access_methods].join(" ").toLocaleLowerCase();
  return (!category.value || item.category === category.value)
    && (!selection.value || item.selection === selection.value)
    && (!query.value.trim() || text.includes(query.value.trim().toLocaleLowerCase()));
}));
const canSave = computed(() => !saving.value && Boolean(editing.value?.can_edit)
  && Boolean(editing.value?.definition.credential_fields.length)
  && editing.value!.definition.credential_fields.every(field => Boolean(credentials.value[field.name]?.trim())));
const groups = computed(() => categories.value.map(name => ({ name, sources: filtered.value.filter(source => source.definition.category === name) })).filter(group => group.sources.length));
const canSaveNote = computed(() => !saving.value && Boolean(noteEditing.value?.can_edit_note)
  && noteText.value.length <= 2000 && noteText.value.trim() !== (noteEditing.value?.note?.text ?? ""));

function clearEditor(): void {
  credentials.value = {};
  editing.value = null;
  deleting.value = null;
  noteEditing.value = null; noteText.value = ""; noteError.value = "";
  saving.value = false;
}
function resetIdentity(): void {
  generation += 1;
  clearEditor();
  view.value = null;
  query.value = ""; category.value = ""; selection.value = "";
  error.value = ""; notice.value = ""; loading.value = false;
  focusBeforeEdit = null;
  checking.value = null; usageFailures.value = {}; browserFailures.value = {};
}
const unsubscribe = platformClient.subscribe(() => {
  const next = platformClient.snapshot();
  if (next.revision !== identityRevision || next.mutation || !next.identity) {
    identityRevision = next.revision;
    resetIdentity();
  }
});
function valid(expected: number, revision: number): boolean {
  const state = platformClient.snapshot();
  return generation === expected && state.revision === revision && Boolean(state.identity) && state.mutation === null;
}
function publicLink(value: string | null | undefined): string | undefined {
  if (!value) return undefined;
  try { const url = new URL(value); return url.protocol === "https:" && !url.username && !url.password ? url.href : undefined; }
  catch { return undefined; }
}
function testLabel(source: PlatformDataSource): string {
  if (source.test_status === "not_run") return "尚未测试";
  const scope = source.last_test_scope === "account" ? "账户验证" : source.last_test_scope === "sample" ? "数据样本测试" : "最近测试";
  const outcome = { passed: "通过", failed: "未通过", inconclusive: "结果不确定" };
  return scope + outcome[source.test_status];
}
function timestamp(value: string | null | undefined): string {
  if (!value) return "尚无记录";
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date.toLocaleString("zh-CN") : "时间未知";
}
async function load(): Promise<void> {
  if (saving.value || checking.value) return;
  const expected = ++generation, revision = platformClient.snapshot().revision;
  clearEditor(); loading.value = true; error.value = "";
  try {
    const result = await platformClient.dataSources();
    if (valid(expected, revision)) view.value = result;
  } catch (failure) {
    if (!valid(expected, revision)) return;
    view.value = null;
    error.value = failure instanceof PlatformDataSourceError && failure.status === 403
      ? "当前平台账号无权管理数据来源。" : "数据来源暂不可用，请刷新重试。";
  } finally {
    if (valid(expected, revision)) {
      loading.value = false;
      void refreshUsage();
    }
  }
}
async function checkBrowser(source: PlatformDataSource): Promise<void> {
  if (checking.value || saving.value || loading.value || noteEditing.value || !source.can_check_browser) return;
  const id = source.definition.source_id, expected = generation, revision = platformClient.snapshot().revision;
  checking.value = id; delete browserFailures.value[id];
  try {
    const result = await platformClient.checkDataSourceBrowser(id);
    if (!valid(expected, revision)) return;
    if (view.value) view.value.sources = view.value.sources.map(item => item.definition.source_id === id ? result : item);
  } catch {
    if (valid(expected, revision)) browserFailures.value[id] = "本次测试未完成，请稍后重试。";
  } finally { if (valid(expected, revision)) checking.value = null; }
}
async function checkUsage(source: PlatformDataSource): Promise<void> {
  if (checking.value || saving.value || loading.value || noteEditing.value || !source.configured || !source.can_check_usage) return;
  const id = source.definition.source_id, expected = generation, revision = platformClient.snapshot().revision;
  checking.value = id; delete usageFailures.value[id];
  try {
    const result = await platformClient.checkDataSourceUsage(id, { expected_revision: source.revision });
    if (valid(expected, revision) && view.value) {
      view.value.sources = view.value.sources.map(item => item.definition.source_id === id && item.revision === result.revision ? result : item);
    }
  } catch (failure) {
    if (valid(expected, revision)) {
      if (failure instanceof PlatformDataSourceError && failure.status === 403) view.value = null;
      else usageFailures.value[id] = failure instanceof PlatformDataSourceError && failure.status === 409
        ? "配置已更新，请刷新来源状态。" : "本次检查未完成，上次记录不代表当前额度。";
    }
  } finally { if (valid(expected, revision)) checking.value = null; }
}
async function refreshUsage(): Promise<void> {
  if (saving.value || checking.value || loading.value || editing.value || deleting.value || noteEditing.value || document.visibilityState === "hidden") return;
  const expected = generation;
  for (const source of view.value?.sources ?? []) {
    if (expected !== generation) return;
    if (source.configured && source.can_check_usage) await checkUsage(source);
  }
}
async function edit(source: PlatformDataSource): Promise<void> {
  if (!source.can_edit || !source.definition.credential_fields.length || saving.value || loading.value) return;
  focusBeforeEdit = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  clearEditor(); notice.value = ""; error.value = "";
  editing.value = source;
  credentials.value = Object.fromEntries(source.definition.credential_fields.map(field => [field.name, ""]));
  await nextTick(); dialog.value?.querySelector<HTMLInputElement>("input")?.focus();
}
function cancel(): void {
  if (saving.value) return;
  clearEditor();
  if (focusBeforeEdit?.isConnected) focusBeforeEdit.focus();
  focusBeforeEdit = null;
}
async function editNote(source: PlatformDataSource): Promise<void> {
  if (!source.can_edit_note || saving.value || loading.value || checking.value) return;
  focusBeforeEdit = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  clearEditor(); notice.value = ""; error.value = "";
  noteEditing.value = source; noteText.value = source.note?.text ?? "";
  await nextTick(); dialog.value?.querySelector<HTMLTextAreaElement>("textarea")?.focus();
}
async function saveNote(): Promise<void> {
  if (!canSaveNote.value || !noteEditing.value) return;
  const source = noteEditing.value, expected = ++generation, revision = platformClient.snapshot().revision;
  saving.value = true; noteError.value = "";
  try {
    const note = await platformClient.saveDataSourceNote(source.definition.source_id, {
      expected_revision: source.note?.revision ?? 0, text: noteText.value,
    });
    if (!valid(expected, revision)) return;
    if (view.value) view.value.sources = view.value.sources.map(item => item.definition.source_id === source.definition.source_id ? { ...item, note } : item);
    clearEditor(); notice.value = `${source.definition.name} 备注已${note.text ? '保存' : '清空'}。`;
    if (focusBeforeEdit?.isConnected) focusBeforeEdit.focus();
  } catch (failure) {
    if (!valid(expected, revision)) return;
    const status = failure instanceof PlatformDataSourceError ? failure.status : 0;
    if (status === 401 || status === 403) {
      clearEditor(); view.value = null; error.value = "当前会话无权修改来源备注，请重新登录。";
    } else {
      noteError.value = status === 409 ? "备注已被其他管理员更新，请取消编辑并刷新后重试。"
        : status === 400 || status === 422 ? "备注格式无效，请使用 2000 字以内的普通文本。"
          : "尚不能确认备注是否保存。草稿已保留，请取消编辑并刷新核对后重试。";
    }
  } finally { if (valid(expected, revision)) saving.value = false; }
}
async function confirmDelete(source: PlatformDataSource): Promise<void> {
  if (!source.can_edit || !source.configured || !source.definition.credential_fields.length || saving.value || loading.value) return;
  focusBeforeEdit = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  clearEditor(); notice.value = ""; error.value = ""; deleting.value = source;
  await nextTick(); dialog.value?.querySelector<HTMLButtonElement>("button")?.focus();
}
async function remove(): Promise<void> {
  if (saving.value || !deleting.value?.can_edit || !deleting.value.configured) return;
  const source = deleting.value;
  const expected = ++generation, revision = platformClient.snapshot().revision;
  saving.value = true; error.value = ""; notice.value = "";
  try {
    const result = await platformClient.deleteDataSourceCredentials(source.definition.source_id, { expected_revision: source.revision });
    if (!valid(expected, revision)) return;
    if (view.value) view.value.sources = view.value.sources.map(item => item.definition.source_id === result.definition.source_id ? result : item);
    clearEditor(); notice.value = `${source.definition.name} 密钥已从平台配置中删除。`;
  } catch (failure) {
    if (!valid(expected, revision)) return;
    clearEditor();
    const status = failure instanceof PlatformDataSourceError ? failure.status : 0;
    if (status === 409) {
      const recovery = generation + 1;
      await load();
      if (valid(recovery, revision)) error.value = "配置已更新，请核对当前状态后重新操作。";
    } else {
      if (status === 403) view.value = null;
      error.value = status === 403 ? "当前平台账号无权修改数据来源。" : "尚不能确认密钥是否删除，请刷新核对当前配置。";
    }
  } finally { if (valid(expected, revision)) saving.value = false; }
}
function dialogKeys(event: KeyboardEvent): void {
  if (event.key === "Escape") { event.preventDefault(); cancel(); }
  if (event.key !== "Tab") return;
  const elements = [...dialog.value?.querySelectorAll<HTMLElement>("input:not(:disabled), textarea:not(:disabled), button:not(:disabled)") ?? []];
  const first = elements[0], last = elements.at(-1);
  if (!first) { event.preventDefault(); return; }
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
}
async function save(): Promise<void> {
  if (!canSave.value || !editing.value) return;
  const source = editing.value;
  const expected = ++generation, revision = platformClient.snapshot().revision;
  saving.value = true; error.value = ""; notice.value = "";
  const pending = platformClient.saveDataSourceCredentials(source.definition.source_id, {
    expected_revision: source.revision, credentials: { ...credentials.value },
  });
  credentials.value = {};
  try {
    const result = await pending;
    if (!valid(expected, revision)) return;
    if (view.value) view.value.sources = view.value.sources.map(item => item.definition.source_id === result.definition.source_id ? result : item);
    clearEditor();
    notice.value = `${source.definition.name} 密钥已保存，尚未测试新配置。保存不会自动启用数据采集。`;
    if (focusBeforeEdit?.isConnected) focusBeforeEdit.focus();
  } catch (failure) {
    if (!valid(expected, revision)) return;
    clearEditor();
    const status = failure instanceof PlatformDataSourceError ? failure.status : 0;
    if (status === 409) {
      const recovery = generation + 1;
      await load();
      if (valid(recovery, revision)) error.value = "配置已更新，请核对当前状态后重新输入密钥。";
    } else {
      if (status === 403) view.value = null;
      error.value = status === 403 ? "当前平台账号无权修改数据来源。"
        : status === 422 ? "密钥格式不符合要求，请重新输入完整凭证。"
          : "尚不能确认密钥是否保存，请先刷新核对配置版本，再重新输入。";
    }
  } finally { if (valid(expected, revision)) saving.value = false; }
}
onMounted(() => {
  void load();
  usageTimer = setInterval(() => void refreshUsage(), 300_000);
  document.addEventListener("visibilitychange", refreshUsage);
});
onUnmounted(() => { clearInterval(usageTimer); document.removeEventListener("visibilitychange", refreshUsage); unsubscribe(); resetIdentity(); });
</script>

<template>
  <section
    class="sources-panel"
    aria-labelledby="sources-title"
  >
    <header class="sources-heading">
      <div>
        <p class="phase-eyebrow">
          平台管理员
        </p>
        <h1 id="sources-title">
          获客数据来源
        </h1>
        <p class="meta">
          统一查看企业、联系人、采购、贸易和公开网页来源，维护平台使用的 API 凭证。
        </p>
      </div>
      <button
        type="button"
        :disabled="loading || saving || Boolean(checking)"
        @click="load"
      >
        {{ loading ? '正在读取…' : '刷新来源状态' }}
      </button>
    </header>
    <p class="sources-hint">
      已配置密钥不等于接口已接通；接入状态、密钥配置和最近测试分别展示。公开网页与下载来源无需填写 API 密钥。
    </p>
    <p class="sources-hint">
      已支持的额度查询在此页面打开时自动检查，可见时每 5 分钟更新；短时间重复检查使用缓存。当前已接入 Hunter 免费账户查询。
    </p>
    <p
      v-if="usageAlerts.length"
      class="safe-banner usage-alert"
      role="status"
    >
      额度或密钥需要关注：{{ usageAlerts.map(source => source.definition.name).join('、') }}。请查看对应来源的额度状态与检查时间。
    </p>
    <p
      v-if="error"
      class="safe-banner danger"
      role="alert"
    >
      {{ error }}
    </p>
    <p
      v-if="notice"
      class="safe-banner"
      role="status"
    >
      {{ notice }}
    </p>
    <p
      v-if="loading"
      role="status"
    >
      正在读取数据来源…
    </p>
    <template v-if="view">
      <div class="source-filters">
        <label>搜索来源<input
          v-model="query"
          type="search"
          placeholder="名称、国家、获取方式…"
        ></label>
        <label>类别<select v-model="category"><option value="">全部类别</option><option
          v-for="item in categories"
          :key="item"
          :value="item"
        >{{ item }}</option></select></label>
        <label>选择范围<select v-model="selection"><option value="">全部范围</option><option
          v-for="(label, value) in selectionLabels"
          :key="value"
          :value="value"
        >{{ label }}</option></select></label>
      </div>
      <p class="meta">
        显示 {{ filtered.length }} / {{ view.sources.length }} 个来源 · 更新于 {{ timestamp(view.generated_at) }}
      </p>
      <p
        v-if="filtered.length === 0"
        class="sources-empty"
      >
        没有符合当前筛选条件的来源，请调整搜索词或类别。
      </p>
      <section
        v-for="group in groups"
        :key="group.name"
        :data-source-group="group.name"
        class="source-group"
        :aria-label="group.name"
      >
        <header class="group-heading">
          <h2>{{ group.name }}</h2><span class="meta">{{ group.sources.length }} 个来源</span>
        </header>
        <table class="source-table">
          <thead>
            <tr>
              <th scope="col">
                来源
              </th><th scope="col">
                网站与获取方式
              </th><th scope="col">
                认证方式
              </th><th scope="col">
                配置时间
              </th><th scope="col">
                测试状态
              </th><th scope="col">
                运行状态
              </th><th scope="col">
                操作
              </th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="source in group.sources"
              :key="source.definition.source_id"
              :data-source-id="source.definition.source_id"
            >
              <td data-label="来源">
                <h3>{{ source.definition.name }}</h3>
                <span class="source-selection">{{ selectionLabels[source.definition.selection] }}</span>
                <p class="integration-state">
                  {{ source.can_check_browser ? "登录检测已接入 · 正式采集待接入" : integrationLabels[source.definition.integration_status] }}
                </p>
                <p class="source-description">
                  {{ source.definition.description }}
                </p>
                <details
                  v-if="source.note?.text"
                  class="source-note"
                >
                  <summary :aria-label="`${source.definition.name} 管理员备注`">
                    <span>管理员备注</span>
                    <span class="note-expand">展开</span>
                    <span class="note-collapse">收起</span>
                  </summary>
                  <div class="source-note-content">
                    <p>{{ source.note.text }}</p>
                    <span class="cell-note">更新于 {{ timestamp(source.note.updated_at) }}</span>
                  </div>
                </details>
                <details class="source-details">
                  <summary>覆盖与接入说明</summary><p>{{ source.definition.coverage }}</p><p>{{ source.definition.integration_note }}</p>
                </details>
              </td>
              <td data-label="网站与获取方式">
                <p class="access-methods">
                  {{ source.definition.access_methods.join(' · ') }}
                </p>
                <nav
                  class="source-links"
                  :aria-label="`${source.definition.name} 官方入口`"
                >
                  <a
                    v-if="publicLink(source.definition.website_url)"
                    :href="publicLink(source.definition.website_url)"
                    target="_blank"
                    rel="noopener noreferrer"
                  >官方网站 ↗</a>
                  <a
                    v-if="publicLink(source.definition.docs_url)"
                    :href="publicLink(source.definition.docs_url)"
                    target="_blank"
                    rel="noopener noreferrer"
                  >接口文档 ↗</a>
                  <a
                    v-if="publicLink(source.definition.registration_url)"
                    :href="publicLink(source.definition.registration_url)"
                    target="_blank"
                    rel="noopener noreferrer"
                  >注册 / 开通 ↗</a>
                </nav>
              </td>
              <td data-label="认证方式">
                <template v-if="source.definition.credential_fields.length">
                  <span
                    v-if="source.configured"
                    class="key-mask"
                    aria-label="已配置，密钥不显示"
                  >••••••••</span><span
                    class="credential-state"
                    :class="{ configured: source.configured }"
                  >{{ source.configured ? '已配置' : '未配置' }}</span>
                </template>
                <template v-else>
                  <span>{{ source.status_kind === "session" ? "登录会话" : credentialModeLabels[source.definition.credential_mode] }}</span><p class="cell-note">
                    {{ source.status_kind === 'session' ? '会话由专用服务保存' : source.definition.credential_mode === 'public' ? '无需登录或密钥' : '按接入说明配置' }}
                  </p>
                </template>
              </td>
              <td data-label="配置时间">
                <span>{{ timestamp(source.updated_at) }}</span><p
                  v-if="source.revision"
                  class="cell-note"
                >
                  版本 {{ source.revision }}
                </p>
              </td>
              <td data-label="测试状态">
                <span>{{ source.status_kind === "session" ? "登录验证" : source.status_kind === "website" ? "网站访问验证" : testLabel(source) }}</span><span
                  v-if="source.tested_at"
                  class="test-time"
                >{{ timestamp(source.tested_at) }}</span>
              </td>
              <td data-label="运行状态">
                <DataSourceConnection
                  v-if="source.status_kind === 'session' || source.status_kind === 'website'"
                  :source="source"
                  :checking="checking === source.definition.source_id"
                  :failure="browserFailures[source.definition.source_id]"
                />
                <DataSourceUsage
                  v-else
                  :source="source"
                  :checking="checking === source.definition.source_id"
                  :disabled="saving || loading || Boolean(checking)"
                  :failure="usageFailures[source.definition.source_id]"
                  @check="checkUsage(source)"
                />
              </td>
              <td data-label="操作">
                <div
                  v-if="source.can_edit && source.definition.credential_fields.length"
                  class="source-actions"
                >
                  <button
                    type="button"
                    :disabled="saving || loading || Boolean(checking)"
                    @click="edit(source)"
                  >
                    {{ source.configured ? '修改密钥' : '添加密钥' }}
                  </button><button
                    v-if="source.configured"
                    type="button"
                    class="delete-action"
                    :disabled="saving || loading || Boolean(checking)"
                    @click="confirmDelete(source)"
                  >
                    删除密钥
                  </button>
                </div><span
                  v-else
                  class="cell-note"
                >{{ source.can_check_browser ? '无需 API Key' : source.definition.credential_fields.length ? '只读' : source.definition.credential_mode === 'public' ? '无需配置' : '查看接入说明' }}</span>
                <div
                  v-if="source.can_check_browser"
                  class="source-actions"
                >
                  <button
                    type="button"
                    :disabled="saving || loading || Boolean(checking) || Boolean(noteEditing)"
                    @click="checkBrowser(source)"
                  >
                    {{ checking === source.definition.source_id ? "正在测试…" : "测试登录" }}
                  </button>
                </div>
                <div
                  v-if="source.can_edit_note"
                  class="source-actions note-actions"
                >
                  <button
                    type="button"
                    :disabled="saving || loading || Boolean(checking)"
                    @click="editNote(source)"
                  >
                    {{ source.note?.text ? '编辑备注' : '添加备注' }}
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </section>
    </template>
    <div
      v-if="editing || deleting || noteEditing"
      class="credential-overlay"
    >
      <section
        ref="dialog"
        class="credential-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="credential-title"
        @keydown="dialogKeys"
      >
        <template v-if="noteEditing">
          <h2 id="credential-title">
            {{ noteEditing.definition.name }} · 来源备注
          </h2>
          <p class="meta">
            记录费用、开通进度和使用限制，请勿填写 API Key 或密码。备注不会改变接口、额度或测试状态。
          </p>
          <form @submit.prevent="saveNote">
            <label for="source-note-text">来源备注
              <textarea
                id="source-note-text"
                v-model="noteText"
                rows="8"
                maxlength="2000"
                :disabled="saving"
              />
            </label>
            <p class="meta">
              {{ noteText.length }} / 2000 字 · 清空内容后保存可删除备注
            </p>
            <p
              v-if="noteError"
              class="safe-banner danger"
              role="alert"
            >
              {{ noteError }}
            </p>
            <div class="credential-actions">
              <button
                type="button"
                :disabled="saving || !noteText"
                @click="noteText = ''"
              >
                清空内容
              </button><button
                type="button"
                :disabled="saving"
                @click="cancel"
              >
                取消
              </button><button
                type="submit"
                class="btn-primary"
                :disabled="!canSaveNote"
              >
                {{ saving ? '正在保存…' : '保存备注' }}
              </button>
            </div>
          </form>
        </template>
        <template v-else-if="editing">
          <h2 id="credential-title">
            {{ editing.definition.name }} · {{ editing.configured ? '修改密钥' : '配置密钥' }}
          </h2>
          <p class="meta">
            输入新的完整凭证。已保存的密钥不显示，提交后输入内容立即清空。
          </p>
          <form
            autocomplete="off"
            @submit.prevent="save"
          >
            <label
              v-for="field in editing.definition.credential_fields"
              :key="field.name"
              :for="`source-credential-${field.name}`"
            >{{ field.label }}
              <input
                :id="`source-credential-${field.name}`"
                v-model="credentials[field.name]"
                type="password"
                autocomplete="new-password"
                spellcheck="false"
                autocapitalize="none"
                required
                maxlength="8192"
                :disabled="saving"
              >
            </label>
            <p class="meta">
              保存新配置会重置此前的测试状态；不会自动购买额度或启动采集。
            </p>
            <div class="credential-actions">
              <button
                type="button"
                :disabled="saving"
                @click="cancel"
              >
                取消
              </button><button
                class="btn-primary"
                type="submit"
                :disabled="!canSave"
              >
                {{ saving ? '正在保存…' : '保存密钥' }}
              </button>
            </div>
          </form>
        </template>
        <template v-else-if="deleting">
          <h2 id="credential-title">
            删除 {{ deleting.definition.name }} 密钥
          </h2>
          <p>将清除这个来源在平台保存的整组凭证。后续需要重新添加才能使用依赖这些凭证的接口。</p>
          <p class="meta">
            此操作不会撤销服务商网站上的密钥，也不会删除已获取的数据。
          </p>
          <div class="credential-actions">
            <button
              type="button"
              :disabled="saving"
              @click="cancel"
            >
              取消
            </button><button
              type="button"
              class="delete-action"
              :disabled="saving"
              @click="remove"
            >
              {{ saving ? '正在删除…' : '确认删除' }}
            </button>
          </div>
        </template>
      </section>
    </div>
  </section>
</template>

<style scoped>
.sources-panel { display: grid; gap: 20px; min-width: 0; }
.sources-heading { display: flex; justify-content: space-between; align-items: flex-start; gap: 20px; flex-wrap: wrap; }
.sources-heading > div { min-width: 0; flex: 1 1 360px; }
.sources-heading h1 { margin: 4px 0 8px; }
.sources-hint { padding: 14px 16px; background: var(--fact-soft); color: var(--text-secondary); border-radius: var(--radius-sm); }
.source-filters { display: grid; grid-template-columns: minmax(160px, 2fr) repeat(2, minmax(150px, 1fr)); gap: 16px; }
.source-filters label, .credential-dialog label { display: grid; gap: 8px; font-size: 14px; font-weight: 600; }
.source-filters input, .source-filters select, .credential-dialog input, .credential-dialog textarea { min-width: 0; width: 100%; box-sizing: border-box; padding: 11px 12px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface); color: var(--text-primary); }
.credential-dialog textarea { resize: vertical; font: inherit; line-height: 1.6; }
.source-note { width: fit-content; max-width: 100%; box-sizing: border-box; margin-top: 12px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--canvas); }
.source-note[open] { width: 100%; }
.source-note summary { display: flex; align-items: center; gap: 8px; padding: 8px 10px; cursor: pointer; list-style: none; font-size: 12px; font-weight: 600; }
.source-note summary::-webkit-details-marker { display: none; }
.source-note summary::before { content: ""; width: 0; height: 0; border-top: 4px solid transparent; border-bottom: 4px solid transparent; border-left: 5px solid currentColor; flex-shrink: 0; }
.source-note[open] summary::before { transform: rotate(90deg); }
.source-note summary:hover { color: var(--fact); }
.source-note summary:focus-visible { outline: 2px solid var(--fact); outline-offset: 2px; border-radius: var(--radius-sm); }
.note-expand, .note-collapse { margin-left: auto; color: var(--text-secondary); font-weight: 400; white-space: nowrap; }
.note-collapse, .source-note[open] .note-expand { display: none; }
.source-note[open] .note-collapse { display: inline; }
.source-note-content { padding: 0 10px 10px; }
.source-note-content p { margin-top: 0; white-space: pre-wrap; overflow-wrap: anywhere; }
.source-note-content .cell-note { display: block; }
.note-actions { margin-top: 10px; }
.source-group { min-width: 0; border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface); overflow: hidden; }
.group-heading { padding: 18px 22px; display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; background: var(--canvas); border-bottom: 1px solid var(--border); }
.group-heading h2 { font-size: 18px; }
.source-table { width: 100%; border-collapse: collapse; table-layout: fixed; }
.source-table th { text-align: left; color: var(--text-secondary); font-size: 12px; font-weight: 500; padding: 14px; border-bottom: 1px solid var(--border); }
.source-table th:first-child { width: 22%; }
.source-table th:nth-child(2) { width: 16%; }
.source-table th:nth-child(3) { width: 10%; }
.source-table th:nth-child(4) { width: 13%; }
.source-table th:nth-child(5) { width: 12%; }
.source-table th:nth-child(6) { width: 16%; }
.source-table th:nth-child(7) { width: 11%; }
.source-table td { padding: 18px 14px; vertical-align: top; border-bottom: 1px solid var(--border); font-size: 13px; overflow-wrap: anywhere; }
.source-table tr:last-child td { border-bottom: 0; }
.source-table h3 { font-size: 16px; margin-bottom: 5px; }
.source-selection { font-size: 12px; color: var(--text-secondary); }
.integration-state { margin-top: 7px; font-size: 12px; color: var(--fact); }
.source-description, .cell-note, .source-details { font-size: 12px; color: var(--text-secondary); margin-top: 6px; }
.source-details p { margin-top: 6px; }
.source-details summary { cursor: pointer; }
.source-links { display: grid; gap: 7px; margin-top: 10px; font-size: 12px; }
.key-mask { display: block; letter-spacing: 2px; font-size: 17px; line-height: 1; margin-bottom: 7px; }
.credential-state { color: var(--text-secondary); }
.credential-state.configured { color: var(--fact); }
.test-time { display: block; font-size: 12px; color: var(--text-secondary); margin-top: 6px; }
.source-actions { display: grid; justify-items: start; gap: 8px; }
.source-actions button { font-size: 12px; padding: 6px 10px; }
.delete-action { color: var(--danger, #b42318); }
.sources-empty { padding: 40px 20px; text-align: center; color: var(--text-secondary); }
.credential-overlay { position: fixed; inset: 0; z-index: 1000; display: grid; place-items: center; padding: 20px; background: rgb(0 0 0 / 45%); }
.credential-dialog { width: min(100%, 480px); max-height: calc(100dvh - 40px); overflow-y: auto; min-width: 0; padding: 28px; border-radius: var(--radius); background: var(--surface); display: grid; gap: 16px; box-sizing: border-box; }
.credential-dialog form { display: grid; gap: 16px; }
.credential-actions { display: flex; justify-content: flex-end; gap: 12px; }
@media (max-width: 1100px) {
  .source-table, .source-table tbody, .source-table tr, .source-table td { display: block; width: 100%; }
  .source-table thead { display: none; }
  .source-table tr { padding: 16px; border-bottom: 1px solid var(--border); }
  .source-table tr:last-child { border-bottom: 0; }
  .source-table td { display: grid; grid-template-columns: 84px minmax(0, 1fr); gap: 6px 12px; padding: 9px 0; border: 0; }
  .source-table td::before { content: attr(data-label); color: var(--text-secondary); font-size: 12px; grid-column: 1; grid-row: 1 / span 8; }
  .source-table td > * { grid-column: 2; }
  .source-actions { display: flex; flex-wrap: wrap; }
}
@media (max-width: 550px) { .source-filters { grid-template-columns: minmax(0, 1fr); } .credential-dialog { padding: 20px; } }
</style>
