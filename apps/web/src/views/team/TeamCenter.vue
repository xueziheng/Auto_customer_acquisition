<script setup lang="ts">
import { computed, inject, onMounted, ref } from "vue";

import type { components } from "../../api/api";
import { apiClient, createApiClient } from "../../api/client";

type ApiClient = ReturnType<typeof createApiClient>;
type Employee = components["schemas"]["EmployeeView"];
type Territory = components["schemas"]["TerritoryAssignmentView"];

const client = inject<ApiClient>("tradeos-api-client", apiClient);
const employees = ref<Employee[]>([]);
const territory = ref<Territory[]>([]);
const loading = ref(true);
const error = ref<string | null>(null);

const employeeNames = computed(
  () => new Map(employees.value.map((item) => [item.employee_id, item.name])),
);
const coveredCountries = computed(
  () => new Set(territory.value.flatMap((item) => item.countries ?? [])).size,
);
const assignedEmployees = computed(
  () => new Set(territory.value.map((item) => item.employee_id)).size,
);

const roleLabels: Readonly<Record<string, string>> = Object.freeze({
  boss: "老板",
  finance: "财务",
  manager: "经理",
  product: "产品",
  sales: "销售",
  sourcing: "寻源",
  viewer: "只读",
});

function employeeName(employeeId: string | null): string {
  if (!employeeId) return "—";
  return employeeNames.value.get(employeeId) ?? employeeId;
}

function formatDate(value: string | null): string {
  if (!value) return "持续生效";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? value
    : parsed.toLocaleDateString("zh-CN");
}

function dimension(values: string[] | undefined): string {
  return values?.length ? values.join(" · ") : "不限";
}

function safeError(status: number): string {
  if (status === 403) return "只有老板可以查看完整团队与分配矩阵";
  if (status === 503) return "团队服务暂不可用";
  return "团队与分配矩阵读取失败，请稍后重试";
}

async function loadTeam(): Promise<void> {
  loading.value = true;
  error.value = null;
  try {
    const [employeeResult, territoryResult] = await Promise.all([
      client.GET("/team/employees"),
      client.GET("/team/territory"),
    ]);
    if (employeeResult.response.status !== 200 || !employeeResult.data) {
      error.value = safeError(employeeResult.response.status);
      return;
    }
    if (territoryResult.response.status !== 200 || !territoryResult.data) {
      error.value = safeError(territoryResult.response.status);
      return;
    }
    employees.value = employeeResult.data;
    territory.value = territoryResult.data;
  } catch {
    error.value = "无法连接团队服务";
  } finally {
    loading.value = false;
  }
}

onMounted(() => void loadTeam());
</script>

<template>
  <div class="shell team-shell">
    <div class="page-head team-head">
      <div>
        <p class="phase-eyebrow">
          团队与业务分配
        </p>
        <h1>团队与归属</h1>
      </div>
      <button
        type="button"
        :disabled="loading"
        @click="loadTeam"
      >
        {{ loading ? "加载中…" : "刷新矩阵" }}
      </button>
    </div>

    <div class="safe-banner">
      <span aria-hidden="true">i</span>
      <div>客户归属按八级规则逐级命中，首个命中即停止；本页只展示可验证的员工资料与生效规则。</div>
    </div>
    <div
      v-if="error"
      class="safe-banner danger"
      role="alert"
    >
      {{ error }}
    </div>

    <section
      class="team-metrics"
      aria-label="团队分配概览"
    >
      <article><span>活跃员工</span><strong>{{ employees.length }}</strong><p>仅来自同租户有效员工记录</p></article>
      <article><span>生效规则</span><strong>{{ territory.length }}</strong><p>停用员工遗留规则已过滤</p></article>
      <article><span>覆盖国家</span><strong>{{ coveredCountries }}</strong><p>按规则国家集合去重</p></article>
      <article><span>已配置员工</span><strong>{{ assignedEmployees }}</strong><p>至少命中一条分配规则</p></article>
    </section>

    <section class="team-layout">
      <article class="team-panel employee-panel">
        <header>
          <div>
            <p class="card-kicker">
              在岗团队
            </p><h2>活跃员工</h2>
          </div>
          <span>{{ employees.length }} 人</span>
        </header>
        <div
          v-if="loading"
          class="empty"
        >
          正在读取团队…
        </div>
        <div
          v-else-if="!employees.length"
          class="empty"
        >
          当前没有可显示的活跃员工
        </div>
        <div
          v-else
          class="employee-list"
        >
          <article
            v-for="item in employees"
            :key="item.employee_id"
            class="employee-card"
          >
            <header>
              <div class="employee-avatar">
                {{ item.name.slice(0, 1) }}
              </div>
              <div><h3>{{ item.name }}</h3><span>{{ roleLabels[item.role] ?? item.role }}</span></div>
            </header>
            <dl>
              <div><dt>员工编号</dt><dd>{{ item.employee_id }}</dd></div>
              <div><dt>直属经理</dt><dd>{{ employeeName(item.manager_id ?? null) }}</dd></div>
              <div><dt>语言</dt><dd>{{ dimension(item.languages) }}</dd></div>
              <div><dt>时区</dt><dd>{{ item.timezone ?? "未设置" }}</dd></div>
              <div><dt>活跃客户上限</dt><dd>{{ item.max_active_accounts ?? "未设置" }}</dd></div>
            </dl>
          </article>
        </div>
      </article>

      <article class="team-panel territory-panel">
        <header>
          <div>
            <p class="card-kicker">
              业务分配矩阵
            </p><h2>业务分配矩阵</h2>
          </div>
          <span>按优先级升序</span>
        </header>
        <div
          v-if="loading"
          class="empty"
        >
          正在读取分配规则…
        </div>
        <div
          v-else-if="!territory.length"
          class="empty"
        >
          当前没有生效的分配规则
        </div>
        <div
          v-else
          class="territory-list"
        >
          <article
            v-for="(item, index) in territory"
            :key="`${item.employee_id}-${item.priority}-${index}`"
            class="territory-card"
          >
            <header>
              <div><span class="priority-pill">优先级 {{ item.priority }}</span><h3>{{ employeeName(item.employee_id) }}</h3></div>
              <small>{{ formatDate(item.effective_from) }} → {{ formatDate(item.effective_until ?? null) }}</small>
            </header>
            <dl>
              <div><dt>国家</dt><dd>{{ dimension(item.countries) }}</dd></div>
              <div><dt>需求类别</dt><dd>{{ dimension(item.need_categories) }}</dd></div>
              <div><dt>产品类别</dt><dd>{{ dimension(item.product_categories) }}</dd></div>
              <div><dt>买家类型</dt><dd>{{ dimension(item.buyer_types) }}</dd></div>
              <div><dt>语言</dt><dd>{{ dimension(item.languages) }}</dd></div>
              <div><dt>经理</dt><dd>{{ employeeName(item.manager_id ?? null) }}</dd></div>
              <div><dt>备用负责人</dt><dd>{{ employeeName(item.backup_employee_id ?? null) }}</dd></div>
            </dl>
          </article>
        </div>
      </article>
    </section>
  </div>
</template>

<style scoped>
.team-shell { overflow: auto; gap: var(--space4); }
.team-head { justify-content: space-between; padding-top: var(--space3); }
.team-metrics { display: grid; grid-template-columns: repeat(4, 1fr); gap: var(--space3); }
.team-metrics article, .team-panel { border: 1px solid var(--border); border-radius: 12px; background: var(--surface); }
.team-metrics article { padding: var(--space4); }
.team-metrics span { color: var(--text-secondary); font-size: 11px; font-weight: 700; }
.team-metrics strong { display: block; margin-top: var(--space1); font-size: 28px; }
.team-metrics p { color: var(--text-secondary); font-size: 10px; }
.team-layout { display: grid; grid-template-columns: minmax(320px, .75fr) minmax(520px, 1.25fr); gap: var(--space4); }
.team-panel { padding: var(--space4); }
.team-panel > header { display: flex; justify-content: space-between; align-items: flex-end; gap: var(--space3); padding-bottom: var(--space3); }
.team-panel > header span { color: var(--text-secondary); font-size: 11px; }
.team-panel h2 { font-size: 18px; }
.employee-list, .territory-list { display: grid; gap: var(--space3); max-height: 590px; overflow: auto; }
.employee-card, .territory-card { border: 1px solid var(--border); border-radius: var(--radius); padding: var(--space3); }
.employee-card > header { display: flex; align-items: center; gap: var(--space2); }
.employee-avatar { display: grid; place-items: center; width: 36px; height: 36px; border-radius: 50%; background: var(--fact-soft); color: var(--fact); font-weight: 800; }
.employee-card h3, .territory-card h3 { font-size: 14px; }
.employee-card header span { color: var(--text-secondary); font-size: 10px; }
dl { display: grid; gap: var(--space2); margin-top: var(--space3); }
dl div { display: grid; grid-template-columns: 92px 1fr; gap: var(--space2); }
dt { color: var(--text-secondary); font-size: 10px; }
dd { margin: 0; overflow-wrap: anywhere; font-size: 11px; }
.territory-card { border-left: 4px solid var(--action); }
.territory-card > header { display: flex; justify-content: space-between; gap: var(--space3); }
.territory-card > header > div { display: flex; align-items: center; gap: var(--space2); }
.territory-card small { color: var(--text-secondary); font-size: 10px; }
.territory-card dl { grid-template-columns: repeat(2, 1fr); }
.priority-pill { border-radius: 999px; background: #eaf0ff; padding: 2px 7px; color: var(--action); font-size: 9px; font-weight: 800; }
.empty { display: grid; place-items: center; min-height: 220px; color: var(--text-secondary); }
@media (max-width: 1000px) { .team-metrics { grid-template-columns: repeat(2, 1fr); } .team-layout { grid-template-columns: 1fr; } }
@media (max-width: 600px) { .team-metrics, .territory-card dl { grid-template-columns: 1fr; } .territory-card > header { align-items: flex-start; flex-direction: column; } }
</style>
