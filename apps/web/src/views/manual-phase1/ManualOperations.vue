<script setup lang="ts">
import { computed } from "vue";
import { useRoute } from "vue-router";

const route = useRoute();

const operations = {
  products: {
    eyebrow: "PRODUCT & SUPPLY",
    title: "产品与供应能力",
    description: "由员工维护产品、变体与供应能力；客户可见视图与内部视图必须分开。",
    steps: ["登记产品与变体", "核对来源与规格", "分别生成内部 / 销售 / 客户视图"],
    boundary: "客户渠道只能读取 customer view，不能靠前端隐藏内部价格或利润字段。",
  },
  sourcing: {
    eyebrow: "MANUAL SOURCING",
    title: "人工寻源操作台",
    description: "Phase 1 由员工对已验证需求执行匹配梯子、候选核验和证据留存。",
    steps: ["确认需求完整度至少为 3", "记录匹配梯子前五级结论", "提交带快照的供应商候选"],
    boundary: "网页抓取价只能标为 indicative；缺少证据快照的候选不得提交。",
  },
  "costing-quotes": {
    eyebrow: "DETERMINISTIC COST & QUOTE",
    title: "成本与报价",
    description: "Phase 1 由员工录入成本项，确定性代码计算金额，正式报价逐次审批。",
    steps: ["创建成本表版本", "确认所有成本项和报价基准", "锁定后提交正式报价审批"],
    boundary: "金额只用 Decimal 计算；只有 quoted 供应价可进入客户可见报价。",
  },
} as const;

const operation = computed(() => String(route.meta.operation ?? "products"));
const current = computed(() => operations[operation.value as keyof typeof operations] ?? operations.products);
</script>

<template>
  <div class="shell phase-center-shell">
    <div class="page-head">
      <div>
        <p class="phase-eyebrow">{{ current.eyebrow }}</p>
        <h1>{{ current.title }}</h1>
      </div>
      <span class="status manual-status">Phase 1 · 人工执行</span>
    </div>
    <nav class="section-tabs" aria-label="人工运营模块">
      <RouterLink to="/products">产品</RouterLink>
      <RouterLink to="/sourcing">寻源</RouterLink>
      <RouterLink to="/costing-quotes">成本与报价</RouterLink>
    </nav>
    <div class="safe-banner">
      <span aria-hidden="true">i</span>
      <div>{{ current.description }} 当前只建立安全操作边界，不声称已自动执行。</div>
    </div>
    <section class="center-grid">
      <article class="center-card">
        <span class="card-kicker">操作顺序</span>
        <ol>
          <li v-for="step in current.steps" :key="step">{{ step }}</li>
        </ol>
      </article>
      <article class="center-card boundary-card">
        <span class="card-kicker">不可绕过的边界</span>
        <strong>{{ current.boundary }}</strong>
      </article>
      <article class="center-card muted-card">
        <span class="card-kicker">接口状态</span>
        <strong>领域契约已预留，持久化人工录入接口尚未装配。</strong>
        <p>在接口和审批链完成前，本页不提供会误导为“已保存”的按钮。</p>
      </article>
    </section>
  </div>
</template>
