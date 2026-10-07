<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { RouterLink } from "vue-router";

import ProvenancePopover from "../../components/ProvenancePopover.vue";
import type { components } from "../../api/api";

type LossReason = components["schemas"]["LossReason"];
type Money = components["schemas"]["Money"];
type OpportunityMarkLostBody = components["schemas"]["OpportunityMarkLostBody"];
type OpportunityState = components["schemas"]["OpportunityState"];
type OpportunityView = components["schemas"]["OpportunityView"];
type ProvenanceSummary = components["schemas"]["domains__opportunities__schemas__ProvenanceSummary"];

const props = defineProps<{
  actionStatus: string;
  opportunity: OpportunityView;
  writePending: boolean;
}>();

const emit = defineEmits<{
  markLost: [payload: OpportunityMarkLostBody];
  transition: [target: OpportunityState];
}>();

const stateLabels: Record<OpportunityState, string> = {
  qualified: "已合格",
  assigned: "已分配",
  contacted: "已联系",
  sourcing: "寻源中",
  quoted: "已报价",
  negotiating: "洽谈中",
  won: "已成交",
  lost: "已流失",
};

const transitionTargets: Record<OpportunityState, OpportunityState[]> = {
  qualified: ["assigned"],
  assigned: ["contacted"],
  contacted: ["sourcing", "quoted"],
  sourcing: ["quoted"],
  quoted: ["negotiating"],
  negotiating: [],
  won: [],
  lost: [],
};

const lossReasonLabels: Record<LossReason, string> = {
  unreachable: "无法联系",
  no_reply: "序列结束无回复",
  need_not_real: "需求不成立",
  no_supply_found: "未找到供应",
  price_too_high: "价格原因",
  lost_to_competitor: "输给竞争对手",
  customer_went_silent: "客户中途失联",
  timing_mismatch: "时间要求不匹配",
  compliance_blocked: "合规阻断",
  margin_too_low: "利润空间过低",
  internal_no_capacity: "内部无承接能力",
  duplicate: "重复机会",
};

const lossReasons = Object.keys(lossReasonLabels) as LossReason[];
const selectedTarget = ref<OpportunityState | "">("");
const selectedLossReason = ref<LossReason>("unreachable");
const lossDetail = ref("");

function isOpportunityState(value: string): value is OpportunityState {
  return Object.hasOwn(stateLabels, value);
}

const currentState = computed<OpportunityState | null>(() =>
  isOpportunityState(props.opportunity.state) ? props.opportunity.state : null,
);
const availableTargets = computed(() => (currentState.value ? transitionTargets[currentState.value] : []));
const statusLabel = computed(() => (currentState.value ? stateLabels[currentState.value] : props.opportunity.state));
const rankBucketLabel = computed(() => {
  const bucket = props.opportunity.score?.rank_bucket;
  if (bucket === "high") return "排序桶：高";
  if (bucket === "mid") return "排序桶：中";
  if (bucket === "low") return "排序桶：低";
  return "排序桶：未知";
});

watch(
  () => [props.opportunity.opportunity_id, props.opportunity.state],
  () => {
    selectedTarget.value = availableTargets.value[0] ?? "";
    selectedLossReason.value = "unreachable";
    lossDetail.value = "";
  },
  { immediate: true },
);

function provenanceFor(fieldName: string): ProvenanceSummary | undefined {
  return props.opportunity.provenance?.find((item) => item.field_name === fieldName);
}

function displayMoney(money: Money | null | undefined): string | null {
  return money ? `${money.amount} ${money.currency}` : null;
}

const factFields = computed(() => [
  { fieldName: "quantity", label: "数量", value: props.opportunity.quantity?.toString() ?? null },
  { fieldName: "spec_summary", label: "需求规格", value: props.opportunity.spec_summary ?? null },
  { fieldName: "destination", label: "目的地", value: props.opportunity.destination ?? null },
  { fieldName: "required_by", label: "要求时间", value: props.opportunity.required_by ?? null },
  {
    fieldName: "current_supply_problem",
    label: "当前供应问题",
    value: props.opportunity.current_supply_problem ?? null,
  },
  {
    fieldName: "can_source",
    label: "可寻源",
    value: props.opportunity.can_source === null || props.opportunity.can_source === undefined
      ? null
      : props.opportunity.can_source
        ? "是"
        : "否",
  },
]);

const amountFields = computed(() => [
  { fieldName: "target_price", label: "目标价格", value: displayMoney(props.opportunity.target_price) },
  { fieldName: "estimated_cost", label: "估算成本", value: displayMoney(props.opportunity.estimated_cost) },
  { fieldName: "estimated_profit", label: "估算利润", value: displayMoney(props.opportunity.estimated_profit) },
]);

function submitTransition(): void {
  if (props.writePending || !selectedTarget.value) return;
  emit("transition", selectedTarget.value);
}

function submitMarkLost(): void {
  if (props.writePending) return;
  emit("markLost", {
    reason: selectedLossReason.value,
    detail: lossDetail.value.trim() || null,
  });
}
</script>

<template>
  <article
    class="opportunity-record"
    :aria-busy="writePending"
  >
    <header class="record-header">
      <div class="heading-row">
        <span class="status-tag"><span aria-hidden="true">●</span> {{ statusLabel }}</span>
      </div>
      <h1>{{ opportunity.account_name }}</h1>
      <ProvenancePopover
        v-if="provenanceFor('account_name')"
        field-label="客户名称"
        :provenance="provenanceFor('account_name')!"
      />
      <p class="record-id">
        {{ opportunity.opportunity_id }}
      </p>
      <div class="summary-grid">
        <div><span>负责人</span><strong>{{ opportunity.owner_name ?? opportunity.owner ?? "未分配" }}</strong></div>
        <div>
          <span>国家 / 地区</span><strong>{{ opportunity.country }}</strong>
          <ProvenancePopover
            v-if="provenanceFor('country')"
            field-label="国家 / 地区"
            :provenance="provenanceFor('country')!"
          />
        </div>
        <div><span>产品品类</span><strong>{{ opportunity.product_category }}</strong></div>
        <div>
          <span>下一步 / 到期</span>
          <strong>{{ opportunity.next_action ?? "待确认" }} · {{ opportunity.next_action_due ?? "未设置" }}</strong>
        </div>
        <div><span>账户编号</span><strong>{{ opportunity.account_id }}</strong></div>
        <div>
          <span>需求编号</span><RouterLink :to="{ name: 'validated-need-detail', params: { needId: opportunity.need_id } }">
            {{ opportunity.need_id }}
          </RouterLink>
        </div>
        <div><span>创建时间</span><strong>{{ opportunity.created_at }}</strong></div>
        <div>
          <span>待处理接管</span>
          <strong>{{ opportunity.has_pending_handoff ? "有待处理接管" : "无待处理接管" }}</strong>
        </div>
      </div>
      <RouterLink :to="{ path: '/costing-quotes', query: { opportunity_id: opportunity.opportunity_id } }">
        查看此机会的成本与报价
      </RouterLink>
    </header>

    <section
      class="record-section"
      aria-labelledby="facts-title"
    >
      <div class="section-heading">
        <div>
          <p class="section-kicker">
            来源记录
          </p><h2 id="facts-title">
            关键字段
          </h2>
        </div>
      </div>
      <dl class="fact-grid">
        <div
          v-for="field in factFields.filter((item) => item.value !== null)"
          :key="field.fieldName"
          class="fact-box"
          :class="{ 'fact-box--missing': !provenanceFor(field.fieldName) }"
        >
          <div>
            <dt>{{ field.label }}</dt>
            <dd>{{ field.value }}</dd>
            <span
              v-if="provenanceFor(field.fieldName)"
              class="fact-label"
            ><span aria-hidden="true">✓</span> 来源记录</span>
            <span
              v-else
              class="source-missing"
            >来源摘要暂不可用</span>
          </div>
          <ProvenancePopover
            v-if="provenanceFor(field.fieldName)"
            :field-label="field.label"
            :provenance="provenanceFor(field.fieldName)!"
          />
        </div>
      </dl>
    </section>

    <section
      class="record-section"
      aria-labelledby="inference-title"
    >
      <div class="inference-box">
        <div class="section-heading">
          <h2 id="inference-title">
            <span class="inference-label"><span aria-hidden="true">◇</span> 推断</span> 为什么值得跟进
          </h2>
          <span class="rank-tag">{{ rankBucketLabel }}</span>
        </div>
        <p v-if="!opportunity.score">
          后端暂未提供跟进解释。
        </p>
        <ul
          v-if="opportunity.score"
          class="gate-list"
        >
          <li
            v-for="gate in opportunity.score.passed_gates"
            :key="`pass-${gate}`"
          >
            <span aria-hidden="true">✓</span><span>通过：{{ gate }}</span>
          </li>
          <li
            v-for="gate in opportunity.score.failed_gates"
            :key="`fail-${gate}`"
          >
            <span aria-hidden="true">!</span><span>待补：{{ opportunity.score.gate_reasons[gate] ?? gate }}</span>
          </li>
        </ul>
      </div>
    </section>

    <section
      v-if="amountFields.some((item) => item.value !== null)"
      class="record-section"
      aria-labelledby="amount-title"
    >
      <p class="section-kicker">
        金额摘要
      </p>
      <h2 id="amount-title">
        金额摘要
      </h2>
      <dl class="amount-grid">
        <div
          v-for="field in amountFields.filter((item) => item.value !== null)"
          :key="field.fieldName"
          class="amount-box"
        >
          <div>
            <dt>{{ field.label }}</dt><dd>{{ field.value }}</dd>
            <span
              v-if="provenanceFor(field.fieldName)"
              class="fact-label"
            ><span aria-hidden="true">✓</span> 来源记录</span>
            <span
              v-else
              class="source-missing"
            >来源摘要暂不可用</span>
          </div>
          <ProvenancePopover
            v-if="provenanceFor(field.fieldName)"
            :field-label="field.label"
            :provenance="provenanceFor(field.fieldName)!"
          />
        </div>
      </dl>
    </section>

    <section
      v-if="opportunity.loss_reason || opportunity.died_at_state"
      class="record-section"
      aria-labelledby="loss-title"
    >
      <p class="section-kicker">
        成交结果
      </p>
      <h2 id="loss-title">
        失败闭环
      </h2>
      <p>失败原因：{{ opportunity.loss_reason ? lossReasonLabels[opportunity.loss_reason as LossReason] : "未记录" }}</p>
      <p>终止状态：{{ opportunity.died_at_state ? stateLabels[opportunity.died_at_state as OpportunityState] : "未记录" }}</p>
    </section>

    <section
      class="actions"
      aria-labelledby="actions-title"
    >
      <div class="section-heading">
        <div>
          <p class="section-kicker">
            人工操作
          </p><h2 id="actions-title">
            人工操作
          </h2>
        </div>
        <span class="action-boundary">权限以后台服务校验为准</span>
      </div>
      <div class="controls">
        <select
          v-model="selectedTarget"
          aria-label="选择合法目标状态"
          :disabled="writePending || availableTargets.length === 0"
        >
          <option
            v-if="availableTargets.length === 0"
            value=""
          >
            当前状态无可用推进目标
          </option>
          <option
            v-for="target in availableTargets"
            :key="target"
            :value="target"
          >
            推进为：{{ stateLabels[target] }}
          </option>
        </select>
        <button
          class="primary-action"
          type="button"
          :disabled="writePending || availableTargets.length === 0"
          @click="submitTransition"
        >
          {{ writePending ? "处理中…" : "推进状态" }}
        </button>
      </div>
      <details class="loss-control">
        <summary>标记为流失</summary>
        <div class="loss-form">
          <label>
            流失原因
            <select
              v-model="selectedLossReason"
              aria-label="流失原因"
              :disabled="writePending"
            >
              <option
                v-for="reason in lossReasons"
                :key="reason"
                :value="reason"
              >{{ lossReasonLabels[reason] }}</option>
            </select>
          </label>
          <label>
            补充说明（可选）
            <textarea
              v-model="lossDetail"
              aria-label="补充说明（可选）"
              :disabled="writePending"
              placeholder="只记录必要业务说明，不粘贴客户隐私内容"
            />
          </label>
          <button
            class="danger-action"
            type="button"
            :disabled="writePending"
            @click="submitMarkLost"
          >
            {{ writePending ? "处理中…" : "确认标记流失" }}
          </button>
        </div>
      </details>
      <div
        class="operation-status"
        role="status"
        aria-live="polite"
      >
        {{ actionStatus }}
      </div>
    </section>
  </article>
</template>

<style scoped>
.opportunity-record {
  position: relative;
  min-width: 0;
  overflow: hidden;
  border: 1px solid #cbd7d5;
  border-radius: 11px;
  background: #fff;
  box-shadow: 0 1px 2px rgba(20, 35, 35, 0.08);
}

.opportunity-record:has(.source-trigger[aria-expanded="true"]) {
  margin-right: 304px;
}

.record-header,
.record-section,
.actions {
  padding: 17px 20px;
  border-bottom: 1px solid #e1e8e6;
}

.record-header h1 {
  margin: 9px 0 2px;
  font-size: 22px;
  line-height: 1.25;
}

.heading-row,
.section-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 9px;
}

.status-tag,
.fact-label,
.inference-label,
.rank-tag {
  display: inline-flex;
  min-height: 23px;
  align-items: center;
  gap: 5px;
  padding: 2px 7px;
  border-radius: 6px;
  font-size: 11px;
  font-weight: 750;
  line-height: 1.2;
  white-space: nowrap;
}

.status-tag {
  color: #135f59;
  border: 1px solid #88c9c0;
  background: #e7f5f2;
}

.record-id {
  margin: 0;
  color: #526363;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
}

.summary-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
  margin-top: 14px;
}

.summary-grid > div {
  padding: 9px 10px;
  border-radius: 8px;
  background: #f4f8f7;
}

.summary-grid span,
.section-kicker,
.action-boundary {
  color: #526363;
  font-size: 11px;
}

.summary-grid a,
.summary-grid strong {
  display: block;
  margin-top: 2px;
  overflow-wrap: anywhere;
}

.section-kicker,
.record-section h2,
.actions h2 {
  margin: 0;
}

.record-section h2,
.actions h2 {
  font-size: 18px;
  line-height: 1.35;
}

.fact-grid {
  display: grid;
  gap: 9px;
  margin: 10px 0 0;
}

.fact-box {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 10px;
  padding: 11px;
  border: 1px solid #78b9b1;
  border-left: 4px solid #0f766e;
  border-radius: 9px;
  background: #e7f5f2;
}

.fact-box--missing {
  border: 1px solid #cbd7d5;
  border-left: 4px solid #9cb0ad;
  background: #f9fbfa;
}

.fact-box dt,
.amount-grid dt {
  color: #3e5b58;
  font-size: 11px;
}

.fact-box dd,
.amount-grid dd {
  margin: 3px 0 0;
  overflow-wrap: anywhere;
  font-weight: 700;
}

.fact-label {
  padding-left: 0;
  color: #0c5e57;
}

.source-missing {
  display: block;
  margin-top: 6px;
  color: #8a5c00;
  font-size: 11px;
  font-weight: 650;
}

.inference-box {
  padding: 13px;
  border: 2px dashed #c59b36;
  border-radius: 9px;
  background: #fff5d6;
}

.inference-label {
  padding-left: 0;
  color: #6f4a00;
}

.rank-tag {
  color: #5a4200;
  border: 1px solid #d9bd69;
  background: #fff8e5;
}

.inference-box p {
  margin: 7px 0 9px;
  color: #5e480f;
}

.gate-list {
  display: grid;
  gap: 5px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.gate-list li {
  display: flex;
  gap: 7px;
}

.amount-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
  margin: 10px 0 0;
}

.amount-box {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 8px;
  padding: 10px;
  border: 1px solid #cbd7d5;
  border-radius: 8px;
  background: #f9fbfa;
}

.actions {
  border-bottom: 0;
  background: #fbfcfc;
}

.controls {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 10px;
}

select,
textarea,
button,
summary {
  font: inherit;
}

select,
textarea {
  min-height: 44px;
  padding: 0 10px;
  border: 1px solid #9cb0ad;
  border-radius: 8px;
  background: #fff;
}

button {
  min-height: 44px;
  padding: 0 12px;
  border: 1px solid #9cb0ad;
  border-radius: 8px;
  background: #fff;
  font-weight: 680;
  cursor: pointer;
}

button:disabled,
select:disabled,
textarea:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}

.primary-action {
  color: #fff;
  border-color: #155eef;
  background: #155eef;
}

.loss-control {
  margin-top: 11px;
  border: 1px solid #ddaaa5;
  border-radius: 9px;
  background: #fffafa;
}

.loss-control summary {
  display: flex;
  min-height: 44px;
  align-items: center;
  padding: 8px 12px;
  color: #b42318;
  cursor: pointer;
  font-weight: 700;
}

.loss-form {
  display: grid;
  gap: 8px;
  padding: 0 12px 12px;
}

.loss-form label {
  color: #526363;
  font-size: 12px;
}

.loss-form select,
.loss-form textarea {
  width: 100%;
  margin-top: 4px;
}

.loss-form textarea {
  min-height: 68px;
  padding: 8px;
  resize: vertical;
}

.danger-action {
  color: #b42318;
  border-color: #d88f87;
}

.operation-status {
  margin-top: 10px;
  padding: 9px 10px;
  border: 1px solid #cbd7d5;
  border-radius: 8px;
  background: #fff;
  font-size: 12px;
}

button:focus-visible,
select:focus-visible,
textarea:focus-visible,
summary:focus-visible {
  outline: 3px solid #7c3aed;
  outline-offset: 2px;
}

@media (max-width: 1240px) {
  .opportunity-record:has(.source-trigger[aria-expanded="true"]) {
    margin-right: 260px;
  }

  .record-header,
  .record-section,
  .actions {
    padding-right: 14px;
    padding-left: 14px;
  }

  .amount-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 700px) {
  .summary-grid { grid-template-columns: minmax(0, 1fr); }
  .opportunity-record:has(.source-trigger[aria-expanded="true"]) { margin-right: 0; }
  .record-header h1, .record-id { overflow-wrap: anywhere; }
}

@media (prefers-reduced-motion: reduce) {
  *,
  *::before,
  *::after {
    scroll-behavior: auto !important;
    transition-duration: 0.01ms !important;
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
  }
}
</style>
