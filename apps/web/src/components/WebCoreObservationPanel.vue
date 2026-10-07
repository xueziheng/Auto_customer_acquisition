<script setup lang="ts">
import { codeLabel } from "./displayLabels";
import type { components } from '../api/api';
defineProps<{
  observation: components['schemas']['WebCoreObservation'] | null;
  error: string | null;
  loading: boolean;
}>();
const stages: Record<components['schemas']['StageObservation']['stage'], string> = {
  demand_signal: '需求信号', need_hypothesis: '需求假设', validated_need: '当前有效已验证需求',
  opportunity_record: '贸易机会记录', supply_match: '供应匹配', quote_record: '报价记录（全部状态）',
  human_execution: '已接受接管的机会', deal_outcome: '成交结果（已关闭）',
};
const missing: Record<string, string> = {
  qualification_evidence_missing: '缺少五项资格联合证据：可接触客户、真实需求、可供应、可接受利润、执行团队',
  run_entity_attribution_missing: '缺少完整运行记录与阶段实体归因',
  supply_match_source_missing: '缺少统一已确认供应匹配来源',
  provider_token_usage_missing: '缺少模型供应商实际模型计量用量',
  human_time_records_missing: '缺少人工实际工作计时',
  rate_card_missing: '缺少可信费率',
};
function money(value: components['schemas']['Money'] | null | undefined): string {
  return value ? `${value.amount} ${value.currency}` : '未知';
}
function time(value: string): string { return new Date(value).toLocaleString('zh-CN', { hour12: false }); }
</script>
<template>
  <section
    class="observation-panel"
    aria-label="租户观测"
  >
    <h2>业务记录与成本输入</h2>
    <p
      v-if="loading"
      role="status"
    >
      正在读取观测…
    </p>
    <p
      v-else-if="error"
      role="alert"
    >
      观测读取失败：{{ error }}
    </p>
    <template v-else-if="observation">
      <p>范围：当前租户 · 各阶段独立窗口 [{{ time(observation.window_start) }}，{{ time(observation.window_end) }})；观测于 {{ time(observation.observed_at) }}。完整性：部分。</p>
      <p>不是同一批需求的转化漏斗，不随运行记录列表筛选变化。状态为观测时的当前快照，不是历史状态。受控数据不代表真实获客成绩。</p>
      <div class="stage-grid">
        <div
          v-for="item in observation.stages"
          :key="item.stage"
        >
          <span>{{ stages[item.stage] }}</span><strong>{{ item.count ?? '未知' }}</strong>
          <small>{{ codeLabel(item.source) }} · {{ codeLabel(item.time_field ?? '缺少时间来源') }}</small>
        </div>
      </div>
      <p>已验证需求仅含当前已验证、可进入寻源或已交接寻源的记录；不含已履行、撤回或丢失。报价记录不等于已批准或已发送。</p>
      <p><strong>合格贸易机会：{{ observation.qualified_opportunity_count ?? '未知' }}</strong></p>
      <ul>
        <li
          v-for="item in observation.missing_inputs"
          :key="item"
        >
          {{ missing[item] ?? '来源缺项' }}
        </li>
      </ul>
      <details>
        <summary>成本输入与当前接管积压</summary>
        <p>输入模型计量单位：{{ observation.inputs?.model_input_tokens ?? '未知' }} · 输出模型计量单位：{{ observation.inputs?.model_output_tokens ?? '未知' }} · 人工工作秒数：{{ observation.inputs?.human_work_seconds ?? '未知' }}</p>
        <p>费用总额：{{ money(observation.inputs?.total_cost) }} · 单位合格机会成本：{{ money(observation.inputs?.cost_per_qualified_opportunity) }}</p>
        <ul>
          <li
            v-for="item in observation.inputs?.missing_inputs"
            :key="item"
          >
            {{ missing[item] ?? '来源缺项' }}
          </li>
        </ul>
        <p>来源：工具调用记录的创建时间；统计窗口内创建记录的当前累计尝试次数，不是窗口内发生量。调用记录与执行尝试分列，成本级别不是实付费用。</p>
        <p v-if="!observation.source_calls.length">
          该窗口无工具调用记录。
        </p>
        <p
          v-for="item in observation.source_calls"
          :key="item.tool_id"
        >
          {{ codeLabel(item.tool_id) }}：调用 {{ item.call_count }} · 尝试 {{ item.attempt_count }} · 重放回执 {{ item.duplicate_receipt_count }}
        </p>
        <p>Tavily 额度（仅本地搜索额度预留记录）：已消耗 {{ observation.consumed_credits }} / 已预留 {{ observation.reserved_credits }} / 不确定 {{ observation.uncertain_credits }}。按预留创建窗口及当前状态计数，不等于本窗口实际结算。</p>
        <p>当前租户待接管：{{ observation.handoffs.queue_depth }}；最久等待 {{ observation.handoffs.oldest_wait_seconds === null ? '未知或不适用' : `${observation.handoffs.oldest_wait_seconds} 秒` }}；时间异常 {{ observation.handoffs.invalid_time_count }}。</p>
        <p>来源：人工接管请求；统计当前全队列，不限上述创建窗口。等待时长不是人工工作耗时。</p>
        <ul>
          <li
            v-for="item in observation.handoffs.by_employee"
            :key="item.employee_id ?? 'unassigned'"
          >
            {{ item.employee_id ?? '未分配，需经理处理' }}：{{ item.queue_depth }} 项
          </li>
        </ul>
      </details>
    </template>
  </section>
</template>
<style scoped>
.observation-panel { margin: 16px 0; padding: 18px; border: 1px solid #cbd7d5; border-radius: 12px; background: white; overflow-wrap: anywhere; }
h2 { margin: 0 0 12px; font-size: 18px; } p, li { font-size: 13px; line-height: 1.6; color: #435c57; }
.stage-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }
.stage-grid div { display: grid; gap: 6px; padding: 12px; background: #f2f7f5; border-radius: 8px; }
strong { color: #173b39; } .stage-grid strong { font-size: 24px; } small { font-size: 11px; color: #526363; }
summary { min-height: 44px; padding: 12px 0; cursor: pointer; font-weight: 700; }
@media (max-width: 760px) { .stage-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } .observation-panel { padding: 12px; } }
</style>
