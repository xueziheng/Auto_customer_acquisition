<script setup lang="ts">
/* global Blob, Event, File, HTMLInputElement, URL */
import { computed, inject, onMounted, onUnmounted, ref } from "vue";

import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";

type ApiClient = ReturnType<typeof createApiClient>;
type Extraction = components["schemas"]["WorkExtractionView"];
type Payload = components["schemas"]["ExtractionPayload"];
type RawArtifactKind = components["schemas"]["RawArtifactKind"];
type WorkSourceKind = components["schemas"]["WorkSourceKind"];
type Upload = components["schemas"]["WorkUploadView"];

interface UploadPlan {
  artifactKind: RawArtifactKind;
  sourceKind: WorkSourceKind;
}

const MIME_PLANS: Readonly<Record<string, UploadPlan>> = Object.freeze({
  "application/pdf": { artifactKind: "pdf", sourceKind: "pdf_text" },
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {
    artifactKind: "excel",
    sourceKind: "spreadsheet_text",
  },
  "audio/mp4": { artifactKind: "audio", sourceKind: "audio_transcript" },
  "audio/mpeg": { artifactKind: "audio", sourceKind: "audio_transcript" },
  "audio/wav": { artifactKind: "audio", sourceKind: "audio_transcript" },
  "image/jpeg": { artifactKind: "image", sourceKind: "image_ocr" },
  "image/png": { artifactKind: "image", sourceKind: "image_ocr" },
  "image/webp": { artifactKind: "image", sourceKind: "image_ocr" },
  "message/rfc822": { artifactKind: "email_raw", sourceKind: "email_text" },
  "text/csv": { artifactKind: "excel", sourceKind: "spreadsheet_text" },
});

const client = inject<ApiClient>("tradeos-api-client", apiClient);
const uploads = ref<Upload[]>([]);
const selectedFile = ref<File | null>(null);
const selectedUpload = ref<Upload | null>(null);
const extraction = ref<Extraction | null>(null);
const revision = ref<Payload | null>(null);
const progressEvidenceText = ref("");
const loading = ref(true);
const uploading = ref(false);
const detailLoading = ref(false);
const confirming = ref(false);
const error = ref<string | null>(null);
const notice = ref<string | null>(null);
const previewUrl = ref<string | null>(null);
const previewMime = ref<string | null>(null);

function localDateTimeValue(): string {
  const now = new Date();
  const local = new Date(now.getTime() - now.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

const occurredAt = ref(localDateTimeValue());
const customerTimezone = ref(
  Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
);
const selectedPlan = computed(() =>
  selectedFile.value ? MIME_PLANS[selectedFile.value.type] ?? null : null,
);
const isConfirmed = computed(() => extraction.value?.confirmation != null);

const statusLabels: Record<Upload["status"], string> = {
  awaiting_confirmation: "等待员工确认",
  confirmed: "已确认生效",
  extracting: "正在提取",
  failed: "提取失败",
  uploaded: "已保存，等待提取",
};

function formatDate(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? value
    : parsed.toLocaleString("zh-CN", { hour12: false });
}

function safeError(status: number): string {
  if (status === 400) return "资料格式或修订内容无效，请核对后重试";
  if (status === 403) return "只能查看和确认本人上传的工作资料";
  if (status === 409) return "资料状态已经变化，请刷新";
  if (status === 503) return "工作资料服务暂不可用";
  return "请求未完成，请稍后重试";
}

function clonePayload(payload: Payload): Payload {
  return JSON.parse(JSON.stringify(payload)) as Payload;
}

function releasePreview(): void {
  if (previewUrl.value) URL.revokeObjectURL(previewUrl.value);
  previewUrl.value = null;
  previewMime.value = null;
}

function selectFile(event: Event): void {
  const input = event.target as HTMLInputElement;
  selectedFile.value = input.files?.[0] ?? null;
  notice.value = null;
  error.value = selectedFile.value && !selectedPlan.value
    ? "不支持该文件类型；请选择 PDF、CSV/XLSX、图片、音频或 EML 邮件原件"
    : null;
}

async function loadUploads(): Promise<void> {
  loading.value = true;
  error.value = null;
  try {
    const result = await client.GET("/work-uploads", {
      params: { query: { limit: 100 } },
    });
    if (result.response.status !== 200 || !result.data) {
      error.value = safeError(result.response.status);
      return;
    }
    uploads.value = result.data;
  } catch {
    error.value = "无法连接工作资料服务";
  } finally {
    loading.value = false;
  }
}

async function uploadSelected(): Promise<void> {
  if (!selectedFile.value || !selectedPlan.value || uploading.value) return;
  const occurred = new Date(occurredAt.value);
  if (Number.isNaN(occurred.getTime()) || !customerTimezone.value.trim()) {
    error.value = "请填写有效的工作发生时间和客户时区";
    return;
  }
  uploading.value = true;
  error.value = null;
  notice.value = null;
  try {
    const result = await client.uploadWorkArtifact({
      artifactKind: selectedPlan.value.artifactKind,
      body: selectedFile.value,
      customerTimezone: customerTimezone.value.trim(),
      occurredAt: occurred.toISOString(),
      sourceKind: selectedPlan.value.sourceKind,
    });
    if (result.response.status !== 201 || !result.data) {
      error.value = safeError(result.response.status);
      return;
    }
    notice.value = "原件已安全保存，已进入提取队列";
    selectedFile.value = null;
    await loadUploads();
  } catch {
    error.value = "原件上传失败，请稍后重试";
  } finally {
    uploading.value = false;
  }
}

async function openUpload(upload: Upload): Promise<void> {
  selectedUpload.value = upload;
  extraction.value = null;
  revision.value = null;
  progressEvidenceText.value = "";
  detailLoading.value = true;
  error.value = null;
  releasePreview();
  try {
    const [artifactResult, extractionResult] = await Promise.all([
      client.GET("/work-uploads/{upload_id}/artifact", {
        params: { path: { upload_id: upload.upload_id } },
        parseAs: "blob",
      }),
      client.GET("/work-uploads/{upload_id}/extraction", {
        params: { path: { upload_id: upload.upload_id } },
      }),
    ]);
    if (artifactResult.response.status === 200 && artifactResult.data instanceof Blob) {
      previewMime.value = artifactResult.response.headers.get("content-type")
        ?? artifactResult.data.type;
      previewUrl.value = URL.createObjectURL(artifactResult.data);
    } else {
      error.value = safeError(artifactResult.response.status);
    }
    if (extractionResult.response.status !== 200) {
      error.value = safeError(extractionResult.response.status);
      return;
    }
    extraction.value = extractionResult.data ?? null;
    if (extraction.value) {
      const effective = extraction.value.confirmation?.payload
        ?? extraction.value.payload;
      revision.value = clonePayload(effective);
      progressEvidenceText.value = effective.progress_note?.evidence_quotes.join("\n") ?? "";
    }
  } catch {
    error.value = "原件或提取结果读取失败，请稍后重试";
  } finally {
    detailLoading.value = false;
  }
}

async function confirmRevision(): Promise<void> {
  if (!selectedUpload.value || !revision.value || confirming.value || isConfirmed.value) return;
  if (revision.value.progress_note) {
    revision.value.progress_note.evidence_quotes = progressEvidenceText.value
      .split("\n")
      .map((value) => value.trim())
      .filter((value, index, values) => value && values.indexOf(value) === index);
  }
  confirming.value = true;
  error.value = null;
  try {
    const result = await client.POST("/work-uploads/{upload_id}/confirm", {
      params: { path: { upload_id: selectedUpload.value.upload_id } },
      body: revision.value,
    });
    if (result.response.status !== 200 || !result.data) {
      error.value = safeError(result.response.status);
      return;
    }
    extraction.value = result.data;
    revision.value = clonePayload(result.data.confirmation?.payload ?? result.data.payload);
    notice.value = "员工修订已作为新版本确认；Agent 原始提取保持不变";
    await loadUploads();
  } catch {
    error.value = "修订版本未提交，请稍后重试";
  } finally {
    confirming.value = false;
  }
}

function displayNeedValue(value: string | components["schemas"]["ExtractedMoneyValue"]): string {
  return typeof value === "string" ? value : `${value.amount} ${value.currency}`;
}

onMounted(() => void loadUploads());
onUnmounted(releasePreview);
</script>

<template>
  <div class="shell work-shell">
    <div class="page-head work-head">
      <div>
        <p class="phase-eyebrow">
          HUMAN WORK INTAKE
        </p><h1>员工工作上传</h1>
      </div>
      <button
        type="button"
        :disabled="loading"
        @click="loadUploads"
      >
        {{ loading ? "加载中…" : "刷新记录" }}
      </button>
    </div>
    <div class="safe-banner">
      <span aria-hidden="true">i</span><div>原件先进入不可变 Artifact Store；Agent 提取只是候选，员工确认新版本后才可进入后续业务流程。</div>
    </div>
    <div
      v-if="error"
      class="safe-banner danger"
      role="alert"
    >
      {{ error }}
    </div>
    <div
      v-if="notice"
      class="safe-banner success"
      role="status"
    >
      {{ notice }}
    </div>

    <section class="intake-layout">
      <article class="panel upload-panel">
        <header>
          <div>
            <p class="card-kicker">
              NEW ARTIFACT
            </p><h2>上传工作原件</h2>
          </div><span>本人可见</span>
        </header>
        <label class="file-picker"><strong>{{ selectedFile?.name ?? "选择本地文件" }}</strong><span>PDF、CSV/XLSX、图片、音频或 EML</span><input
          type="file"
          accept=".pdf,.csv,.xlsx,.png,.jpg,.jpeg,.webp,.mp3,.wav,.m4a,.eml"
          :disabled="uploading"
          @change="selectFile"
        ></label>
        <div class="field-grid">
          <label>工作发生时间<input
            v-model="occurredAt"
            type="datetime-local"
          ></label><label>客户时区<input
            v-model.trim="customerTimezone"
            type="text"
            placeholder="Asia/Shanghai"
          ></label>
        </div>
        <button
          class="btn-primary upload-button"
          type="button"
          :disabled="!selectedFile || !selectedPlan || uploading"
          @click="uploadSelected"
        >
          {{ uploading ? "正在保存原件…" : "上传并进入提取队列" }}
        </button>
      </article>

      <article class="panel ledger-panel">
        <header>
          <div>
            <p class="card-kicker">
              MY INTAKE LEDGER
            </p><h2>我的上传记录</h2>
          </div><span>{{ uploads.length }} 条</span>
        </header>
        <div
          v-if="loading"
          class="empty"
        >
          正在读取上传记录…
        </div>
        <div
          v-else-if="!uploads.length"
          class="empty"
        >
          还没有上传记录
        </div>
        <div
          v-else
          class="upload-list"
        >
          <article
            v-for="item in uploads"
            :key="item.upload_id"
            class="upload-row"
            :class="{ selected: selectedUpload?.upload_id === item.upload_id }"
          >
            <div>
              <span
                class="status-pill"
                :data-status="item.status"
              >{{ statusLabels[item.status] }}</span><strong>{{ item.upload_id }}</strong><small>{{ item.source_kind }} · {{ formatDate(item.occurred_at) }}</small>
            </div>
            <button
              type="button"
              @click="openUpload(item)"
            >
              打开对照
            </button>
          </article>
        </div>
      </article>
    </section>

    <section
      v-if="selectedUpload"
      class="comparison panel"
      aria-label="原件与提取版本对照"
    >
      <header>
        <div>
          <p class="card-kicker">
            EVIDENCE COMPARISON
          </p><h2>原件 → Agent 候选 → 员工确认</h2>
        </div><span>{{ selectedUpload.upload_id }}</span>
      </header>
      <div
        v-if="detailLoading"
        class="empty"
      >
        正在读取原件与提取版本…
      </div>
      <div
        v-else
        class="comparison-grid"
      >
        <article class="comparison-column original-column">
          <header><span>01</span><div><h3>原件证据</h3><p>不可变，仅供核对</p></div></header>
          <div
            v-if="previewUrl"
            class="preview-frame"
          >
            <img
              v-if="previewMime?.startsWith('image/')"
              :src="previewUrl"
              alt="员工上传原件预览"
            >
            <audio
              v-else-if="previewMime?.startsWith('audio/')"
              :src="previewUrl"
              controls
            >无法播放该音频</audio>
            <iframe
              v-else-if="previewMime === 'application/pdf'"
              :src="previewUrl"
              title="原件预览"
            />
            <a
              v-else
              :href="previewUrl"
              download
            >下载原件核对</a>
          </div>
          <p
            v-else
            class="empty compact"
          >
            原件预览暂不可用
          </p>
          <dl class="metadata">
            <div><dt>Artifact</dt><dd>{{ selectedUpload.artifact_id }}</dd></div><div><dt>发生时间</dt><dd>{{ formatDate(selectedUpload.occurred_at) }}</dd></div><div><dt>客户时区</dt><dd>{{ selectedUpload.customer_timezone }}</dd></div>
          </dl>
        </article>

        <article class="comparison-column agent-column">
          <header><span>02</span><div><h3>Agent 原始提取</h3><p>只读候选，不是已确认事实</p></div></header>
          <div
            v-if="!extraction"
            class="empty compact"
          >
            尚无提取结果，请稍后刷新
          </div>
          <template v-else>
            <div class="version-note">
              {{ extraction.extracted_by }} · {{ formatDate(extraction.created_at) }}
            </div>
            <section class="extract-group">
              <h4>事实候选</h4><article
                v-for="(item, index) in extraction.payload.facts"
                :key="index"
                class="extract-card"
              >
                <span>{{ item.fact_type }}</span><strong>{{ item.value }}</strong><blockquote>{{ item.evidence_quote }}</blockquote>
              </article><p
                v-if="!extraction.payload.facts.length"
                class="muted"
              >
                未提取到事实候选
              </p>
            </section>
            <section class="extract-group">
              <h4>Need 字段候选</h4><article
                v-for="(item, index) in extraction.payload.need_field_updates"
                :key="index"
                class="extract-card inference-card"
              >
                <span>{{ item.field_name }}</span><strong>{{ displayNeedValue(item.value) }}</strong><blockquote>{{ item.evidence_quote }}</blockquote>
              </article><p
                v-if="!extraction.payload.need_field_updates.length"
                class="muted"
              >
                未提取到字段更新
              </p>
            </section>
            <section class="extract-group">
              <h4>承诺候选</h4><article
                v-for="(item, index) in extraction.payload.commitments"
                :key="index"
                class="extract-card commitment-extract"
              >
                <span>{{ item.commitment_type }}</span><strong>{{ item.action }}</strong><small>{{ item.due_at }}</small><blockquote>{{ item.verbatim }}</blockquote>
              </article><p
                v-if="!extraction.payload.commitments.length"
                class="muted"
              >
                未提取到承诺
              </p>
            </section>
          </template>
        </article>

        <article class="comparison-column revision-column">
          <header><span>03</span><div><h3>员工修订版本</h3><p>确认将追加新版本，不覆盖 Agent 结果</p></div></header>
          <div
            v-if="!revision"
            class="empty compact"
          >
            等待 Agent 提取后才能修订
          </div>
          <template v-else>
            <div
              v-if="extraction?.confirmation"
              class="confirmed-version"
            >
              已确认版本 {{ extraction.confirmation.revision }} · {{ formatDate(extraction.confirmation.confirmed_at) }}
            </div>
            <section class="revision-group">
              <h4>事实</h4><article
                v-for="(item, index) in revision.facts"
                :key="index"
                class="revision-card"
                :data-revision-fact="index"
              >
                <select
                  v-model="item.fact_type"
                  :disabled="isConfirmed"
                >
                  <option value="customer_statement">
                    客户陈述
                  </option><option value="employee_statement">
                    员工陈述
                  </option><option value="customer_reaction">
                    客户反应
                  </option><option value="activity">
                    活动
                  </option>
                </select><label>内容<input
                  v-model.trim="item.value"
                  type="text"
                  :disabled="isConfirmed"
                ></label><label>原文证据<textarea
                  v-model.trim="item.evidence_quote"
                  :disabled="isConfirmed"
                /></label>
              </article>
            </section>
            <section class="revision-group">
              <h4>Need 字段更新</h4><article
                v-for="(item, index) in revision.need_field_updates"
                :key="index"
                class="revision-card"
              >
                <label>字段<input
                  v-model.trim="item.field_name"
                  type="text"
                  :disabled="isConfirmed"
                ></label><label v-if="typeof item.value === 'string'">值<input
                  v-model.trim="item.value"
                  type="text"
                  :disabled="isConfirmed"
                ></label><div
                  v-else
                  class="money-fields"
                >
                  <label>金额<input
                    v-model.trim="item.value.amount"
                    type="text"
                    :disabled="isConfirmed"
                  ></label><label>币种<input
                    v-model.trim="item.value.currency"
                    type="text"
                    :disabled="isConfirmed"
                  ></label>
                </div><label>原文证据<textarea
                  v-model.trim="item.evidence_quote"
                  :disabled="isConfirmed"
                /></label>
              </article>
            </section>
            <section class="revision-group">
              <h4>承诺</h4><article
                v-for="(item, index) in revision.commitments"
                :key="index"
                class="revision-card"
              >
                <select
                  v-model="item.commitment_type"
                  :disabled="isConfirmed"
                >
                  <option value="employee">
                    员工承诺
                  </option><option value="customer">
                    客户承诺
                  </option>
                </select><label>动作<input
                  v-model.trim="item.action"
                  type="text"
                  :disabled="isConfirmed"
                ></label><label>绝对到期时间<input
                  v-model.trim="item.due_at"
                  type="text"
                  :disabled="isConfirmed"
                ></label><label class="check-label"><input
                  v-model="item.due_at_uncertain"
                  type="checkbox"
                  :disabled="isConfirmed"
                >时间仍不确定</label><label>原话<textarea
                  v-model.trim="item.verbatim"
                  :disabled="isConfirmed"
                /></label>
              </article>
            </section>
            <section
              v-if="revision.progress_note"
              class="revision-group"
            >
              <h4>进展摘要</h4><article class="revision-card">
                <label>摘要<textarea
                  v-model.trim="revision.progress_note.summary"
                  :disabled="isConfirmed"
                /></label><label>证据原话（每行一条）<textarea
                  v-model="progressEvidenceText"
                  :disabled="isConfirmed"
                /></label>
              </article>
            </section>
            <button
              v-if="!isConfirmed"
              class="btn-primary confirm-button"
              type="button"
              :disabled="confirming"
              @click="confirmRevision"
            >
              {{ confirming ? "正在确认…" : "确认修订并生效" }}
            </button>
          </template>
        </article>
      </div>
    </section>
  </div>
</template>

<style scoped>
.work-shell { overflow: auto; gap: var(--space4); }
.work-head { justify-content: space-between; padding-top: var(--space3); }
.safe-banner.success { border-color: #7acbbd; background: var(--fact-soft); color: var(--fact); }
.intake-layout { display: grid; grid-template-columns: minmax(300px, .72fr) minmax(480px, 1.28fr); gap: var(--space4); }
.panel { border: 1px solid var(--border); border-radius: 12px; background: var(--surface); padding: var(--space4); }
.panel > header { display: flex; justify-content: space-between; align-items: flex-end; gap: var(--space3); }
.panel > header > span { color: var(--text-secondary); font-size: 12px; }
.panel h2 { font-size: 18px; }
.file-picker { display: grid; place-items: center; min-height: 120px; margin-top: var(--space4); border: 1px dashed var(--action); border-radius: var(--radius); background: #f4f7ff; cursor: pointer; text-align: center; }
.file-picker span { color: var(--text-secondary); font-size: 11px; }
.file-picker input { position: absolute; width: 1px; height: 1px; opacity: 0; }
.field-grid { display: grid; grid-template-columns: 1fr 1fr; gap: var(--space3); margin-top: var(--space4); }
.field-grid label, .revision-card label { display: grid; gap: var(--space1); color: var(--text-secondary); font-size: 11px; }
input, textarea, select { width: 100%; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface); padding: 7px 9px; color: var(--text-primary); }
textarea { min-height: 60px; resize: vertical; }
.btn-primary { border-color: var(--action); background: var(--action); color: white; font-weight: 700; }
.upload-button { width: 100%; margin-top: var(--space4); }
.upload-list { display: grid; gap: var(--space2); max-height: 330px; margin-top: var(--space3); overflow: auto; }
.upload-row { display: flex; justify-content: space-between; align-items: center; gap: var(--space3); border: 1px solid var(--border); border-radius: var(--radius); padding: var(--space3); }
.upload-row.selected { border-color: var(--action); box-shadow: inset 3px 0 var(--action); }
.upload-row > div { display: grid; min-width: 0; gap: 2px; }
.upload-row strong, .upload-row small { overflow-wrap: anywhere; }
.upload-row small { color: var(--text-secondary); }
.status-pill { width: fit-content; border-radius: 999px; background: var(--warning-soft); padding: 2px 7px; color: var(--warning); font-size: 10px; font-weight: 800; }
.status-pill[data-status="confirmed"] { background: var(--fact-soft); color: var(--fact); }
.status-pill[data-status="failed"] { background: var(--danger-soft); color: var(--danger); }
.empty { display: grid; place-items: center; min-height: 150px; color: var(--text-secondary); }
.empty.compact { min-height: 90px; }
.comparison-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: var(--space3); margin-top: var(--space4); }
.comparison-column { min-width: 0; border: 1px solid var(--border); border-radius: var(--radius); padding: var(--space3); }
.comparison-column > header { display: flex; gap: var(--space2); border-bottom: 1px solid var(--border); padding-bottom: var(--space3); }
.comparison-column > header > span { display: grid; place-items: center; width: 28px; height: 28px; flex: 0 0 auto; border-radius: 50%; background: var(--fact-soft); color: var(--fact); font-size: 10px; font-weight: 800; }
.agent-column > header > span { background: var(--inference-soft); color: var(--inference); }
.revision-column > header > span { background: #eaf0ff; color: var(--action); }
.comparison-column h3 { font-size: 15px; }
.comparison-column header p { color: var(--text-secondary); font-size: 10px; }
.preview-frame { display: grid; place-items: center; min-height: 310px; margin-top: var(--space3); overflow: hidden; border-radius: var(--radius-sm); background: #edf1f0; }
.preview-frame iframe, .preview-frame img { width: 100%; height: 310px; border: 0; object-fit: contain; }
.preview-frame audio { width: calc(100% - 24px); }
.metadata { display: grid; gap: var(--space2); margin-top: var(--space3); }
.metadata div { display: grid; grid-template-columns: 80px 1fr; gap: var(--space2); }
.metadata dt { color: var(--text-secondary); font-size: 10px; }
.metadata dd { margin: 0; overflow-wrap: anywhere; font-size: 11px; }
.version-note, .confirmed-version { margin-top: var(--space3); border-radius: var(--radius-sm); padding: var(--space2); font-size: 10px; }
.version-note { background: var(--inference-soft); color: var(--inference); }
.confirmed-version { background: var(--fact-soft); color: var(--fact); font-weight: 700; }
.extract-group, .revision-group { display: grid; gap: var(--space2); margin-top: var(--space3); }
.extract-group h4, .revision-group h4 { color: var(--text-secondary); font-size: 11px; text-transform: uppercase; }
.extract-card, .revision-card { display: grid; gap: var(--space2); border: 1px solid var(--border); border-left: 3px solid var(--fact); border-radius: var(--radius-sm); padding: var(--space2); }
.extract-card.inference-card { border-left-color: var(--inference); }
.commitment-extract { border-left-color: var(--action); }
.extract-card > span { color: var(--text-secondary); font-size: 9px; font-weight: 800; text-transform: uppercase; }
.extract-card > strong { overflow-wrap: anywhere; font-size: 12px; }
.extract-card blockquote { border-left: 2px solid var(--border); padding-left: var(--space2); color: var(--text-secondary); font-size: 10px; font-style: italic; }
.extract-card small, .muted { color: var(--text-secondary); font-size: 10px; }
.revision-card { border-left-color: var(--action); }
.money-fields { display: grid; grid-template-columns: 1fr 1fr; gap: var(--space2); }
.check-label { display: flex !important; grid-template-columns: auto 1fr; align-items: center; }
.check-label input { width: auto; }
.confirm-button { width: 100%; margin-top: var(--space4); }
@media (max-width: 1050px) { .comparison-grid { grid-template-columns: 1fr; } .preview-frame { min-height: 220px; } .preview-frame iframe, .preview-frame img { height: 220px; } }
@media (max-width: 760px) { .intake-layout, .field-grid { grid-template-columns: 1fr; } .upload-row { align-items: stretch; flex-direction: column; } }
</style>
