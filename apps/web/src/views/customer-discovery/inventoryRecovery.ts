import type { components } from "../../api/api";
import type { ApiIdentityReader } from "../../api/client";

type Claim = components["schemas"]["InventoryRequestBody"];
export interface PendingInventoryClaim { key: string; body: Claim }
const storageKey = "tradeos-inventory-pending-v1";
const sessions = new WeakMap<ApiIdentityReader, ReturnType<typeof createRecovery>>();

function identityKey(client: ApiIdentityReader): string | null {
  const identity = client.identitySnapshot().identity;
  return identity ? JSON.stringify([identity.mode, identity.tenantId, identity.employeeId]) : null;
}

function parsePending(raw: string, owner: string): PendingInventoryClaim | null {
  try {
    const value = JSON.parse(raw);
    const query = value.body?.query;
    if (value.owner !== owner || typeof value.key !== "string" || !/^[\w-]{1,128}$/.test(value.key) ||
        !Number.isInteger(value.body?.count) || value.body.count < 1 || value.body.count > 100 ||
        !query || !Array.isArray(query.product_tags) || !query.product_tags.length ||
        !query.product_tags.every((tag: unknown) => typeof tag === "string" && tag.length > 0 && tag.length <= 500) ||
        ![null, "africa"].includes(query.region) || !(query.country === null || typeof query.country === "string" && /^[A-Z]{2}$/.test(query.country))) return null;
    return { key: value.key, body: { count: value.body.count, query: { product_tags: query.product_tags, region: query.region, country: query.country } } };
  } catch { return null; }
}

function createRecovery(client: ApiIdentityReader) {
  let owner = identityKey(client);
  let pending: PendingInventoryClaim | null = null;
  let durable = true;
  try {
    const raw = globalThis.sessionStorage.getItem(storageKey);
    pending = raw && owner ? parsePending(raw, owner) : null;
    if (raw && !pending) globalThis.sessionStorage.removeItem(storageKey);
  } catch { durable = false; }
  const clear = () => {
    pending = null;
    try { globalThis.sessionStorage.removeItem(storageKey); } catch { durable = false; }
  };
  // 组件未挂载时也要清理身份变更，旧请求不得恢复到另一个员工。
  client.subscribeIdentity(() => { clear(); owner = identityKey(client); });
  return {
    read: () => pending,
    durable: () => durable,
    save(value: PendingInventoryClaim) {
      if (!owner) throw new Error("inventory_identity_required");
      pending = value;
      try { globalThis.sessionStorage.setItem(storageKey, JSON.stringify({ owner, ...value })); } catch { durable = false; }
    },
    clear(expectedKey: string) {
      if (pending?.key === expectedKey) clear();
    },
  };
}

export function inventoryRecovery(client: ApiIdentityReader) {
  let recovery = sessions.get(client);
  if (!recovery) { recovery = createRecovery(client); sessions.set(client, recovery); }
  return recovery;
}
