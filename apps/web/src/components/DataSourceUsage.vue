<script setup lang="ts">
import type { PlatformDataSource } from "../api/platform-auth";

defineProps<{ source: PlatformDataSource; checking: boolean; disabled: boolean; failure?: string }>();
defineEmits<{ check: [] }>();
const labels = {
  not_checked: "尚未检查", available: "额度正常", low: "额度即将用完", partial_exhausted: "部分额度已用完",
  exhausted: "额度已用完", credential_invalid: "密钥无效或无权限", payment_required: "需要充值或升级",
  rate_limited: "暂时限流", unavailable: "暂时无法查询", unknown: "额度未知",
};
const units = { credits: "积分", searches: "邮箱查找", verifications: "邮箱验证" };
function time(value: string | null | undefined): string {
  if (!value) return "尚无记录";
  return new Date(value).toLocaleString("zh-CN");
}
</script>

<template>
  <div class="usage-cell">
    <span v-if="!source.definition.credential_fields.length">{{ source.definition.credential_mode === "public" ? "公开 API，额度查询未接入" : "开通接口后检查" }}</span>
    <span v-else-if="!source.configured">配置密钥后检查</span>
    <template v-else-if="!source.can_check_usage">
      <span>额度查询待接入</span>
      <small>可在服务商官网查看余额</small>
    </template>
    <template v-else>
      <span
        class="usage-badge"
        :data-status="source.usage?.status ?? 'not_checked'"
      >{{ labels[source.usage?.status ?? 'not_checked'] }}</span>
      <small v-if="source.usage?.stale">上次结果已过期，等待更新</small>
      <p
        v-for="counter in source.usage?.counters ?? []"
        :key="counter.kind"
        class="usage-counter"
      >
        {{ units[counter.kind] }}：剩余 <strong>{{ counter.remaining ?? '未知' }}</strong>
        <small v-if="counter.limit != null">总额度 {{ counter.limit }}<template v-if="counter.used != null"> · 已用 {{ counter.used }}</template></small>
      </p>
      <small v-if="source.usage?.status === 'low'">提醒阈值：剩余不超过总量的 10%</small>
      <small v-if="source.usage?.counters.some(counter => counter.kind === 'credits') && source.usage.counters.length > 1">查找与验证共享积分，可用次数不能相加。</small>
      <small v-if="source.usage?.status === 'rate_limited'">请求频率受限，不代表余额为零</small>
      <small v-if="source.usage?.reset_date">额度重置：{{ source.usage.reset_date }}</small>
      <small v-if="source.usage?.checked_at">检查于 {{ time(source.usage.checked_at) }}</small>
      <small
        v-if="failure"
        class="usage-failure"
        role="alert"
      >{{ failure }}</small>
      <button
        type="button"
        :disabled="disabled || checking"
        @click="$emit('check')"
      >
        {{ checking ? '正在检查…' : '检查额度' }}
      </button>
    </template>
  </div>
</template>

<style scoped>
.usage-cell { display: grid; gap: 7px; min-width: 125px; overflow-wrap: anywhere; }
.usage-cell small { display: block; color: #687585; font-size: 12px; line-height: 1.5; }
.usage-counter { margin: 0; font-size: 13px; }
.usage-badge { width: fit-content; border-radius: 5px; padding: 4px 7px; background: #eff2f5; font-size: 12px; }
.usage-badge[data-status="available"] { background: #e7f5ec; color: #236641; }
.usage-badge[data-status="low"], .usage-badge[data-status="partial_exhausted"], .usage-badge[data-status="rate_limited"] { background: #fff3d9; color: #855300; }
.usage-badge[data-status="exhausted"], .usage-badge[data-status="credential_invalid"], .usage-badge[data-status="payment_required"] { background: #fdecea; color: #a32e25; }
.usage-cell .usage-failure { color: #a32e25; }
.usage-cell button { justify-self: start; }
</style>
