<script setup lang="ts">
import { onMounted, onUnmounted, ref } from "vue";
import { apiClient, controlledWebConfig } from "./api/client";
import { RouterView } from "vue-router";
import ControlledModeBar from "./components/ControlledModeBar.vue";
import LoginPanel from "./components/LoginPanel.vue";
import PlatformConsole from "./components/PlatformConsole.vue";
import NotificationBadge from "./components/NotificationBadge.vue";
import WorkspaceNavigation from "./components/WorkspaceNavigation.vue";
import { currentAuthenticationMutation, listenForSessionInvalidation, logout, restoreSession, subscribeAuthenticationMutation, supportsAuthenticationMutations } from "./api/authentication";

const isPlatform = /^\/platform\/?$/.test(window.location.pathname);
const isControlled = controlledWebConfig() !== null;
const isIsolatedDevelopment = import.meta.env.DEV && !import.meta.env.PROD;
const snapshot = ref(apiClient.identitySnapshot());
const identityGeneration = ref(snapshot.value.generation);
const authenticationSupported = supportsAuthenticationMutations();
const authMutation = ref(currentAuthenticationMutation());
const unsubscribeMutation = isPlatform ? () => {} : subscribeAuthenticationMutation(() => { authMutation.value = currentAuthenticationMutation(); });
const loading = ref(!isPlatform && authenticationSupported && !snapshot.value.identity && !isControlled && !isIsolatedDevelopment);
const sessionError = ref("");
const exiting = ref(false);
const unsubscribeIdentity = isPlatform ? () => {} : apiClient.subscribeIdentity(() => {
  snapshot.value = apiClient.identitySnapshot();
  identityGeneration.value = snapshot.value.generation;
});
const stopListening = isPlatform || isControlled || isIsolatedDevelopment ? () => {} : listenForSessionInvalidation();
onUnmounted(() => { unsubscribeIdentity(); unsubscribeMutation(); stopListening(); });
async function restore(): Promise<void> {
  loading.value = true; sessionError.value = "";
  try { await restoreSession(); }
  catch { sessionError.value = "无法恢复会话，请检查本机服务后重试。"; }
  finally { loading.value = false; }
}
async function exit(): Promise<void> {
  if (exiting.value) return;
  exiting.value = true; sessionError.value = "";
  try { await logout(); }
  catch { sessionError.value = "退出未完成：无法确认服务器会话已撤销，请恢复服务后重试退出。"; }
  finally { exiting.value = false; }
}
onMounted(() => { if (loading.value) void restore(); });

const appName: string = "TradeOS";
</script>

<template>
  <PlatformConsole v-if="isPlatform" />
  <main
    v-else
    :aria-label="appName"
  >
    <p
      v-if="!authenticationSupported && !isIsolatedDevelopment"
      role="alert"
      class="session-status"
    >
      当前浏览器不支持安全会话锁，无法登录或退出。请使用新版 Chrome 或 Edge 浏览器打开本机入口。
    </p>
    <p
      v-if="authMutation"
      role="status"
      class="session-status"
    >
      {{ authMutation === 'logout' ? '正在完成退出，请等待…' : '正在处理登录，请等待…' }}
    </p>
    <p
      v-if="loading"
      role="status"
      class="session-status"
    >
      正在恢复会话…
    </p>
    <section
      v-if="sessionError"
      class="session-status"
      role="alert"
    >
      <p>{{ sessionError }}</p><button
        :disabled="authMutation !== null"
        @click="restore"
      >
        重试恢复
      </button>
      <button
        :disabled="exiting || authMutation !== null"
        @click="exit"
      >
        重试退出
      </button>
    </section>
    <LoginPanel v-if="authenticationSupported && authMutation !== 'logout' && !loading && !snapshot.identity && !isControlled && !isIsolatedDevelopment" />
    <template v-if="!authMutation && !loading && (isIsolatedDevelopment || (authenticationSupported && (snapshot.identity || isControlled)))">
      <header class="topbar">
        <span class="brand">TradeOS</span>
        <WorkspaceNavigation level="primary" />
        <span class="spacer" />
        <NotificationBadge :key="identityGeneration" />
        <button
          v-if="snapshot.identity?.mode === 'authenticated'"
          class="logout-button"
          :disabled="exiting || authMutation !== null"
          @click="exit"
        >
          {{ exiting ? '正在退出…' : '退出' }}
        </button>
      </header>
      <WorkspaceNavigation level="secondary" />
      <ControlledModeBar />
      <RouterView :key="isIsolatedDevelopment && !isControlled ? 0 : identityGeneration" />
    </template>
  </main>
</template>

<style>
:root {
  --canvas: #f5f7f7;
  --surface: #ffffff;
  --text-primary: #172323;
  --text-secondary: #526363;
  --border: #cbd7d5;
  --fact: #0f766e;
  --fact-soft: #e7f5f2;
  --inference: #9a6700;
  --inference-soft: #fff5d6;
  --action: #155eef;
  --danger: #b42318;
  --danger-soft: #fef0ec;
  --warning: #b54708;
  --warning-soft: #fff4e5;
  --focus: #7c3aed;
  --topbar: #0e4a46;
  --topbar-text: #e7f5f2;
  --gutter: 24px;
  --space1: 4px;
  --space2: 8px;
  --space3: 12px;
  --space4: 16px;
  --space5: 24px;
  --space6: 32px;
  --radius: 8px;
  --radius-sm: 4px;
}
* {
  box-sizing: border-box;
  margin: 0;
  padding: 0;
}
html,
body,
#app {
  height: 100%;
  overflow: hidden;
}
.session-status { padding: 24px; }
main[aria-label="TradeOS"] { display:flex; flex-direction:column; height:100%; }
main[aria-label="TradeOS"] > .shell, main[aria-label="TradeOS"] > .board-shell { flex:1; min-height:0; width:100%; height:auto; }
main[aria-label="TradeOS"] > .topbar { flex-shrink:0; }
body {
  font-family: "PingFang SC", "Microsoft YaHei", "Noto Sans CJK SC", system-ui, sans-serif;
  background: var(--canvas);
  color: var(--text-primary);
  font-size: 14px;
  line-height: 1.5;
}
button,
a,
input,
textarea,
select {
  font: inherit;
}
button {
  cursor: pointer;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border);
  background: var(--surface);
  padding: 6px 12px;
  color: var(--text-primary);
}
button:focus-visible,
a:focus-visible,
input:focus-visible,
textarea:focus-visible,
li:focus-visible {
  outline: 3px solid var(--focus);
  outline-offset: 2px;
}
button:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.topbar {
  background: var(--topbar);
  color: var(--topbar-text);
  display: flex;
  align-items: center;
  gap: var(--space5);
  padding: 0 var(--gutter);
  height: 56px;
  width: 100%;
}
.brand {
  font-weight: 700;
  letter-spacing: 0.02em;
  white-space: nowrap;
  flex-shrink: 0;
}
.logout-button {
  flex-shrink: 0;
  color: var(--topbar-text);
  background: transparent;
  border: 1px solid rgba(255, 255, 255, 0.3);
}
.spacer {
  flex: 1;
}
.shell {
  max-width: 1360px;
  margin: 0 auto;
  padding: var(--space4) var(--gutter) var(--space5);
  display: flex;
  flex-direction: column;
  gap: var(--space4);
  height: calc(100vh - 56px);
}
.page-head {
  display: flex;
  align-items: baseline;
  gap: var(--space4);
  flex-wrap: wrap;
}
.page-head h1 {
  font-size: 20px;
}
.meta {
  color: var(--text-secondary);
  font-size: 12px;
}
.safe-banner {
  border: 1px solid var(--warning);
  background: var(--warning-soft);
  color: var(--warning);
  border-radius: var(--radius);
  padding: var(--space3) var(--space4);
  display: flex;
  gap: var(--space3);
  align-items: flex-start;
  font-size: 13px;
}
.safe-banner.danger {
  color: var(--danger);
  border-color: var(--danger);
  background: var(--danger-soft);
}
.btn-primary {
  background: var(--action);
  color: #fff;
  border-color: var(--action);
}
.status {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  padding: 2px 8px;
  border-radius: 999px;
  border: 1px solid;
}
.phase-center-shell {
  overflow-y: auto;
}
.phase-eyebrow,
.card-kicker {
  color: var(--fact);
  font-size: 10px;
  font-weight: 800;
  letter-spacing: 0.1em;
  text-transform: uppercase;
}
.manual-status {
  border-color: var(--inference);
  color: var(--inference);
  background: var(--inference-soft);
}
.disabled-status {
  border-color: var(--text-secondary);
  color: var(--text-secondary);
  background: var(--canvas);
}
.center-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--space4);
}
.center-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: var(--space5);
  display: grid;
  align-content: start;
  gap: var(--space3);
  min-height: 150px;
}
.center-card p,
.center-card li {
  color: var(--text-secondary);
}
.center-card ol {
  padding-left: 20px;
  display: grid;
  gap: var(--space2);
}
.muted-card {
  background: var(--canvas);
}
.file-picker {
  display: inline-flex;
  width: max-content;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 6px 12px;
  color: var(--text-secondary);
}
.file-picker input {
  display: none;
}
@media (max-width: 1240px) {
  :root {
    --gutter: 20px;
  }
}
@media (max-width: 700px) {
  .topbar {
    height: auto;
    min-height: 104px;
    flex-wrap: wrap;
    gap: var(--space2);
    padding: var(--space2) var(--gutter);
  }
  .topbar nav {
    order: 3;
    flex-basis: 100%;
    gap: var(--space2);
  }
  .shell {
    height: calc(100vh - 104px);
  }
  .center-grid {
    grid-template-columns: 1fr;
  }
}
@media (prefers-reduced-motion: reduce) {
  * {
    transition: none !important;
    animation: none !important;
  }
}
</style>
