import { ref } from "vue";
import type { components } from "../../api/api";
import type { ApiIdentityReader } from "../../api/client";

type Command = components["schemas"]["ReplyWorkUpdate"];
const sessions = new WeakMap<ApiIdentityReader, ReturnType<typeof createRecovery>>();

function createRecovery(client: ApiIdentityReader) {
  const records = ref<Record<string, Command>>({});
  const clear = () => { records.value = {}; };
  client.subscribeIdentity(clear);
  return {
    records, clear,
    save(actionId: string, command: Command) {
      if (records.value[actionId]) throw new Error("handling_request_unresolved");
      records.value[actionId] = { ...command };
    },
    forget(actionId: string, requestId: string | null | undefined) {
      if (records.value[actionId]?.request_id === requestId) delete records.value[actionId];
    },
  };
}

/** 仅保留当前身份在内存中的未决请求；说明不写入浏览器持久存储。 */
export function replyWorkRecovery(client: ApiIdentityReader) {
  let recovery = sessions.get(client);
  if (!recovery) {
    recovery = createRecovery(client);
    sessions.set(client, recovery);
  }
  return recovery;
}
