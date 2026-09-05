<script setup lang="ts">
/* global setInterval, clearInterval, Event, HTMLSelectElement, fetch */
import { onMounted, onUnmounted, ref } from "vue";
import { apiClient, configureControlledIdentity, controlledWebConfig } from "../api/client";
const config = controlledWebConfig();
const selected = ref(apiClient.identitySnapshot().identity?.employeeId ?? "");
const health = ref("正在核对进程");
let timer: ReturnType<typeof setInterval> | undefined;
let active = true;
function change(event: Event): void {
  const value = (event.target as HTMLSelectElement).value;
  configureControlledIdentity(value);
  selected.value = value;
}
async function refresh(): Promise<void> {
  try {
    const response = await fetch("/__controlled/status");
    const state = await response.json();
    if (active) health.value = response.ok && state.status === "ready" ? "API / 调度 / Web 当前就绪" : "进程未就绪或状态未知";
  } catch { if (active) health.value = "进程状态未知"; }
}
onMounted(() => { if (config) { void refresh(); timer = setInterval(() => { void refresh(); }, 2000); } });
onUnmounted(() => { active = false; if (timer) clearInterval(timer); });
</script>
<template>
  <section
    v-if="config"
    class="controlled-mode"
    aria-label="本机受控演练"
  >
    <strong>本机受控演练 · 开发身份</strong>
    <label>演练角色 <select
      :value="selected"
      @change="change"
    ><option
      v-for="identity in config.identities"
      :key="identity.employeeId"
      :value="identity.employeeId"
    >{{ identity.label }}</option></select></label>
    <span role="status">{{ health }}</span>
    <details><summary>待配置与能力边界</summary><p>先通过设置提案与独立审批配置业务。发件身份登记和预热入口待完成；自动正文入站、完整回复、通知投递 worker、Agent、Browser 未启用。研究、联系人和寻源外部场景未装配。所有邮件仅进入本次受控邮箱。</p></details>
  </section>
</template>
<style scoped>
.controlled-mode { display:flex; gap:8px 16px; flex-wrap:wrap; align-items:center; padding:8px 16px; color:#713f12; background:#fef3c7; border-bottom:1px solid #d97706; }
.controlled-mode select { max-width:100%; padding:3px; }
.controlled-mode details { max-width:640px; }
</style>
