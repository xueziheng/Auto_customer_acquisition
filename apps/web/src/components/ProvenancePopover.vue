<script setup lang="ts">
/* global crypto, document, HTMLButtonElement, HTMLElement, KeyboardEvent, MouseEvent, Node, URL, window */
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from "vue";

import type { components } from "../api/api";

type ProvenanceSummary = components["schemas"]["domains__opportunities__schemas__ProvenanceSummary"];

const props = defineProps<{
  fieldLabel: string;
  provenance: ProvenanceSummary;
}>();

const dialogId = `provenance-${crypto.randomUUID()}`;
const isOpen = ref(false);
const trigger = ref<HTMLButtonElement>();
const dialog = ref<HTMLElement>();
const closeButton = ref<HTMLButtonElement>();

const safeSourceUrl = computed(() => {
  if (!props.provenance.source_url) return null;
  try {
    const parsed = new URL(props.provenance.source_url);
    return parsed.protocol === "https:" ? parsed.toString() : null;
  } catch {
    return null;
  }
});

async function open(): Promise<void> {
  isOpen.value = true;
  await nextTick();
  closeButton.value?.focus();
}

function close(returnFocus = true): void {
  if (!isOpen.value) return;
  isOpen.value = false;
  if (returnFocus) {
    nextTick(() => trigger.value?.focus());
  }
}

function focusableElements(): HTMLElement[] {
  if (!dialog.value) return [];
  return [...dialog.value.querySelectorAll<HTMLElement>("button:not([disabled]), a[href]")].filter(
    (element) => !element.hidden,
  );
}

function onDialogKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape") {
    event.preventDefault();
    close();
    return;
  }
  if (event.key !== "Tab") return;
  const focusable = focusableElements();
  if (focusable.length === 0) {
    event.preventDefault();
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last?.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first?.focus();
  }
}

function onDocumentPointerDown(event: MouseEvent): void {
  if (!isOpen.value) return;
  const target = event.target;
  if (!(target instanceof Node)) return;
  if (!dialog.value?.contains(target) && !trigger.value?.contains(target)) close();
}

function openSource(): void {
  if (safeSourceUrl.value) window.open(safeSourceUrl.value, "_blank", "noopener,noreferrer");
}

onMounted(() => document.addEventListener("mousedown", onDocumentPointerDown));
onBeforeUnmount(() => document.removeEventListener("mousedown", onDocumentPointerDown));
</script>

<template>
  <button
    ref="trigger"
    class="source-trigger"
    type="button"
    aria-haspopup="dialog"
    :aria-expanded="isOpen"
    :aria-controls="dialogId"
    @click="open"
  >
    <span aria-hidden="true">⌕</span>
    查看来源
  </button>
  <aside
    :id="dialogId"
    ref="dialog"
    class="provenance-popover"
    role="dialog"
    aria-modal="false"
    :aria-labelledby="`${dialogId}-title`"
    :hidden="!isOpen"
    @keydown="onDialogKeydown"
  >
    <header class="provenance-header">
      <div>
        <h2
          :id="`${dialogId}-title`"
          tabindex="-1"
        >
          字段来源
        </h2>
        <p>{{ fieldLabel }} · 已验证事实</p>
      </div>
      <button
        ref="closeButton"
        class="close-button"
        type="button"
        aria-label="关闭来源"
        @click="close()"
      >
        <span aria-hidden="true">×</span>
      </button>
    </header>
    <div class="provenance-body">
      <dl>
        <div><dt>来源类型</dt><dd>{{ provenance.source_type }}</dd></div>
        <div><dt>来源标识</dt><dd>{{ provenance.source_id }}</dd></div>
        <div><dt>提取者</dt><dd>{{ provenance.extracted_by }}</dd></div>
        <div><dt>提取时间</dt><dd>{{ provenance.extracted_at }}</dd></div>
        <div><dt>确认人</dt><dd>{{ provenance.confirmed_by ?? "尚未确认" }}</dd></div>
        <div><dt>确认时间</dt><dd>{{ provenance.confirmed_at ?? "尚未确认" }}</dd></div>
      </dl>
      <button
        v-if="safeSourceUrl"
        class="open-source"
        type="button"
        @click="openSource"
      >
        打开来源
      </button>
    </div>
  </aside>
</template>

<style scoped>
.source-trigger {
  align-self: center;
  min-width: 92px;
  min-height: 44px;
  padding: 0 9px;
  color: #0c5e57;
  border: 1px solid #67a9a0;
  border-radius: 8px;
  background: #fff;
  font: inherit;
  font-size: 12px;
  font-weight: 680;
  cursor: pointer;
}

.provenance-popover {
  position: fixed;
  z-index: 20;
  top: 98px;
  right: 29px;
  width: 290px;
  max-height: calc(100vh - 112px);
  overflow: auto;
  border: 1px solid #cbd7d5;
  border-radius: 11px;
  background: #fff;
  box-shadow: 0 12px 32px rgba(20, 35, 35, 0.18);
}

.provenance-popover[hidden] {
  display: none;
}

.provenance-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 9px;
  padding: 15px;
  color: #f5fffd;
  background: #245650;
}

.provenance-header h2,
.provenance-header p {
  margin: 0;
}

.provenance-header h2 {
  font-size: 16px;
  line-height: 1.35;
}

.provenance-header p {
  margin-top: 3px;
  color: #c9e2de;
  font-size: 11px;
}

.close-button {
  min-width: 44px;
  min-height: 44px;
  padding: 0;
  color: #fff;
  border: 1px solid #82aca7;
  border-radius: 8px;
  background: transparent;
  font: inherit;
  font-size: 22px;
  cursor: pointer;
}

.provenance-body {
  padding: 14px;
}

.demo-badge {
  display: inline-flex;
  min-height: 23px;
  align-items: center;
  padding: 2px 7px;
  border: 1px solid #cbd7d5;
  border-radius: 6px;
  color: #445554;
  background: #e9efee;
  font-size: 11px;
  font-weight: 750;
}

dl {
  margin: 8px 0 0;
}

dl > div {
  padding: 7px 0;
  border-bottom: 1px solid #e1e8e6;
}

dt {
  color: #526363;
  font-size: 11px;
}

dd {
  margin: 2px 0 0;
  overflow-wrap: anywhere;
  font-size: 12px;
  font-weight: 680;
}

.open-source {
  width: 100%;
  min-height: 44px;
  margin-top: 12px;
  color: #0c5e57;
  border: 1px solid #67a9a0;
  border-radius: 8px;
  background: #f2faf8;
  font: inherit;
  font-weight: 680;
  cursor: pointer;
}

button:focus-visible {
  outline: 3px solid #7c3aed;
  outline-offset: 2px;
}

@media (max-width: 1240px) {
  .provenance-popover {
    top: 89px;
    right: 20px;
    width: 250px;
    max-height: calc(100vh - 99px);
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
