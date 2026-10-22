import { ref } from "vue";
import type { components } from "../../api/api";
import type { ApiIdentityReader } from "../../api/client";
export interface PendingFollowup {
  allocationId: string;
  key: string;
  body: components["schemas"]["CustomerFollowupCommand"];
}
const storageKey = "tradeos-customer-followup-pending-v1";
const sessions = new WeakMap<
  ApiIdentityReader,
  ReturnType<typeof createRecovery>
>();
function ownerKey(client: ApiIdentityReader) {
  const i = client.identitySnapshot().identity;
  return i ? JSON.stringify([i.mode, i.tenantId, i.employeeId]) : null;
}
function createRecovery(client: ApiIdentityReader) {
  let owner = ownerKey(client),
    durable = true;
  const records = ref<PendingFollowup[]>([]);
  function persist(next: PendingFollowup[]) {
    try {
      if (next.length && owner)
        globalThis.sessionStorage.setItem(
          storageKey,
          JSON.stringify({ owner, records: next }),
        );
      else globalThis.sessionStorage.removeItem(storageKey);
    } catch {
      durable = false;
      throw new Error("followup_recovery_unavailable");
    }
  }
  function removeWhere(keep: (record: PendingFollowup) => boolean) {
    const next = records.value.filter(keep);
    try {
      persist(next);
    } catch {
      // 已确认结果的清理失败仍保留原核对依据，不替换成新命令。
      return;
    }
    records.value = next;
  }
  try {
    const raw = globalThis.sessionStorage.getItem(storageKey);
    if (raw) {
      const saved = JSON.parse(raw);
      if (
        owner &&
        saved.owner === owner &&
        Array.isArray(saved.records) &&
        saved.records.length <= 100 &&
        saved.records.every(
          (p: PendingFollowup) =>
            typeof p.allocationId === "string" &&
            /^[\w-]{1,128}$/.test(p.allocationId) &&
            typeof p.key === "string" &&
            /^[\w-]{1,128}$/.test(p.key) &&
            p.body &&
            [
              p.body.campaign_id,
              p.body.contact_point_id,
              p.body.source_hypothesis_id,
            ].every((v) => typeof v === "string" && /^[\w-]{1,128}$/.test(v)) &&
            Number.isSafeInteger(p.body.campaign_version) &&
            p.body.campaign_version > 0 &&
            (p.body.execution_mode === undefined ||
              p.body.execution_mode === "automatic" || p.body.execution_mode === "manual"),
        )
      ) {
        records.value = saved.records.map((p: PendingFollowup) => ({
          allocationId: p.allocationId,
          key: p.key,
          body: {
            campaign_id: p.body.campaign_id,
            contact_point_id: p.body.contact_point_id,
            source_hypothesis_id: p.body.source_hypothesis_id,
            campaign_version: p.body.campaign_version,
            ...(p.body.execution_mode === undefined ? {} : { execution_mode: p.body.execution_mode }),
          },
        }));
      } else globalThis.sessionStorage.removeItem(storageKey);
    }
  } catch {
    durable = false;
  }
  client.subscribeIdentity(() => {
    records.value = [];
    owner = ownerKey(client);
    try {
      persist([]);
    } catch {
      // 当前身份不可继续读取上一身份记录。
    }
  });
  return {
    records,
    durable: () => durable,
    find: (id: string) => records.value.find((p) => p.allocationId === id),
    save(p: PendingFollowup) {
      if (p.body.execution_mode !== undefined &&
          p.body.execution_mode !== "automatic" && p.body.execution_mode !== "manual")
        throw new Error("followup_recovery_conflict");
      const existing = records.value.find(v => v.allocationId === p.allocationId);
      if (existing) {
        if (
          existing.key !== p.key ||
          JSON.stringify(existing.body) !== JSON.stringify(p.body)
        )
          throw new Error("followup_recovery_conflict");
        return;
      }
      if (!owner || !durable || records.value.length >= 100)
        throw new Error("followup_recovery_unavailable");
      const next = [...records.value, { ...p, body: { ...p.body } }];
      // 精确原命令先持久化；任何写失败都不能进入POST或改变旧未决记录。
      persist(next);
      records.value = next;
    },
    clear(id: string, key: string) {
      removeWhere(p => !(p.allocationId === id && p.key === key));
    },
    clearAll() {
      records.value = [];
      try {
        persist([]);
      } catch {
        // 权限失效时清除当前内存视图。
      }
    },
    forget(id: string) {
      records.value = records.value.filter(p => p.allocationId !== id);
      try {
        persist(records.value);
      } catch {
        // 权限失效时清除当前内存视图。
      }
    },
  };
}
export function followupRecovery(client: ApiIdentityReader) {
  let value = sessions.get(client);
  if (!value) {
    value = createRecovery(client);
    sessions.set(client, value);
  }
  return value;
}
