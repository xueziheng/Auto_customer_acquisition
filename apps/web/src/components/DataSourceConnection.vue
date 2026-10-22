<script setup lang="ts">
import type { PlatformDataSource } from "../api/platform-auth";
const props = defineProps<{ source: PlatformDataSource; checking: boolean; failure?: string }>();
const sessionLabels = {
  not_checked: "尚未测试登录", available: "登录有效", stale: "上次登录有效，待复测",
  login_required: "需要重新登录", blocked: "网站拒绝访问，登录状态未知",
  rate_limited: "网站暂时限流", unavailable: "暂时无法验证",
};
const websiteLabels = {
  not_checked: "网站尚未测试", available: "网站可访问", stale: "上次可访问，待复测",
  login_required: "网站要求登录", blocked: "网站拒绝访问", rate_limited: "网站暂时限流", unavailable: "暂不可访问",
};
function timestamp(value: string): string { return new Date(value).toLocaleString("zh-CN"); }
function status() {
  if (props.checking) return "正在测试…";
  if (props.failure) return "暂时无法验证";
  return (props.source.status_kind === "session" ? sessionLabels : websiteLabels)[props.source.browser?.status ?? "not_checked"];
}
</script>
<template>
  <div class="connection-status">
    <strong :data-connection-status="failure ? 'unavailable' : checking ? 'checking' : source.browser?.status ?? 'not_checked'">{{ status() }}</strong>
    <small v-if="source.browser?.checked_at && !failure && !checking">上次检查：{{ timestamp(source.browser.checked_at) }}</small>
    <small v-if="source.status_kind === 'session' && source.can_check_browser">服务器专用会话 · 手动测试</small>
    <small v-if="!source.can_check_browser">检测待接入</small>
    <small
      v-if="failure"
      role="alert"
    >{{ failure }}</small>
  </div>
</template>
<style scoped>
.connection-status { display: grid; gap: .45rem; font-size: .85rem; }
small { color: var(--muted, #657575); line-height: 1.5; }
strong { font-weight: 500; }
[data-connection-status="available"] { color: #17735b; }
[data-connection-status="login_required"], [data-connection-status="blocked"], [data-connection-status="unavailable"] { color: #a14925; }
</style>
