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
  const pending = login(username.value.trim(), password.value);
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
      <h1>登录 TradeOS</h1>
      <p>使用管理员创建的 TradeOS 员工账号登录，登录后再填写发件邮箱地址和授权码。</p>
      <label for="pilot-username">TradeOS 员工账号</label>
      <input
        id="pilot-username"
        v-model="username"
        autocomplete="username"
        placeholder="请输入管理员创建的员工账号"
        required
        maxlength="254"
        :disabled="busy"
      >
      <label for="pilot-password">TradeOS 登录密码</label>
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
        登录后查看当前账号已连接的邮箱与可用功能。
      </p>
      <a href="/platform">平台管理员登录</a>
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
