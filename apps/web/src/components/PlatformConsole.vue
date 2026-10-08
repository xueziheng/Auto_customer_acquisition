<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { platformClient, type PlatformOverview } from "../api/platform-auth";

const state = ref(platformClient.snapshot());
const supported = platformClient.supportsAuthentication();
const restoring = ref(true);
const refreshing = ref(false);
const username = ref("");
const password = ref("");
const error = ref("");
const logoutFailed = ref(false);
const overview = ref<PlatformOverview | null>(null);
let requestGeneration = 0;
const busy = computed(() => restoring.value || state.value.mutation !== null);
const unsubscribe = platformClient.subscribe(() => {
  const next = platformClient.snapshot();
  if (next.revision !== state.value.revision) {
    overview.value = null;
    requestGeneration += 1;
    refreshing.value = false;
  }
  state.value = next;
});
const stopListening = platformClient.listen();
onUnmounted(() => { unsubscribe(); stopListening(); requestGeneration += 1; });

async function refresh(): Promise<void> {
  const expected = ++requestGeneration;
  overview.value = null;
  refreshing.value = true;
  error.value = "";
  try {
    const result = await platformClient.overview();
    if (expected === requestGeneration) overview.value = result;
  } catch {
    if (expected === requestGeneration) error.value = "企业概览暂不可用，请点击刷新重试。";
  } finally {
    if (expected === requestGeneration) refreshing.value = false;
  }
}
async function restore(): Promise<void> {
  restoring.value = true;
  error.value = "";
  try {
    await platformClient.restore();
    if (state.value.identity) await refresh();
  } catch { error.value = "无法恢复平台会话，请检查连接后重试。"; }
  finally { restoring.value = false; }
}
async function submit(): Promise<void> {
  if (busy.value) return;
  error.value = "";
  const pending = platformClient.login(username.value.trim(), password.value);
  password.value = "";
  try {
    await pending;
    logoutFailed.value = false;
    await refresh();
  } catch { error.value = "平台登录失败，请检查账号和密码；尝试过多时请稍后重试。"; }
}
async function exit(): Promise<void> {
  if (busy.value) return;
  error.value = "";
  try { await platformClient.logout(); logoutFailed.value = false; }
  catch {
    logoutFailed.value = true;
    error.value = "退出未完成：尚不能确认服务器已撤销会话，请重试退出。";
  }
}
function count(value: number | null | undefined): string {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? String(value) : "暂不可用";
}
function timestamp(value: string): string {
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date.toLocaleString("zh-CN") : "时间未知";
}
onMounted(() => {
  if (supported) void restore();
  else restoring.value = false;
});
</script>

<template>
  <main
    class="platform-shell"
    aria-label="TradeOS 平台管理"
  >
    <header class="platform-header">
      <div>
        <strong>TradeOS</strong>
        <span>平台管理</span>
      </div>
      <nav aria-label="平台入口">
        <a href="/">企业工作台</a>
        <button
          v-if="state.identity"
          type="button"
          :disabled="busy"
          @click="exit"
        >
          退出平台
        </button>
      </nav>
    </header>
    <div class="platform-content">
      <p
        v-if="!supported"
        class="safe-banner danger"
        role="alert"
      >
        当前浏览器不支持安全会话锁，请使用新版 Chrome 或 Edge 登录。
      </p>
      <p
        v-if="restoring || state.mutation"
        class="platform-message"
        role="status"
      >
        {{ restoring ? '正在恢复平台会话…' : state.mutation === 'logout' ? '正在退出平台…' : '正在登录平台…' }}
      </p>
      <section
        v-if="error"
        class="safe-banner danger platform-error"
        role="alert"
      >
        <p>{{ error }}</p>
        <button
          v-if="logoutFailed"
          type="button"
          :disabled="busy"
          @click="exit"
        >
          重试退出
        </button>
        <button
          v-else-if="!state.identity"
          type="button"
          :disabled="busy"
          @click="restore"
        >
          重试恢复
        </button>
      </section>

      <form
        v-if="supported && !restoring && !state.identity"
        class="platform-login"
        @submit.prevent="submit"
      >
        <p class="phase-eyebrow">
          TradeOS
        </p>
        <h1>平台管理员登录</h1>
        <p class="meta">
          查看已注册企业、账号层级和使用情况。
        </p>
        <label for="platform-username">平台账号</label>
        <input
          id="platform-username"
          v-model="username"
          autocomplete="username"
          required
          maxlength="254"
          :disabled="busy"
        >
        <label for="platform-password">密码</label>
        <input
          id="platform-password"
          v-model="password"
          type="password"
          autocomplete="current-password"
          required
          :disabled="busy"
        >
        <button
          class="btn-primary"
          type="submit"
          :disabled="busy"
        >
          {{ state.mutation === 'login' ? '正在登录…' : '登录平台' }}
        </button>
        <a href="/">企业账号登录</a>
      </form>

      <section
        v-if="state.identity && !state.mutation"
        class="platform-overview"
        aria-label="企业使用概览"
      >
        <div class="page-head platform-heading">
          <div>
            <p class="phase-eyebrow">
              平台管理员 · {{ state.identity.display_name }}（{{ state.identity.username }}）
            </p>
            <h1>企业与账号</h1>
            <p class="meta">
              企业管理员管理本企业，员工按负责人范围跟进客户。
            </p>
          </div>
          <button
            type="button"
            :disabled="refreshing"
            @click="refresh"
          >
            {{ refreshing ? '正在读取…' : '刷新企业概览' }}
          </button>
        </div>
        <p
          v-if="refreshing"
          role="status"
        >
          正在读取已注册企业…
        </p>
        <template v-if="overview">
          <p class="meta">
            {{ overview.enterprises.length }} 家已注册企业 · 更新于 {{ timestamp(overview.generated_at) }}
          </p>
          <p v-if="overview.enterprises.length === 0">
            当前没有已注册企业。
          </p>
          <div class="enterprise-grid">
            <article
              v-for="enterprise in overview.enterprises"
              :key="enterprise.tenant_id"
              class="enterprise-card"
            >
              <header>
                <div>
                  <h2>{{ enterprise.name }}</h2>
                  <p class="meta">
                    {{ enterprise.tenant_id }}
                  </p>
                </div>
                <span
                  class="status"
                  :class="enterprise.enabled ? 'enabled-status' : 'disabled-status'"
                >{{ enterprise.enabled ? '已启用' : '已停用' }}</span>
              </header>
              <p
                v-if="!enterprise.available"
                class="safe-banner"
                role="status"
              >
                暂无法读取此企业的使用情况，请稍后刷新。
              </p>
              <template v-else>
                <dl class="enterprise-counts">
                  <div><dt>活跃成员</dt><dd>{{ count(enterprise.active_members) }}</dd></div>
                  <div><dt>企业管理员</dt><dd>{{ count(enterprise.admins) }}</dd></div>
                  <div><dt>员工</dt><dd>{{ count(enterprise.employees) }}</dd></div>
                  <div><dt>客户</dt><dd>{{ count(enterprise.customers) }}</dd></div>
                  <div><dt>已验证需求</dt><dd>{{ count(enterprise.validated_needs) }}</dd></div>
                  <div><dt>贸易机会</dt><dd>{{ count(enterprise.opportunities) }}</dd></div>
                  <div><dt>运行中流程</dt><dd>{{ count(enterprise.active_tasks) }}</dd></div>
                </dl>
                <section class="enterprise-members">
                  <h3>账号层级</h3>
                  <p
                    v-if="enterprise.members.length === 0"
                    class="meta"
                  >
                    当前没有可显示的成员。
                  </p>
                  <ul v-else>
                    <li
                      v-for="(member, index) in enterprise.members"
                      :key="`${member.name}-${index}`"
                    >
                      <strong>{{ member.name }}</strong>
                      <span>{{ member.role === 'admin' ? '企业管理员' : '员工' }}</span>
                    </li>
                  </ul>
                </section>
              </template>
            </article>
          </div>
        </template>
      </section>
    </div>
  </main>
</template>

<style scoped>
.platform-shell { height: 100%; overflow-y: auto; overflow-wrap: anywhere; }
.platform-header { background: var(--topbar); color: var(--topbar-text); padding: 16px var(--gutter); display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
.platform-header > div, .platform-header nav { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
.platform-header a { color: var(--topbar-text); }
.platform-header button { background: transparent; color: var(--topbar-text); }
.platform-content { width: min(100%, 1200px); margin: 0 auto; padding: 24px var(--gutter); min-width: 0; }
.platform-message { margin-bottom: 16px; }
.platform-error { margin-bottom: 20px; flex-wrap: wrap; }
.platform-login { display: grid; gap: 12px; width: min(100%, 420px); margin: 5vh auto; padding: 28px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface); }
.platform-login input { width: 100%; min-width: 0; padding: 10px; border: 1px solid var(--border); border-radius: var(--radius-sm); }
.platform-login h1 { font-size: 24px; }
.platform-overview { display: grid; gap: 20px; min-width: 0; }
.platform-heading { justify-content: space-between; }
.platform-heading > div { min-width: 0; }
.enterprise-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 20px; }
.enterprise-card { min-width: 0; display: grid; align-content: start; gap: 20px; border: 1px solid var(--border); border-radius: var(--radius); padding: 24px; background: var(--surface); }
.enterprise-card > header { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.enterprise-card h2 { font-size: 20px; }
.enabled-status { color: var(--fact); background: var(--fact-soft); }
.enterprise-counts { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; }
.enterprise-counts dt { font-size: 12px; color: var(--text-secondary); }
.enterprise-counts dd { font-size: 24px; margin: 4px 0 0; }
.enterprise-members { border-top: 1px solid var(--border); padding-top: 16px; display: grid; gap: 12px; }
.enterprise-members h3 { font-size: 14px; }
.enterprise-members ul { list-style: none; display: grid; gap: 8px; }
.enterprise-members li { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; padding: 8px 12px; border-radius: var(--radius-sm); background: var(--canvas); }
.enterprise-members li span { color: var(--text-secondary); white-space: nowrap; }
@media (max-width: 800px) { .enterprise-grid { grid-template-columns: minmax(0, 1fr); } }
@media (max-width: 500px) { .platform-content { padding: 20px 16px; } .enterprise-card, .platform-login { padding: 20px; } .enterprise-counts { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
</style>
