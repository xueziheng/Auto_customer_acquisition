<script setup lang="ts">
import { ref } from "vue";
import { login } from "../api/authentication";
const username = ref("");
const password = ref("");
const busy = ref(false);
const error = ref("");
async function submit(): Promise<void> {
  if (busy.value) return;
  busy.value = true; error.value = "";
  const pending = login(username.value, password.value);
  password.value = "";
  try { await pending; }
  catch { error.value = "登录失败，请检查账号、密码及本机服务；尝试过多时请稍后重试。"; }
  finally { busy.value = false; }
}
</script>
<template>
  <section
    class="login-shell"
    aria-label="员工登录"
  >
    <form
      class="login-panel"
      @submit.prevent="submit"
    >
      <p class="phase-eyebrow">
        TradeOS
      </p>
      <h1>登录内部运营台</h1>
      <p>使用本机管理员创建的员工账号登录。</p>
      <label for="pilot-username">账号</label>
      <input
        id="pilot-username"
        v-model="username"
        autocomplete="username"
        required
        maxlength="64"
        :disabled="busy"
      >
      <label for="pilot-password">密码</label>
      <input
        id="pilot-password"
        v-model="password"
        type="password"
        autocomplete="current-password"
        required
        :disabled="busy"
      >
      <p
        v-if="error"
        role="alert"
        class="login-error"
      >
        {{ error }}
      </p>
      <button
        type="submit"
        class="btn-primary"
        :disabled="busy"
      >
        {{ busy ? '正在登录…' : '登录' }}
      </button>
      <p class="meta">
        本机内测 · 外部模型、搜索与邮件未配置 · 通知仅站内投递
      </p>
    </form>
  </section>
</template>
<style scoped>
.login-shell { display: grid; place-items: center; padding: 24px; min-height: 100%; overflow-y: auto; }
.login-panel { display: grid; gap: 12px; padding: 32px; width: min(100%, 420px); background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); }
.login-panel input { min-width: 0; width: 100%; padding: 10px; border: 1px solid var(--border); border-radius: var(--radius-sm); }
.login-panel h1 { font-size: 24px; }
.login-error { color: var(--danger); }
@media (max-width: 700px) { .login-panel { padding: 24px; } }
</style>
