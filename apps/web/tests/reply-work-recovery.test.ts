import { describe, expect, it } from "vitest";
import { replyWorkRecovery } from "../src/views/inbox/replyWorkRecovery";
import type { ApiIdentityReader, WebIdentitySnapshot } from "../src/api/client";

describe("回复处理未决请求的页面生命周期", () => {
  it("离页释放监听，同身份重开保留原请求，离页期间换身份清除旧请求", () => {
    let generation = 0;
    const listeners = new Set<() => void>();
    const client: ApiIdentityReader = {
      identitySnapshot: (): WebIdentitySnapshot => ({ generation, identity: { tenantId: "company", employeeId: `employee-${generation}`, mode: "authenticated" } }),
      subscribeIdentity: listener => { listeners.add(listener); return () => listeners.delete(listener); },
    };
    const first = replyWorkRecovery(client);
    first.save("action", { expected_status: "pending", status: "in_progress", disposition: "started", note: "", request_id: "original-request" });
    expect(listeners.size).toBe(1);
    first.dispose(); first.dispose();
    expect(listeners.size).toBe(0);
    const reopened = replyWorkRecovery(client);
    expect(reopened.records.value.action?.request_id).toBe("original-request");
    reopened.dispose(); generation++;
    const other = replyWorkRecovery(client);
    expect(other.records.value).toEqual({});
    other.dispose();
  });
});
