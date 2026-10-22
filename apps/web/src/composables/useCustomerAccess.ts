import { onMounted, ref } from "vue";
import type { components } from "../api/api";
import type { createApiClient } from "../api/client";
import { useQuoteRequestScope } from "../views/costing-quotes/quote-request-scope";

export function useCustomerAccess(client: ReturnType<typeof createApiClient>) {
  const access = ref<components["schemas"]["CustomerAccessView"] | null>(null);
  const loading = ref(false),
    error = ref("");
  let mounted = false;
  const gate = useQuoteRequestScope(
    client,
    () => [],
    () => {
      access.value = null;
      error.value = "";
      loading.value = false;
      if (mounted) globalThis.queueMicrotask(() => void load());
    },
  );
  async function load() {
    if (!gate.hasIdentity.value) return;
    const operation = gate.begin("customer-access");
    if (!operation?.valid()) return;
    access.value = null;
    loading.value = true;
    error.value = "";
    try {
      const result = await client.GET("/prospects/customers/access", {
        signal: operation.signal,
        cache: "no-store",
      });
      if (!operation.valid()) return;
      if (result.response.ok && result.data) access.value = result.data;
      else error.value = "无法确认当前客户权限，请刷新重试。";
    } catch {
      if (operation.valid()) error.value = "无法确认当前客户权限，请刷新重试。";
    } finally {
      if (operation.valid()) loading.value = false;
    }
  }
  onMounted(() => {
    mounted = true;
    void load();
  });
  // reset 在卸载时也被调用；卸载后的微任务由 scope 的 disposed 门禁拒绝。
  return { access, loading, error, load };
}
