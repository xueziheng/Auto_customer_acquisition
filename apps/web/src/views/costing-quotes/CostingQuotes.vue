<script setup lang="ts">
import { computed, inject, ref } from "vue";

import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";

type ApiClient = ReturnType<typeof createApiClient>;
type CostItemCreate = components["schemas"]["CostItemCreate"];
type CostSheetCreate = components["schemas"]["CostSheetCreate"];
type CostSheet = components["schemas"]["CostSheetView"];
type Readiness = components["schemas"]["QuoteReadiness"];

const client = inject<ApiClient>("tradeos-api-client", apiClient);
const opportunityId = ref("");
const sheets = ref<CostSheet[]>([]);
const selectedSheetId = ref("");
const loading = ref(false);
const saving = ref(false);
const checking = ref(false);
const error = ref<string | null>(null);
const notice = ref<string | null>(null);
const readiness = ref<Readiness | null>(null);

const versionType = ref("estimated");
const quantity = ref("100");
const baseCurrency = ref("USD");
const quoteCurrency = ref("USD");
const fxSnapshotId = ref("");

const itemType = ref("product_purchase");
const itemAmount = ref("");
const itemCurrency = ref("USD");
const priceBasis = ref("quoted");
const isPerUnit = ref(true);
const itemSource = ref("");
const itemNote = ref("");
const expectedItems = ref("product_purchase");

const selectedSheet = computed(() =>
  sheets.value.find((sheet) => sheet.cost_sheet_id === selectedSheetId.value) ?? null,
);

const itemTypes = [
  ["product_purchase", "产品采购"],
  ["sample_fee", "样品"],
  ["mold_fee", "模具"],
  ["customization_fee", "定制"],
  ["logo_printing", "Logo 印刷"],
  ["packaging", "包装"],
  ["quality_inspection", "质检"],
  ["wastage", "损耗"],
  ["domestic_freight", "国内运输"],
  ["international_freight", "国际运输"],
  ["insurance", "保险"],
  ["customs_clearance", "报关"],
  ["duties_and_taxes", "关税与税费"],
  ["destination_freight", "目的地运输"],
  ["warehousing", "仓储"],
  ["payment_fees", "支付手续费"],
  ["sales_commission", "销售佣金"],
  ["customer_acquisition", "获客"],
  ["contact_data_cost", "联系人数据"],
  ["ad_allocation", "广告分摊"],
  ["agent_api_allocation", "Agent/API 分摊"],
  ["returns_reserve", "退货售后预留"],
] as const;

function safeError(status: number): string {
  if (status === 400) return "成本参数无效，请核对金额字符串、币种和来源";
  if (status === 403) return "当前角色无权查看或维护成本表";
  if (status === 503) return "成本服务暂不可用";
  return "请求未完成，请稍后重试";
}

function validOpportunity(): boolean {
  if (!/^opp_[0-7][0-9A-HJKMNP-TV-Z]{25}$/.test(opportunityId.value.trim())) {
    error.value = "请输入有效的 Opportunity ID";
    return false;
  }
  return true;
}

async function loadVersions(): Promise<void> {
  if (!validOpportunity()) return;
  loading.value = true;
  error.value = null;
  notice.value = null;
  readiness.value = null;
  try {
    const result = await client.GET(
      "/costing-quotes/opportunities/{opportunity_id}/cost-sheets",
      { params: { path: { opportunity_id: opportunityId.value.trim() } } },
    );
    if (result.response.status !== 200 || !result.data) {
      error.value = safeError(result.response.status);
      return;
    }
    sheets.value = result.data;
    if (!sheets.value.some((sheet) => sheet.cost_sheet_id === selectedSheetId.value)) {
      selectedSheetId.value = sheets.value[0]?.cost_sheet_id ?? "";
    }
  } catch {
    error.value = "无法连接成本服务";
  } finally {
    loading.value = false;
  }
}

async function createSheet(): Promise<void> {
  if (!validOpportunity() || saving.value) return;
  const parsedQuantity = Number(quantity.value);
  if (!Number.isInteger(parsedQuantity) || parsedQuantity <= 0) {
    error.value = "数量必须是正整数";
    return;
  }
  const body: CostSheetCreate = {
    base_currency: baseCurrency.value.trim().toUpperCase(),
    fx_rates: [],
    fx_snapshot_id: fxSnapshotId.value.trim() || null,
    quantity: parsedQuantity,
    quote_currency: quoteCurrency.value.trim().toUpperCase(),
    version_type: versionType.value,
  };
  if (body.version_type === "quoted" && !body.fx_snapshot_id) {
    error.value = "QUOTED 版本必须填写汇率快照 ID";
    return;
  }
  saving.value = true;
  error.value = null;
  try {
    const result = await client.POST(
      "/costing-quotes/opportunities/{opportunity_id}/cost-sheets",
      {
        params: { path: { opportunity_id: opportunityId.value.trim() } },
        body,
      },
    );
    if (result.response.status !== 201 || !result.data) {
      error.value = safeError(result.response.status);
      return;
    }
    selectedSheetId.value = result.data.cost_sheet_id;
    await loadVersions();
    notice.value = "成本表版本已创建";
  } catch {
    error.value = "成本表未创建，请稍后重试";
  } finally {
    saving.value = false;
  }
}

async function saveItem(): Promise<void> {
  if (!selectedSheet.value || saving.value) return;
  if (!itemAmount.value.trim() || !itemSource.value.trim()) {
    error.value = "金额和来源引用均为必填项";
    return;
  }
  const body: CostItemCreate = {
    amount: itemAmount.value.trim(),
    currency: itemCurrency.value.trim().toUpperCase(),
    is_per_unit: isPerUnit.value,
    item_type: itemType.value,
    note: itemNote.value.trim() || null,
    price_basis: priceBasis.value,
    source_ref: itemSource.value.trim(),
  };
  saving.value = true;
  error.value = null;
  try {
    const result = await client.POST(
      "/costing-quotes/cost-sheets/{cost_sheet_id}/items",
      {
        params: { path: { cost_sheet_id: selectedSheet.value.cost_sheet_id } },
        body,
      },
    );
    if (result.response.status !== 204) {
      error.value = safeError(result.response.status);
      return;
    }
    itemAmount.value = "";
    itemSource.value = "";
    itemNote.value = "";
    await loadVersions();
    notice.value = "成本项已保存，来源与录入人已留痕";
  } catch {
    error.value = "成本项未保存，请稍后重试";
  } finally {
    saving.value = false;
  }
}

async function checkReadiness(): Promise<void> {
  if (!selectedSheet.value || checking.value) return;
  const expected = expectedItems.value
    .split(",")
    .map((value) => value.trim())
    .filter((value, index, values) => value && values.indexOf(value) === index);
  checking.value = true;
  error.value = null;
  try {
    const result = await client.POST(
      "/costing-quotes/cost-sheets/{cost_sheet_id}/readiness",
      {
        params: { path: { cost_sheet_id: selectedSheet.value.cost_sheet_id } },
        body: { expected_item_types: expected },
      },
    );
    if (result.response.status !== 200 || !result.data) {
      error.value = safeError(result.response.status);
      return;
    }
    readiness.value = result.data;
  } catch {
    error.value = "报价就绪检查未完成，请稍后重试";
  } finally {
    checking.value = false;
  }
}

function formatDate(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? value
    : parsed.toLocaleString("zh-CN", { hour12: false });
}
</script>

<template>
  <div class="shell costing-shell">
    <div class="page-head costing-head">
      <div>
        <p class="phase-eyebrow">
          DETERMINISTIC COSTING
        </p><h1>成本与报价</h1>
      </div>
      <span class="status manual-status">Phase 1 · 人工录入</span>
    </div>
    <div class="safe-banner">
      <span aria-hidden="true">i</span>
      <div>金额只以 Decimal 字符串提交；本页只保存人工成本和检查阻断项，不生成客户报价。</div>
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

    <section class="opportunity-bar panel">
      <label>Opportunity ID
        <input
          v-model.trim="opportunityId"
          name="opportunity-id"
          placeholder="opp_…"
        >
      </label>
      <button
        type="button"
        :disabled="loading"
        @click="loadVersions"
      >
        {{ loading ? "读取中…" : "读取成本版本" }}
      </button>
    </section>

    <section class="workspace-grid">
      <article class="panel">
        <header>
          <div>
            <p class="card-kicker">
              NEW VERSION
            </p><h2>创建成本表版本</h2>
          </div>
        </header>
        <div class="field-grid">
          <label>版本类型<select v-model="versionType"><option value="estimated">ESTIMATED</option><option value="quoted">QUOTED</option><option value="actual">ACTUAL</option></select></label>
          <label>数量<input
            v-model="quantity"
            inputmode="numeric"
          ></label>
          <label>核算币种<input
            v-model.trim="baseCurrency"
            maxlength="3"
          ></label>
          <label>报价币种<input
            v-model.trim="quoteCurrency"
            maxlength="3"
          ></label>
          <label class="wide">汇率快照 ID<input
            v-model.trim="fxSnapshotId"
            placeholder="QUOTED 必填"
          ></label>
        </div>
        <button
          class="btn-primary"
          type="button"
          :disabled="saving"
          @click="createSheet"
        >
          创建新版本
        </button>
      </article>

      <article class="panel versions-panel">
        <header>
          <div>
            <p class="card-kicker">
              VERSION LEDGER
            </p><h2>成本版本</h2>
          </div><span>{{ sheets.length }} 个</span>
        </header>
        <div
          v-if="!sheets.length"
          class="empty"
        >
          输入 Opportunity ID 后读取版本
        </div>
        <button
          v-for="sheet in sheets"
          :key="sheet.cost_sheet_id"
          class="version-row"
          :class="{ selected: selectedSheetId === sheet.cost_sheet_id }"
          type="button"
          @click="selectedSheetId = sheet.cost_sheet_id; readiness = null"
        >
          <span><strong>{{ sheet.version_type.toUpperCase() }} · v{{ sheet.version_number }}</strong><small>{{ sheet.quantity }} 件 · {{ sheet.base_currency }} → {{ sheet.quote_currency }}</small></span>
          <span class="state-pill">{{ sheet.is_locked ? "已锁定" : "可编辑" }}</span>
        </button>
      </article>
    </section>

    <section
      v-if="selectedSheet"
      class="detail-grid"
    >
      <article class="panel item-panel">
        <header>
          <div>
            <p class="card-kicker">
              COST PROVENANCE
            </p><h2>成本项与来源</h2>
          </div><span>{{ selectedSheet.cost_sheet_id }}</span>
        </header>
        <div
          v-if="!selectedSheet.items.length"
          class="empty"
        >
          还没有已确认成本项
        </div>
        <div
          v-else
          class="item-list"
        >
          <article
            v-for="item in selectedSheet.items"
            :key="`${item.item_type}:${item.source_ref}`"
            class="item-row"
          >
            <div><strong>{{ item.item_label }}</strong><small>{{ item.price_basis }} · {{ item.is_per_unit ? "单件" : "整单" }}</small></div>
            <div class="amount">
              {{ item.amount.amount }} {{ item.amount.currency }}
            </div>
            <div class="source">
              <span>来源</span><strong>{{ item.source_ref ?? "未提供" }}</strong><small>{{ item.entered_by_id ?? "待人工确认" }}</small>
            </div>
          </article>
        </div>
        <div class="field-grid item-form">
          <label>成本类型<select v-model="itemType"><option
            v-for="option in itemTypes"
            :key="option[0]"
            :value="option[0]"
          >{{ option[1] }}</option></select></label>
          <label>金额<input
            v-model.trim="itemAmount"
            name="item-amount"
            inputmode="decimal"
            placeholder="例如 12.50"
          ></label>
          <label>币种<input
            v-model.trim="itemCurrency"
            maxlength="3"
          ></label>
          <label>价格基准<select v-model="priceBasis"><option value="quoted">quoted</option><option value="indicative">indicative</option><option value="actual">actual</option></select></label>
          <label class="wide">来源引用<input
            v-model.trim="itemSource"
            name="item-source"
            placeholder="供应商报价或证据 Artifact 引用"
          ></label>
          <label class="wide">备注<input
            v-model.trim="itemNote"
            placeholder="可选，不代替来源"
          ></label>
          <label class="check"><input
            v-model="isPerUnit"
            type="checkbox"
          >按单件计价</label>
        </div>
        <button
          class="btn-primary"
          type="button"
          :disabled="saving || selectedSheet.is_locked"
          @click="saveItem"
        >
          保存成本项
        </button>
      </article>

      <aside class="panel readiness-panel">
        <p class="card-kicker">
          QUOTE READINESS
        </p><h2>报价阻断检查</h2>
        <p class="muted">
          只读检查，不会锁定成本表，也不会接受参考价风险。
        </p>
        <label>场景要求的成本类型
          <input
            v-model.trim="expectedItems"
            name="expected-items"
            placeholder="逗号分隔的 CostItemType"
          >
        </label>
        <button
          type="button"
          :disabled="checking"
          @click="checkReadiness"
        >
          {{ checking ? "检查中…" : "检查报价就绪" }}
        </button>
        <div
          v-if="readiness"
          class="readiness-result"
          :class="{ ready: readiness.ready }"
        >
          <strong>{{ readiness.ready ? "结构检查通过" : "仍有阻断项" }}</strong>
          <ul>
            <li
              v-for="blocker in readiness.blockers"
              :key="blocker"
            >
              {{ blocker }}
            </li>
          </ul>
          <div v-if="readiness.missing_items?.length">
            <span>缺少成本项</span><code
              v-for="item in readiness.missing_items"
              :key="item"
            >{{ item }}</code>
          </div>
          <div v-if="readiness.indicative_items?.length">
            <span>参考价成本项</span><code
              v-for="item in readiness.indicative_items"
              :key="item"
            >{{ item }}</code>
          </div>
        </div>
        <dl><div><dt>单位完整成本</dt><dd>{{ selectedSheet.unit_full_cost ? `${selectedSheet.unit_full_cost.amount} ${selectedSheet.unit_full_cost.currency}` : "尚不可计算" }}</dd></div><div><dt>汇率快照</dt><dd>{{ selectedSheet.fx_snapshot_id ?? "未绑定" }}</dd></div><div><dt>创建时间</dt><dd>{{ formatDate(selectedSheet.created_at) }}</dd></div></dl>
      </aside>
    </section>
  </div>
</template>

<style scoped>
.costing-shell { overflow: auto; }
.costing-head { justify-content: space-between; padding-top: var(--space3); }
.panel { border: 1px solid var(--border); border-radius: 12px; background: var(--surface); padding: var(--space4); }
.panel > header { display: flex; justify-content: space-between; gap: var(--space3); align-items: start; }
.opportunity-bar { display: flex; gap: var(--space3); align-items: end; }
.opportunity-bar label { flex: 1; }
label { display: grid; gap: var(--space1); color: var(--text-secondary); font-size: 11px; font-weight: 700; }
input, select { min-width: 0; border: 1px solid var(--border); border-radius: var(--radius-sm); background: white; padding: 9px 10px; color: var(--text-primary); }
button { border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 9px 13px; background: white; cursor: pointer; }
.btn-primary { border-color: var(--action); background: var(--action); color: white; }
button:disabled { cursor: not-allowed; opacity: .55; }
.workspace-grid { display: grid; grid-template-columns: 1fr 1fr; gap: var(--space4); }
.detail-grid { display: grid; grid-template-columns: minmax(0, 1.6fr) minmax(280px, .7fr); gap: var(--space4); }
.field-grid { display: grid; grid-template-columns: 1fr 1fr; gap: var(--space3); margin: var(--space4) 0; }
.field-grid .wide { grid-column: 1 / -1; }
.versions-panel { display: grid; align-content: start; gap: var(--space2); }
.version-row { display: flex; justify-content: space-between; align-items: center; width: 100%; text-align: left; }
.version-row.selected { border-color: var(--action); box-shadow: 0 0 0 1px var(--action); }
.version-row span:first-child { display: grid; gap: 3px; }
small, .muted { color: var(--text-secondary); overflow-wrap: anywhere; }
.state-pill { border-radius: 999px; background: var(--fact-soft); color: var(--fact); padding: 3px 8px; font-size: 10px; font-weight: 800; }
.item-list { display: grid; gap: var(--space2); margin-top: var(--space3); }
.item-row { display: grid; grid-template-columns: 1fr auto minmax(170px, .8fr); gap: var(--space3); align-items: center; border: 1px solid var(--border); border-radius: var(--radius-sm); padding: var(--space3); }
.item-row > div { display: grid; gap: 3px; }
.amount { font-variant-numeric: tabular-nums; font-weight: 800; }
.source span { color: var(--text-secondary); font-size: 10px; }
.source strong { overflow-wrap: anywhere; font-size: 11px; }
.item-form { border-top: 1px solid var(--border); padding-top: var(--space4); }
.check { display: flex; align-items: center; gap: var(--space2); }
.check input { width: auto; }
.readiness-panel { align-self: start; display: grid; gap: var(--space3); }
.readiness-result { border-left: 4px solid var(--danger); background: var(--danger-soft); padding: var(--space3); }
.readiness-result.ready { border-left-color: var(--success); background: var(--success-soft); }
.readiness-result ul { padding-left: 18px; }
.readiness-result div { display: flex; flex-wrap: wrap; gap: var(--space1); margin-top: var(--space2); }
code { border-radius: 4px; background: rgba(255,255,255,.7); padding: 2px 5px; }
dl { display: grid; gap: var(--space2); margin: 0; }
dl div { display: grid; grid-template-columns: 90px 1fr; gap: var(--space2); }
dt { color: var(--text-secondary); font-size: 11px; }
dd { margin: 0; overflow-wrap: anywhere; font-size: 12px; }
.empty { display: grid; min-height: 120px; place-items: center; color: var(--text-secondary); }
@media (max-width: 960px) { .workspace-grid, .detail-grid { grid-template-columns: 1fr; } }
@media (max-width: 640px) { .opportunity-bar { align-items: stretch; flex-direction: column; } .field-grid, .item-row { grid-template-columns: 1fr; } .field-grid .wide { grid-column: auto; } }
</style>
