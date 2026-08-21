<script setup lang="ts">
/* global Event, HTMLInputElement */
import { ref } from "vue";

const selectedName = ref<string | null>(null);

function selectFile(event: Event): void {
  const input = event.target as HTMLInputElement;
  selectedName.value = input.files?.[0]?.name ?? null;
}
</script>

<template>
  <div class="shell phase-center-shell">
    <div class="page-head"><div><p class="phase-eyebrow">HUMAN WORK INTAKE</p><h1>员工工作上传</h1></div><span class="status manual-status">确认后生效</span></div>
    <div class="safe-banner"><span aria-hidden="true">i</span><div>原件应先进入 Artifact Store；提取结果必须与原始内容并排供员工修正，确认前不得写入业务事实。</div></div>
    <section class="center-grid">
      <article class="center-card upload-card">
        <span class="card-kicker">选择本地原件</span>
        <label class="file-picker">选择文件<input type="file" disabled @change="selectFile" /></label>
        <p>{{ selectedName ?? "上传接口尚未装配，当前不会读取或发送本地文件。" }}</p>
      </article>
      <article class="center-card"><span class="card-kicker">提取对照</span><strong>原始内容 → 模型提取 → 员工修改</strong><p>数量、规格、价格、目的地和时间要求逐字段保留来源。</p></article>
      <article class="center-card boundary-card"><span class="card-kicker">生效门禁</span><strong>员工点击确认后才可形成 Need、Commitment 或跟进任务。</strong></article>
    </section>
  </div>
</template>
