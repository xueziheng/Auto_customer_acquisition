import { computed, onBeforeUnmount, ref, watch } from "vue";
import { codeLabel } from "../../components/displayLabels";
import type { components } from "../../api/api";
import type { ApiIdentityReader, WebIdentitySnapshot } from "../../api/client";

export function sameIdentitySnapshot(a: WebIdentitySnapshot, b: WebIdentitySnapshot): boolean {
  return a.generation === b.generation
    && a.identity?.tenantId === b.identity?.tenantId
    && a.identity?.employeeId === b.identity?.employeeId
    && a.identity?.mode === b.identity?.mode;
}

// 仅控制本页响应的可见性；中止等待不代表服务端已取消。
export function useQuoteRequestScope(client: ApiIdentityReader, scope: () => readonly unknown[], reset: () => void) {
  let generation = 0;
  let disposed = false;
  const requests = new Map<string, AbortController>();
  const identityRevision = ref(0);
  function invalidate(): void {
    generation += 1;
    for (const request of requests.values()) request.abort();
    requests.clear();
  }
  const hasIdentity = computed(() => {
    void identityRevision.value;
    try { return client.identitySnapshot().identity !== null; } catch { return false; }
  });
  const unsubscribe = client.subscribeIdentity(() => {
    invalidate();
    identityRevision.value += 1;
    reset();
  });
  watch(scope, invalidate, { flush: "sync", deep: true });
  onBeforeUnmount(() => { disposed = true; invalidate(); unsubscribe(); reset(); });
  function begin(channel: string) {
    let identity: WebIdentitySnapshot;
    try { identity = client.identitySnapshot(); } catch { return null; }
    requests.get(channel)?.abort();
    const controller = new AbortController();
    requests.set(channel, controller);
    const version = generation;
    const valid = (): boolean => {
      try {
        return !disposed && version === generation && requests.get(channel) === controller
          && sameIdentitySnapshot(identity, client.identitySnapshot());
      } catch { return false; }
    };
    return { valid, signal: controller.signal };
  }
  return { begin, hasIdentity, invalidate };
}

export function quoteError(status: number, error?: components["schemas"]["ApiErrorResponse"] | components["schemas"]["QuoteFileApiError"]): string {
  const labels: Record<number, string> = {
    400: "参数无效", 403: "当前身份无权执行此操作", 404: "记录不存在或尚未配置",
    409: "绑定失效、状态冲突或已过期；请核对持久记录后重新确认",
    429: "请求受限；不自动重试", 503: "服务未配置或暂不可用，并非空数据",
  };
  return `${labels[status] ?? "请求结果待核对"}${error ? `：${codeLabel(error.code)} · ${error.message}` : ""}`;
}

export function useQuoteConfirmation(client: ApiIdentityReader, scope: () => readonly unknown[], reset: () => void) {
  const message = ref("");
  const key = ref("");
  const pending = ref(false);
  let intent = "";
  const gate = useQuoteRequestScope(client, scope, () => {
    message.value = ""; key.value = ""; intent = ""; pending.value = false; reset();
  });
  watch(scope, () => {
    if (key.value) message.value = pending.value
      ? "原请求结果待核对；输入已标为新意图。保留原键，不自动重发；仅明确发起新确认时创建新键"
      : "输入已标为新意图；原键保留供核对，仅明确发起新确认时提交";
    pending.value = false;
  }, { flush: "sync", deep: true });
  async function confirm<B, D>(body: B, send: (key: string, signal: AbortSignal) => Promise<{
    data?: D; error?: components["schemas"]["ApiErrorResponse"]; response: Response;
  }>, apply: (data: D) => void): Promise<void> {
    if (pending.value || !gate.hasIdentity.value) return;
    const operation = gate.begin("confirm");
    if (!operation?.valid()) return;
    const nextIntent = JSON.stringify(body);
    if (nextIntent !== intent || !key.value) { intent = nextIntent; key.value = crypto.randomUUID(); }
    pending.value = true; message.value = "提交中；切换页面或身份不会取消服务端操作";
    try {
      if (!operation.valid()) return;
      const result = await send(key.value, operation.signal);
      if (!operation.valid()) return;
      if (result.data !== undefined && result.response.ok) {
        apply(result.data); message.value = "已保存；确认身份与来源已留痕";
      } else message.value = quoteError(result.response.status, result.error);
    } catch {
      if (operation.valid()) message.value = "结果未知，待核对；保留原幂等键。请先读取已保存记录，同一意图确认仍使用原键";
    } finally { if (operation.valid()) pending.value = false; }
  }
  return { ...gate, confirm, message, key, pending };
}
