<script setup lang="ts">
/* global File, HTMLInputElement, Event, Blob */
import { computed, inject, onBeforeUnmount, onMounted, ref } from "vue";
import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";
import { useQuoteRequestScope } from "../costing-quotes/quote-request-scope";

type Document = components["schemas"]["KnowledgeDocumentView"];
type Detail = components["schemas"]["KnowledgeDocumentDetail"];
const client = inject<ReturnType<typeof createApiClient>>("tradeos-api-client", apiClient);
const documents = ref<Document[]>([]);
const detail = ref<Detail | null>(null);
const query = ref("");
const offset = ref(0);
const total = ref(0);
const loading = ref(false);
const detailLoading = ref(false);
const busy = ref(false);
const error = ref("");
const notice = ref("");
const selectedFile = ref<File | null>(null);
const fileInput = ref<HTMLInputElement | null>(null);
const acknowledgeUnknown = ref(false);
const pollingPaused = ref(false);
const limit = 20;
const maximumBytes = 10 * 1024 * 1024;
const formats: Readonly<Record<string, string>> = Object.freeze({
  txt: "text/plain", md: "text/markdown", csv: "text/csv", pdf: "application/pdf",
  png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", webp: "image/webp",
  docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
});
const statusLabels: Record<Document["status"], string> = {
  queued: "等待整理", processing: "正在整理", awaiting_confirmation: "待管理员确认",
  confirmed: "已确认", failed: "整理失败", unknown: "处理结果待核对",
};
const exportLabels: Record<NonNullable<Document["export_state"]>, string> = {
  pending: "等待写入资料库", synced: "已写入资料库", failed: "资料库写入失败",
};
const parseWarningLabels: Readonly<Record<string, string>> = {
  external_links_ignored: "未访问资料中的外部链接。",
  spreadsheet_raw_values: "表格使用单元格原始值，请核对格式和单位。",
  formulas_not_evaluated: "未计算表格公式，请核对公式对应的数值。",
  vision_transcription_required: "包含需要 AI 看图转录的内容，请对照原件核验。",
  image_resized_1600: "图片已缩放后识别，细小文字需对照原件。",
  visual_pages_rendered_1600: "PDF 图像页已渲染后识别，细小文字需对照原件。",
};
const failureLabels: Readonly<Record<string, string>> = {
  processing_failed: "整理未完成，请核对原件后重试。",
  model_result_unknown: "模型调用结果未知；请先核对记录，重试可能再次产生模型费用。",
  lease_expired: "处理超时，结果待核对；重试可能再次产生模型费用。",
  authorization_revoked: "上传者的使用权限已变更，请联系企业管理员。",
  invalid_analysis: "整理结果未通过原文证据核验。",
  source_unsupported: "文件无法读取或图像格式无效，请检查原件后重新上传。",
  source_limit_exceeded: "原件或整理结果超过处理上限；请按产品或页码拆成更小的文件后上传。",
  source_integrity_failed: "原件完整性核验失败，请联系企业管理员。",
  provider_unavailable: "整理服务暂不可用。",
};
let mounted = false;
let pollTimer: ReturnType<typeof globalThis.setTimeout> | undefined;
let polls = 0;
let uploadKey = "";
const objectUrls = new Set<string>();
function stopPolling(): void {
  if (pollTimer !== undefined) globalThis.clearTimeout(pollTimer);
  pollTimer = undefined;
}
function clearFile(): void {
  selectedFile.value = null; uploadKey = "";
  if (fileInput.value) fileInput.value.value = "";
}
function reset(): void {
  stopPolling(); documents.value = []; detail.value = null; total.value = 0;
  query.value = ""; offset.value = 0; error.value = ""; notice.value = "";
  loading.value = false; detailLoading.value = false; busy.value = false;
  acknowledgeUnknown.value = false; pollingPaused.value = false; polls = 0; clearFile();
  for (const url of objectUrls) globalThis.URL.revokeObjectURL(url);
  objectUrls.clear();
  if (mounted) globalThis.queueMicrotask(() => void refresh());
}
onBeforeUnmount(() => { mounted = false; });
const gate = useQuoteRequestScope(client, () => [], reset);
const hasPending = computed(() => [...documents.value, ...(detail.value ? [detail.value.document] : [])].some(item =>
  item.status === "queued" || item.status === "processing"
  || (item.current_revision_id !== null && item.export_state === "pending"),
));
function safeError(status: number): string {
  if (status === 400 || status === 422) return "资料格式或请求无效，请检查文件与当前版本。";
  if (status === 401) return "会话已失效，请重新登录。";
  if (status === 403) return "当前账号无权执行此操作。";
  if (status === 404) return "资料不存在或不属于当前企业。";
  if (status === 409) return "资料状态已变化，请刷新后核对最新版本。";
  if (status === 413) return "文件超过 10 MiB，请拆分后上传。";
  if (status === 429) return "请求过于频繁，请稍后手动重试。";
  if (status === 503) return "企业资料整理服务暂不可用。";
  return "请求未完成，请刷新核对资料状态。";
}
function exportLabel(state: Document["export_state"]): string { return state ? exportLabels[state] : "同步状态未知"; }
function imageEvidence(page: number): string {
  const originalPage = detail.value?.revision?.image_source_pages?.[page - 1];
  return originalPage ? `图像 ${page} · 原文件第 ${originalPage} 页` : `图像 ${page}`;
}
function date(value: string): string { return new Date(value).toLocaleString("zh-CN", { hour12: false }); }
function size(value: number): string { return value < 1024 * 1024 ? `${Math.ceil(value / 1024)} KB` : `${(value / 1024 / 1024).toFixed(1)} MiB`; }
function mime(file: File): string | null {
  const extension = file.name.split(".").at(-1)?.toLowerCase() ?? "";
  const expected = formats[extension];
  if (!expected) return null;
  const browserAlias = file.type === "application/octet-stream"
    || (extension === "csv" && file.type === "application/vnd.ms-excel")
    || (extension === "md" && file.type === "text/plain");
  if (file.type && file.type !== expected && !browserAlias) return null;
  return expected;
}
function selectFile(event: Event): void {
  const file = (event.target as HTMLInputElement).files?.[0];
  clearFile(); error.value = ""; notice.value = "";
  if (!file) return;
  if (!mime(file)) { error.value = "请选择 TXT、Markdown、PDF、DOCX、XLSX、CSV 或 PNG、JPEG、WebP 图片。"; return; }
  if (file.size < 1 || file.size > maximumBytes) { error.value = "文件不能为空，且不能超过 10 MiB。"; return; }
  selectedFile.value = file; uploadKey = globalThis.crypto.randomUUID();
}
function schedulePoll(): void {
  stopPolling();
  if (!mounted || !gate.hasIdentity.value || !hasPending.value) return;
  if (polls >= 80) { pollingPaused.value = true; return; }
  pollTimer = globalThis.setTimeout(() => { polls += 1; void loadDocuments(true); }, 3000);
}
async function loadDocuments(background = false): Promise<void> {
  if (!gate.hasIdentity.value) return;
  stopPolling();
  const operation = gate.begin("knowledge-list");
  if (!operation) return;
  if (!background) loading.value = true;
  error.value = "";
  try {
    const result = await client.GET("/knowledge/documents", {
      params: { query: { query: query.value, limit, offset: offset.value } }, signal: operation.signal, cache: "no-store",
    });
    if (!operation.valid()) return;
    if (!result.response.ok || !result.data) {
      if ([401, 403].includes(result.response.status)) { documents.value = []; detail.value = null; total.value = 0; }
      error.value = safeError(result.response.status); return;
    }
    documents.value = result.data.items; total.value = result.data.total;
    if (detail.value) await openDocument(detail.value.document.document_id, true);
    if (operation.valid()) schedulePoll();
  } catch { if (operation.valid()) error.value = "无法连接企业资料服务；已停止自动刷新，请稍后重试。"; }
  finally { if (operation.valid()) loading.value = false; }
}
async function refresh(): Promise<void> { polls = 0; pollingPaused.value = false; await loadDocuments(); }
async function search(): Promise<void> { offset.value = 0; await refresh(); }
async function page(delta: number): Promise<void> { offset.value += delta * limit; await refresh(); }
async function openDocument(id: string, background = false): Promise<void> {
  const operation = gate.begin("knowledge-detail");
  if (!operation || !gate.hasIdentity.value) return;
  if (!background) { detail.value = null; acknowledgeUnknown.value = false; detailLoading.value = true; }
  try {
    const result = await client.GET("/knowledge/documents/{document_id}", {
      params: { path: { document_id: id } }, signal: operation.signal, cache: "no-store",
    });
    if (!operation.valid()) return;
    if (!result.response.ok || !result.data) { detail.value = null; error.value = safeError(result.response.status); return; }
    detail.value = result.data;
  } catch { if (operation.valid()) { detail.value = null; error.value = "资料详情读取失败，请重试。"; } }
  finally { if (operation.valid()) detailLoading.value = false; }
}
async function upload(): Promise<void> {
  const file = selectedFile.value;
  if (!file || !uploadKey || busy.value || !gate.hasIdentity.value) return;
  const contentType = mime(file);
  if (!contentType) return;
  const operation = gate.begin("knowledge-mutation");
  if (!operation) return;
  busy.value = true; error.value = ""; notice.value = "";
  try {
    const result = await client.uploadKnowledgeDocument(file, contentType, uploadKey, operation.signal);
    if (!operation.valid()) return;
    if (result.response.status !== 202 || !result.data) { error.value = safeError(result.response.status); return; }
    clearFile(); query.value = ""; offset.value = 0; notice.value = "原件已保存，整理任务已提交；处理完成后可核对原文与整理结果。";
    await refresh();
    if (operation.valid()) await openDocument(result.data.document_id);
  } catch { if (operation.valid()) error.value = "上传结果待核对；请先刷新记录。重试同一文件将沿用原请求标识。"; }
  finally { if (operation.valid()) busy.value = false; }
}
async function act(kind: "confirm" | "retry" | "sync"): Promise<void> {
  const current = detail.value;
  if (!current || busy.value || !gate.hasIdentity.value) return;
  const doc = current.document;
  if ((kind === "confirm" && !doc.can_confirm) || (kind === "retry" && !doc.can_retry) || (kind === "sync" && !doc.can_sync)) return;
  if (kind === "retry" && doc.status === "unknown" && !acknowledgeUnknown.value) return;
  const operation = gate.begin("knowledge-mutation");
  if (!operation) return;
  busy.value = true; error.value = ""; notice.value = "";
  try {
    const params = { path: { document_id: doc.document_id } };
    const result = kind === "retry"
      ? await client.POST("/knowledge/documents/{document_id}/retry", {
        params, body: { expected_version: doc.version, acknowledge_unknown: acknowledgeUnknown.value }, signal: operation.signal,
      })
      : kind === "confirm"
        ? await client.POST("/knowledge/documents/{document_id}/confirm", {
          params, body: { expected_version: doc.version, revision_id: current.revision!.revision_id }, signal: operation.signal,
        })
        : await client.POST("/knowledge/documents/{document_id}/sync", {
          params, body: { expected_version: doc.version, revision_id: current.revision!.revision_id }, signal: operation.signal,
        });
    if (!operation.valid()) return;
    if (!result.response.ok) { error.value = safeError(result.response.status); await openDocument(doc.document_id); return; }
    acknowledgeUnknown.value = false;
    notice.value = kind === "confirm" ? "已记录管理员确认；资料库写入状态将单独更新。"
      : kind === "retry" ? "已重新提交整理任务。" : "已提交资料库同步，不会重新调用模型。";
    await refresh();
  } catch { if (operation.valid()) error.value = "操作结果待核对，请先刷新记录，不要重复提交。"; }
  finally { if (operation.valid()) busy.value = false; }
}
async function downloadSource(): Promise<void> {
  if (!detail.value || busy.value) return;
  const doc = detail.value.document;
  const operation = gate.begin("knowledge-download");
  if (!operation || !gate.hasIdentity.value) return;
  try {
    const result = await client.GET("/knowledge/documents/{document_id}/source", {
      params: { path: { document_id: doc.document_id } }, signal: operation.signal, parseAs: "blob", cache: "no-store",
    });
    if (!operation.valid()) return;
    if (!result.response.ok || !(result.data instanceof Blob)) { error.value = safeError(result.response.status); return; }
    const url = globalThis.URL.createObjectURL(result.data); objectUrls.add(url);
    const link = globalThis.document.createElement("a"); link.href = url; link.download = doc.source.filename;
    link.click();
    globalThis.setTimeout(() => { globalThis.URL.revokeObjectURL(url); objectUrls.delete(url); }, 1000);
  } catch { if (operation.valid()) error.value = "原件下载失败，请稍后重试。"; }
}
onMounted(() => { mounted = true; void refresh(); });
</script>

<template>
  <div class="shell knowledge-shell">
    <div class="page-head"><div><p class="phase-eyebrow">产品资料</p><h1>企业资料库</h1><p>本企业成员共享资料，企业之间独立保存。</p></div><button type="button" :disabled="loading || busy" @click="refresh">刷新</button></div>
    <p v-if="error" class="safe-banner danger" role="alert">{{ error }}</p>
    <p v-if="notice" class="safe-banner success" role="status">{{ notice }}</p>
    <section class="knowledge-panel intake">
      <div><h2>上传企业资料</h2><p>产品规格、公司介绍和业务说明，上传后由 Codex 调用 DeepSeek 整理；文字证据与图片判断分别保留。</p><p class="muted">TXT、Markdown、PDF（含扫描件）、DOCX、XLSX、CSV、PNG、JPEG、WebP；每份最多 10 MiB。PDF 中需要看图的页面最多 10 页。</p></div>
      <div class="upload-controls"><input ref="fileInput" type="file" accept=".txt,.md,.pdf,.docx,.xlsx,.csv,.png,.jpg,.jpeg,.webp" aria-label="选择企业资料" :disabled="busy" @change="selectFile"><button class="btn-primary" type="button" :disabled="!selectedFile || busy" @click="upload">{{ busy ? "提交中…" : "上传并整理" }}</button></div>
    </section>
    <div class="knowledge-layout">
      <section class="knowledge-panel ledger" aria-label="企业资料列表">
        <form class="search-row" @submit.prevent="search"><input v-model="query" aria-label="搜索企业资料" maxlength="200" placeholder="搜索文件名或已整理内容"><button :disabled="loading || busy" type="submit">搜索</button></form>
        <p v-if="loading" class="empty">正在读取资料…</p>
        <p v-else-if="!documents.length && !error" class="empty">当前没有符合条件的企业资料。</p>
        <div v-for="item in documents" :key="item.document_id" class="document-row" :class="{ selected: detail?.document.document_id === item.document_id }">
          <button type="button" class="document-open" @click="openDocument(item.document_id)"><strong>{{ item.source.filename }}</strong><span>{{ size(item.source.size_bytes) }} · {{ date(item.created_at) }}</span><span>上传者：{{ item.uploader_id }}</span></button>
          <div class="status-row"><span class="status-pill" :data-status="item.status">{{ statusLabels[item.status] }}</span><small v-if="item.current_revision_id">{{ exportLabel(item.export_state) }}</small></div>
        </div>
        <p v-if="pollingPaused" role="status" class="muted">自动刷新已暂停；后台任务继续处理，可点击刷新核对。</p>
        <footer class="pagination"><span>共 {{ total }} 份</span><button type="button" :disabled="offset === 0 || loading || busy" @click="page(-1)">上一页</button><button type="button" :disabled="offset + limit >= total || loading || busy" @click="page(1)">下一页</button></footer>
      </section>
      <section class="knowledge-panel detail" aria-label="资料详情">
        <p v-if="detailLoading" class="empty">正在读取资料详情…</p>
        <p v-else-if="!detail" class="empty">选择一份资料，查看原文与整理结果。</p>
        <template v-else>
          <header><div><h2>{{ detail.revision?.analysis.title ?? detail.document.source.filename }}</h2><p class="muted">{{ statusLabels[detail.document.status] }} · 版本 {{ detail.document.version }}</p></div><button type="button" :disabled="busy" @click="downloadSource">下载原件</button></header>
          <p v-if="detail.document.failure_reason" class="safe-banner danger">{{ failureLabels[detail.document.failure_reason] ?? "处理未完成，请核对记录后重试。" }}</p>
          <template v-if="detail.revision">
            <p class="safe-banner" :class="{ success: detail.revision.confirmed_at }">{{ detail.revision.confirmed_at ? "管理员已确认此版本；下方推断仍是推断，不代表客户需求已验证。" : "AI 整理草稿，等待管理员核对；不代表正式产品或可对外承诺的价格、认证、交期。" }}</p>
            <p v-if="detail.revision.source_kind === 'vision_transcription'" class="safe-banner">AI 看图转录，需对照原件；图中文字可能识别有误。已处理 {{ detail.revision.image_count }} 张图像。</p>
            <section class="fact-section"><h3>原文资料事实</h3><p v-if="!detail.revision.analysis.facts.length" class="muted">没有可核验的文字事实；图片判断单列为推断。</p><article v-for="(fact, index) in detail.revision.analysis.facts" :key="index" class="fact-card"><strong>{{ fact.label }}</strong><p>{{ fact.value }}</p><blockquote>{{ fact.source_quote }}</blockquote></article></section>
            <section v-if="detail.revision.analysis.inferences.length" class="inference-section"><h3>AI 推断 · 尚未验证</h3><article v-for="(inference, index) in detail.revision.analysis.inferences" :key="index" class="inference-card"><p>{{ inference.text }}</p><blockquote v-for="(quote, quoteIndex) in inference.evidence_quotes" :key="quoteIndex">{{ quote }}</blockquote><p v-for="page in inference.image_pages" :key="page" class="muted">图像证据：{{ imageEvidence(page) }}</p></article></section>
            <section v-if="detail.revision.parse_warnings?.length" class="parse-warnings"><h3>解析提醒</h3><p v-for="warning in detail.revision.parse_warnings" :key="warning" class="muted">{{ parseWarningLabels[warning] ?? "部分内容需要对照原件核验。" }}</p></section>
            <details><summary>查看提取原文与来源</summary><p class="muted">原件：{{ detail.document.source.filename }} · 提取于 {{ date(detail.revision.extracted_at) }}</p><pre>{{ detail.revision.source_text }}</pre></details>
            <p class="muted">整理：{{ detail.revision.extracted_by }} · {{ detail.revision.model }}<template v-if="detail.revision.confirmed_at"> · 确认于 {{ date(detail.revision.confirmed_at) }}</template></p>
            <div class="sync-state"><span>{{ exportLabel(detail.document.export_state) }}</span><button v-if="detail.document.can_sync" type="button" :disabled="busy" @click="act('sync')">重试资料库同步</button></div>
            <button v-if="detail.document.can_confirm" class="btn-primary" type="button" :disabled="busy" @click="act('confirm')">已核对原件，确认此版本</button>
          </template>
          <p v-else class="empty">整理完成后，原文证据与草稿将显示在这里。</p>
          <template v-if="detail.document.can_retry"><label v-if="detail.document.status === 'unknown'" class="check-label"><input v-model="acknowledgeUnknown" type="checkbox">我已核对记录，接受重新整理可能再次产生模型费用。</label><button type="button" :disabled="busy || (detail.document.status === 'unknown' && !acknowledgeUnknown)" @click="act('retry')">重新整理</button></template>
        </template>
      </section>
    </div>
  </div>
</template>

<style scoped>
.knowledge-shell { overflow-y: auto; gap: var(--space4); }
.page-head p, .muted { color: var(--text-secondary); font-size: 12px; }
.knowledge-panel { min-width: 0; padding: var(--space4); border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface); }
.intake { display: flex; align-items: center; justify-content: space-between; gap: var(--space4); }
.intake h2, .detail h2 { font-size: 18px; overflow-wrap: anywhere; }
.upload-controls, .search-row, .pagination, .status-row, .sync-state { display: flex; align-items: center; gap: var(--space2); }
.upload-controls { flex-wrap: wrap; max-width: 420px; }
.upload-controls input { max-width: 100%; }
.knowledge-layout { display: grid; grid-template-columns: minmax(260px, .7fr) minmax(0, 1.3fr); align-items: start; gap: var(--space4); }
.search-row input { width: 100%; min-width: 0; border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 8px; }
.document-row { border-bottom: 1px solid var(--border); padding: 12px 0; }
.document-row.selected { background: var(--fact-soft); }
.document-open { display: grid; gap: 3px; width: 100%; text-align: left; border: 0; background: transparent; overflow-wrap: anywhere; white-space: normal; }
.document-open span, .status-row small { color: var(--text-secondary); font-size: 11px; }
.status-row { flex-wrap: wrap; padding: 6px 8px 0; }
.status-pill { padding: 2px 7px; border-radius: 20px; font-size: 11px; background: var(--inference-soft); color: var(--inference); }
.status-pill[data-status="confirmed"] { background: var(--fact-soft); color: var(--fact); }
.status-pill[data-status="failed"], .status-pill[data-status="unknown"] { background: var(--danger-soft); color: var(--danger); }
.pagination { justify-content: flex-end; margin-top: 12px; font-size: 12px; flex-wrap: wrap; }
.pagination span { margin-right: auto; }
.detail { display: grid; gap: var(--space4); }
.detail header { display: flex; justify-content: space-between; align-items: start; gap: var(--space2); }
.detail header button { flex-shrink: 0; }
.fact-section, .inference-section { display: grid; gap: var(--space2); }
.fact-card, .inference-card { padding: var(--space3); border-radius: 6px; border-left: 3px solid var(--fact); background: var(--fact-soft); overflow-wrap: anywhere; }
.inference-card { border-color: var(--inference); background: var(--inference-soft); }
blockquote { border-left: 2px solid var(--border); padding-left: 8px; margin-top: 8px; color: var(--text-secondary); font-size: 12px; white-space: pre-wrap; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; font: inherit; max-height: 320px; overflow-y: auto; padding: 12px; background: var(--canvas); }
details summary { cursor: pointer; }
.sync-state { flex-wrap: wrap; justify-content: space-between; color: var(--text-secondary); font-size: 12px; }
.empty { padding: 24px 0; color: var(--text-secondary); }
.safe-banner.success { color: var(--fact); background: var(--fact-soft); border-color: var(--fact); }
.btn-primary { background: var(--action); color: white; }
.check-label { display: flex; align-items: start; gap: 8px; font-size: 12px; }
@media (max-width: 900px) { .knowledge-layout { grid-template-columns: 1fr; } .intake { align-items: stretch; flex-direction: column; } }
</style>
