<template>
  <div class="shell phase-center-shell">
    <div class="page-head">
      <div>
        <p class="phase-eyebrow">TRADE RUN AUDIT</p>
        <h1>Run 全景</h1>
      </div>
      <span class="status manual-status">审计入口</span>
    </div>
    <div class="safe-banner">
      <span aria-hidden="true">i</span>
      <div>Run 只展示目标、技能、工具元数据、证据引用、变更集、审批和脱敏错误；不展示凭证、页面正文或客户 PII。</div>
    </div>
    <section class="center-grid">
      <article class="center-card"><span class="card-kicker">目标与状态</span><strong>按 Run 追踪一次完整工作，而不是只看 Agent 对话。</strong></article>
      <article class="center-card"><span class="card-kicker">证据与工具</span><strong>工具调用含允许 / 拒绝结果和 Artifact 引用。</strong></article>
      <article class="center-card"><span class="card-kicker">变更与审批</span><strong>ChangeSet 应用前后及人工决定在同一条审计链。</strong></article>
      <article class="center-card muted-card"><span class="card-kicker">接口状态</span><strong>Trade Run 读模型和查询接口尚未装配。</strong><p>本页不会用模拟记录冒充生产执行。</p></article>
    </section>
  </div>
</template>
