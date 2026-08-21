<script setup lang="ts">
import { RouterLink, RouterView } from "vue-router";
import NotificationBadge from "./components/NotificationBadge.vue";

const appName: string = "TradeOS";
</script>

<template>
  <main :aria-label="appName">
    <header class="topbar">
      <span class="brand">TradeOS 内部运营台</span>
      <nav aria-label="主导航">
        <RouterLink to="/prospects/accounts">
          客户发现
        </RouterLink>
        <RouterLink to="/crm/opportunities">
          CRM
        </RouterLink>
        <RouterLink to="/crm/outreach">
          触达工作台
        </RouterLink>
        <RouterLink to="/crm/sending-identities">
          发件身份
        </RouterLink>
        <RouterLink to="/notifications">
          通知
        </RouterLink>
      </nav>
      <span class="spacer" />
      <NotificationBadge />
    </header>
    <RouterView />
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
}
nav {
  display: flex;
  gap: var(--space4);
}
nav a {
  color: var(--topbar-text);
  text-decoration: none;
  padding: 6px 10px;
  border-radius: var(--radius-sm);
  opacity: 0.85;
}
nav a.router-link-active {
  opacity: 1;
  background: rgba(255, 255, 255, 0.14);
  font-weight: 600;
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
@media (max-width: 1240px) {
  :root {
    --gutter: 20px;
  }
}
@media (prefers-reduced-motion: reduce) {
  * {
    transition: none !important;
    animation: none !important;
  }
}
</style>
