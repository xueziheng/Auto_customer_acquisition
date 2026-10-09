// 回调只在当前页面内存中短暂保存；模块先于登录组件加载，URL 不保留授权码。
interface Callback { code: string; state: string; failed: boolean }
export function captureGmailCallback(
  location: Pick<Location, "pathname" | "search">,
  history: Pick<History, "replaceState">,
): Callback | null {
  if (location.pathname !== "/inbox/mailbox/google-callback") return null;
  const query = new URLSearchParams(location.search);
  history.replaceState(null, "", "/inbox/mailbox");
  const code = query.get("code") ?? "";
  const state = query.get("state") ?? "";
  const failed = query.has("error") || query.getAll("code").length !== 1
    || query.getAll("state").length !== 1 || !code || !state
    || code.length > 4096 || state.length > 128;
  return { code: failed ? "" : code, state: failed ? "" : state, failed };
}
let pending = captureGmailCallback(window.location, window.history);
export function takeGmailCallback(): Callback | null {
  const result = pending; pending = null; return result;
}
