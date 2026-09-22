<script setup lang="ts">
import { codeLabel } from "../../components/displayLabels";
import { computed } from "vue";

import type { components } from "../../api/api";
import MessageEvidenceDownload from "../../components/MessageEvidenceDownload.vue";
import ProvenancePopover from "../../components/ProvenancePopover.vue";

type HandoffPacketView = components["schemas"]["HandoffPacketView"];
type OpportunityView = components["schemas"]["OpportunityView"];
type ProvenanceSummary = components["schemas"]["domains__opportunities__schemas__ProvenanceSummary"];

const props = defineProps<{
  opportunity: OpportunityView;
  packet: HandoffPacketView;
}>();

const emit = defineEmits<{ denied: [status: number] }>();

function provenanceFor(fieldName: string): ProvenanceSummary | undefined {
  return props.opportunity.provenance?.find((item) => item.field_name === fieldName);
}

function displayBoolean(value: boolean | null | undefined): string | null {
  if (value === null || value === undefined) return null;
  return value ? "是" : "否";
}

const opportunityFacts = computed(() => [
  { fieldName: "product_category", label: "产品品类", value: props.opportunity.product_category },
  { fieldName: "quantity", label: "数量", value: props.opportunity.quantity?.toString() ?? null },
  { fieldName: "spec_summary", label: "需求规格", value: props.opportunity.spec_summary ?? null },
  { fieldName: "destination", label: "目的地", value: props.opportunity.destination ?? null },
  { fieldName: "required_by", label: "要求时间", value: props.opportunity.required_by ?? null },
  {
    fieldName: "current_supply_problem",
    label: "当前供应问题",
    value: props.opportunity.current_supply_problem ?? null,
  },
  { fieldName: "can_source", label: "可寻源", value: displayBoolean(props.opportunity.can_source) },
]);

function evidenceLabel(provenance: ProvenanceSummary): string {
  if(provenance.source_type === "agent_inference") return "智能助手推断";
  if(provenance.confirmed_by) return "人工确认";
  if(provenance.source_type === "conversation") return "客户会话来源（字段确认见来源）";
  return "来源记录（尚未人工确认）";
}
const messages = computed(() => [...new Set([
  ...(props.packet.evidence_links ?? []), ...(props.opportunity.provenance ?? []).map(item => item.source_id),
].filter(value => /^msg_[0-7][0-9A-HJKMNP-TV-Z]{25}$/.test(value)))]);

const missingInformation = computed(() => props.packet.missing_information ?? []);
const alreadySent = computed(() => props.packet.already_sent ?? []);
const commitmentsMade = computed(() => props.packet.commitments_made ?? []);
const evidenceCount = computed(() => props.packet.evidence_links?.length ?? 0);
</script>

<template>
  <article
    class="handoff-packet"
    aria-label="完整接管包"
  >
    <header class="packet-header">
      <div class="packet-badges">
        <span class="state-tag"><span aria-hidden="true">●</span> {{ codeLabel(packet.state) }}</span>
      </div>
      <h1>{{ packet.account_name }}</h1>
      <p class="record-ids">
        接管 {{ packet.handoff_id }} · 机会 {{ packet.opportunity_id }} · 需求 {{ opportunity.need_id }} · 企业 {{ opportunity.account_id }}
      </p>
      <dl class="summary-grid">
        <div><dt>触发条件</dt><dd>{{ packet.trigger }}</dd></div>
        <div><dt>请求时间</dt><dd>{{ packet.requested_at }}</dd></div>
        <div><dt>已等待（秒）</dt><dd>{{ packet.wait_seconds ?? "暂不可用" }}</dd></div>
        <div><dt>国家 / 地区</dt><dd>{{ packet.country }}</dd></div>
        <div><dt>负责人</dt><dd>{{ packet.assigned_to_name ?? "负责人姓名暂不可用" }}</dd></div>
      </dl>
    </header>

    <section class="packet-section">
      <p class="section-kicker">
        来源记录
      </p>
      <h2>关键字段</h2>
      <dl class="fact-grid">
        <div
          v-for="field in opportunityFacts.filter((item) => item.value !== null)"
          :key="field.fieldName"
          class="fact-field"
          :class="{ 'fact-field--missing': !provenanceFor(field.fieldName) }"
        >
          <div>
            <dt>{{ field.label }}</dt>
            <dd>{{ field.value }}</dd>
            <span
              v-if="provenanceFor(field.fieldName)"
              class="fact-label"
            ><span aria-hidden="true">✓</span> {{ evidenceLabel(provenanceFor(field.fieldName)!) }}</span>
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

    <section class="packet-section context-section">
      <p class="section-kicker">
        接管背景
      </p>
      <h2>完整上下文</h2>
      <dl class="context-grid">
        <div><dt>发现方式</dt><dd>{{ packet.how_we_found_them ?? "暂不可用" }}</dd></div>
        <div><dt>已验证需求摘要</dt><dd>{{ packet.validated_need_summary ?? "暂不可用" }}</dd></div>
        <div>
          <dt>客户原话</dt><dd class="verbatim">
            {{ packet.customer_verbatim }}
          </dd>
        </div>
        <div><dt>对话摘要</dt><dd>{{ packet.conversation_summary ?? "暂不可用" }}</dd></div>
      </dl>
    </section>

    <section class="packet-section">
      <div class="inference-panel">
        <h2><span class="inference-label"><span aria-hidden="true">◇</span> 推断</span> 价值与建议</h2>
        <dl>
          <div><dt>价值说明</dt><dd>{{ packet.why_valuable }}</dd></div>
          <div><dt>建议下一步</dt><dd>{{ packet.suggested_next_step ?? "暂不可用" }}</dd></div>
        </dl>
      </div>
    </section>

    <section class="packet-section packet-lists">
      <div>
        <h2>缺失信息</h2>
        <p v-if="missingInformation.length === 0">
          当前响应未列出缺失信息。
        </p>
        <ul v-else>
          <li
            v-for="item in missingInformation"
            :key="item"
          >
            {{ item }}
          </li>
        </ul>
      </div>
      <div>
        <h2>已发送内容</h2>
        <p v-if="alreadySent.length === 0">
          当前响应未列出已发送内容。
        </p>
        <ul v-else>
          <li
            v-for="item in alreadySent"
            :key="item"
          >
            {{ item }}
          </li>
        </ul>
      </div>
      <div>
        <h2>已作承诺</h2>
        <p v-if="commitmentsMade.length === 0">
          当前响应未列出承诺。
        </p>
        <ul v-else>
          <li
            v-for="item in commitmentsMade"
            :key="item"
          >
            {{ item }}
          </li>
        </ul>
      </div>
      <div class="evidence-entry">
        <h2>证据入口</h2>
        <p>证据入口 {{ evidenceCount }} 项（受权限保护）</p>
        <RouterLink :to="`/demand/needs/${opportunity.need_id}`">
          查看精确需求证据链
        </RouterLink>
        <RouterLink :to="{path:'/inbox',query:{account_id:opportunity.account_id}}">
          查看该企业最近已授权会话与邮件原件
        </RouterLink>
        <MessageEvidenceDownload
          v-for="messageId in messages"
          :key="messageId"
          :message-id="messageId"
          @denied="emit('denied', $event)"
        />
        <p v-if="!messages.length">
          当前接管包未提供可直接下载的消息编号，请进入会话核对。原始资料定位符不可直接下载。
        </p>
      </div>
    </section>
  </article>
</template>

<style scoped>
.evidence-entry {display:grid;gap:8px;}
.handoff-packet {
  padding: 18px;
}

.packet-header {
  padding-bottom: 15px;
  border-bottom: 1px solid #cbd7d5;
}

.packet-badges,
.summary-grid,
.fact-grid,
.context-grid,
.packet-lists {
  display: grid;
  gap: 10px;
}

.packet-badges {
  grid-auto-flow: column;
  justify-content: start;
}

.demo-badge,
.state-tag,
.fact-label,
.inference-label {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  min-height: 23px;
  padding: 2px 7px;
  border-radius: 6px;
  font-size: 11px;
  font-weight: 750;
  line-height: 1.2;
}

.demo-badge {
  color: #445554;
  border: 1px solid #cbd7d5;
  background: #e9efee;
}

.state-tag,
.fact-label {
  color: #135f59;
  border: 1px solid #88c9c0;
  background: #e7f5f2;
}

h1,
h2,
p {
  margin-top: 0;
}

h1 {
  margin: 10px 0 3px;
  color: #172323;
  font-size: 22px;
  line-height: 1.25;
}

h2 {
  margin-bottom: 8px;
  color: #172323;
  font-size: 16px;
  line-height: 1.4;
}

.record-ids,
dt,
.section-kicker,
.source-missing {
  color: #526363;
  font-size: 12px;
}

.record-ids {
  margin-bottom: 13px;
  overflow-wrap: anywhere;
}

.summary-grid {
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.summary-grid > div,
.context-grid > div {
  min-width: 0;
}

dd {
  margin: 3px 0 0;
  overflow-wrap: anywhere;
  color: #172323;
  font-weight: 650;
}

.packet-section {
  padding: 16px 0;
  border-bottom: 1px solid #e1e8e6;
}

.section-kicker {
  margin-bottom: 3px;
  color: #0f766e;
  font-size: 11px;
  font-weight: 780;
  letter-spacing: 0.08em;
}

.fact-grid {
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.fact-field {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  min-height: 88px;
  padding: 11px;
  border: 1px solid #67a9a0;
  border-radius: 9px;
  background: #e7f5f2;
}

.fact-field--missing {
  border-color: #cbd7d5;
  background: #f9fbfa;
}

.fact-field dd {
  margin-bottom: 6px;
}

.source-missing {
  display: inline-block;
}

.context-grid {
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.context-grid > div {
  padding: 9px 0;
  border-bottom: 1px solid #e1e8e6;
}

.verbatim {
  white-space: pre-wrap;
}

.inference-panel {
  padding: 13px;
  border: 1px dashed #b98722;
  border-radius: 9px;
  background: #fff5d6;
}

.inference-panel h2 {
  display: flex;
  align-items: center;
  gap: 7px;
}

.inference-label {
  color: #8a5c00;
  border: 1px dashed #b98722;
  background: #fff8e5;
}

.inference-panel dl,
.inference-panel dd {
  margin: 0;
}

.inference-panel dl {
  display: grid;
  gap: 10px;
}

.packet-lists {
  grid-template-columns: repeat(2, minmax(0, 1fr));
  border-bottom: 0;
}

.packet-lists h2 {
  margin-bottom: 5px;
  font-size: 14px;
}

.packet-lists p,
.packet-lists ul {
  margin: 0;
  color: #526363;
}

.packet-lists ul {
  padding-left: 19px;
}

.evidence-entry {
  padding: 10px;
  border: 1px solid #cbd7d5;
  border-radius: 9px;
  background: #f9fbfa;
}

@media (max-width: 1240px) {
  .handoff-packet {
    padding: 15px;
  }

  .fact-grid,
  .context-grid,
  .packet-lists {
    grid-template-columns: 1fr;
  }
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
