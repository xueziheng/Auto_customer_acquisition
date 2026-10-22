<script setup lang="ts">
import { onMounted, onUnmounted, ref } from "vue";
import { platformClient, type InventoryQuota } from "../api/platform-auth";
const props = defineProps<{ company: string }>();
const quota = ref<InventoryQuota | null>(null), limit = ref(50), busy = ref(false), error = ref("");
let generation = 0, disposed = false;
const unsubscribe = platformClient.subscribe(() => { if (!platformClient.snapshot().identity) { generation++; quota.value = null; } });
onUnmounted(() => { disposed = true; generation++; unsubscribe(); });
async function load() { const current = ++generation; busy.value = true; error.value = ""; try { const value = await platformClient.inventoryQuota(props.company); if (!disposed && current === generation) { quota.value = value; limit.value = value.daily_limit; } } catch { if (!disposed && current === generation) error.value = "候选服务未配置或暂不可用"; } finally { if (!disposed && current === generation) busy.value = false; } }
async function save() { if (busy.value || !Number.isInteger(limit.value) || limit.value < 0 || limit.value > 10000) return; const current = ++generation; busy.value = true; error.value = ""; try { const value = await platformClient.setInventoryQuota(props.company, limit.value); if (!disposed && current === generation) { quota.value = value; limit.value = value.daily_limit; } } catch { if (!disposed && current === generation) error.value = "额度保存未确认，请刷新核对"; } finally { if (!disposed && current === generation) busy.value = false; } }
onMounted(() => void load());
</script>
<template>
  <section
    class="company-quota"
    aria-label="公司候选交付额度"
  >
    <h3>候选交付额度</h3><p v-if="quota">
      今日已领取 {{ quota.used_today }}，剩余 {{ quota.remaining_today }}。同公司全部员工共享。
    </p><form @submit.prevent="save">
      <label>每日新候选数<input
        v-model.number="limit"
        type="number"
        min="0"
        max="10000"
        :disabled="busy || !quota"
        required
      ></label><button
        type="submit"
        :disabled="busy || !quota"
      >
        保存额度
      </button><button
        type="button"
        :disabled="busy"
        @click="load"
      >
        刷新
      </button>
    </form><p
      v-if="error"
      role="alert"
    >
      {{ error }}
    </p>
  </section>
</template>
<style scoped>.company-quota{border-top:1px solid var(--border);margin-top:16px;padding-top:12px}.company-quota h3{font-size:14px;margin:0 0 8px}.company-quota p{font-size:12px;color:var(--muted)}.company-quota form{display:flex;gap:8px;flex-wrap:wrap;align-items:end}.company-quota label{display:grid;gap:5px;font-size:12px}.company-quota input{width:120px;padding:7px;border:1px solid var(--border);border-radius:5px}.company-quota button{padding:7px 10px}</style>
