import { afterEach, describe, expect, it, vi } from "vitest";

import {
  WebIdentityError,
  createApiClient,
  configureAuthenticatedIdentity,
  clearAuthenticatedIdentity,
  type WebIdentityProvider,
} from "../src/api/client";
import { sameIdentitySnapshot } from "../src/views/costing-quotes/quote-request-scope";

const unsubscribers: (() => void)[] = [];
afterEach(() => {
  unsubscribers.splice(0).forEach((unsubscribe) => unsubscribe());
  clearAuthenticatedIdentity();
});

const uploadView = {
  account_id: null,
  artifact_id: "art_01K39P9M5D6K4A91YEQ80EJZ0X",
  created_at: "2026-08-23T08:00:00Z",
  customer_timezone: "Asia/Shanghai",
  employee_id: "emp_01K39P9M5D6K4A91YEQ80EJZ0X",
  need_id: null,
  occurred_at: "2026-08-23T07:30:00Z",
  opportunity_id: null,
  source_kind: "pdf_text" as const,
  status: "uploaded" as const,
  tenant_id: "tn_01K39P9M5D6K4A91YEQ80EJZ0X",
  upload_id: "upl_01K39P9M5D6K4A91YEQ80EJZ0X",
};

function response(): Response {
  return new Response(JSON.stringify([]), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

describe("API request identity", () => {
  it("uses session authentication and removes caller identity assertions", async () => {
    const requests: Request[] = [];
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      if (!(input instanceof Request)) throw new TypeError("Request required");
      requests.push(input);
      return response();
    });
    const identity: WebIdentityProvider = {
      generation: () => 0,
      subscribe: () => () => {},
      current: () => ({
        employeeId: "emp-authenticated",
        mode: "authenticated",
        tenantId: "tn-authenticated",
      }),
    };
    const client = createApiClient(
      { baseUrl: "https://tradeos.test", fetch },
      identity,
    );

    await client.GET("/notifications", {
      headers: {
        "X-Employee-Id": "emp-forged",
        "X-Tenant-Id": "tn-forged",
      },
    });

    expect(requests[0]?.headers.get("X-Tenant-Id")).toBeNull();
    expect(requests[0]?.headers.get("X-Employee-Id")).toBeNull();
  });

  it("fails before fetch when a production identity provider has no session", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>();
    const identity: WebIdentityProvider = {
      generation: () => 0,
      subscribe: () => () => {},
      current: () => {
        throw new WebIdentityError("authenticated_identity_missing");
      },
    };
    const client = createApiClient(
      { baseUrl: "https://tradeos.test", fetch },
      identity,
    );

    await expect(client.GET("/notifications")).rejects.toThrow(
      "authenticated_identity_missing",
    );
    expect(fetch).not.toHaveBeenCalled();
  });

  it("strips local assertion headers when no identity is configured", async () => {
    const requests: Request[] = [];
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      if (!(input instanceof Request)) throw new TypeError("Request required");
      requests.push(input);
      return response();
    });
    const client = createApiClient(
      { baseUrl: "https://tradeos.test", fetch },
      { current: () => null, generation: () => 0, subscribe: () => () => {} },
    );

    await client.GET("/notifications", {
      headers: {
        "X-Employee-Id": "emp-local",
        "X-Tenant-Id": "tn-local",
      },
    });

    expect(requests[0]?.headers.has("X-Tenant-Id")).toBe(false);
    expect(requests[0]?.headers.has("X-Employee-Id")).toBe(false);
  });
});

describe("API raw work upload", () => {
  it("sends binary content through the same tenant and employee identity boundary", async () => {
    let captured: Request | null = null;
    const transport = vi.fn<typeof globalThis.fetch>(async (input) => {
      if (!(input instanceof Request)) throw new TypeError("expected Request");
      captured = input;
      return new Response(JSON.stringify(uploadView), {
        status: 201,
        headers: { "content-type": "application/json" },
      });
    });
    const client = createApiClient(
      { baseUrl: "https://tradeos.test", fetch: transport },
      {
        generation: () => 0,
        subscribe: () => () => {},
        current: () => ({
          employeeId: uploadView.employee_id,
          mode: "authenticated",
          tenantId: uploadView.tenant_id,
        }),
      },
    );

    const result = await client.uploadWorkArtifact({
      artifactKind: "pdf",
      body: new Blob(["pdf-bytes"], { type: "application/pdf" }),
      customerTimezone: "Asia/Shanghai",
      occurredAt: "2026-08-23T07:30:00Z",
      sourceKind: "pdf_text",
    });

    expect(result.data).toEqual(uploadView);
    expect(captured).not.toBeNull();
    const request = captured as unknown as Request;
    expect(request.method).toBe("POST");
    expect(request.headers.get("content-type")).toBe("application/pdf");
    expect(request.headers.get("x-tenant-id")).toBeNull();
    expect(request.headers.get("x-employee-id")).toBeNull();
    expect(await request.text()).toBe("pdf-bytes");
    const url = new URL(request.url);
    expect(url.pathname).toBe("/work-uploads");
    expect(url.searchParams.get("artifact_kind")).toBe("pdf");
    expect(url.searchParams.get("source_kind")).toBe("pdf_text");
    expect(url.searchParams.get("customer_timezone")).toBe("Asia/Shanghai");
    expect(url.searchParams.get("occurred_at")).toBe("2026-08-23T07:30:00Z");
  });
});

describe("identity snapshots and invalidation", () => {
  it("invalidates A-B-A and same-identity reconfiguration synchronously", () => {
    const client = createApiClient({ baseUrl: "https://tradeos.test" });
    configureAuthenticatedIdentity("a", "a");
    const before = client.identitySnapshot();
    expect(sameIdentitySnapshot(before, client.identitySnapshot())).toBe(true);
    const seen: number[] = [];
    unsubscribers.push(client.subscribeIdentity(() => seen.push(client.identitySnapshot().generation)));
    configureAuthenticatedIdentity("b", "b");
    configureAuthenticatedIdentity("a", "a");
    expect(sameIdentitySnapshot(before, client.identitySnapshot())).toBe(false);
    const again = client.identitySnapshot();
    configureAuthenticatedIdentity("a", "a");
    expect(sameIdentitySnapshot(again, client.identitySnapshot())).toBe(false);
    expect(seen).toHaveLength(3);
    expect(new Set(seen).size).toBe(3);
    expect(Object.isFrozen(before)).toBe(true);
    expect(Object.isFrozen(before.identity)).toBe(true);
  });

  it("compares values rather than newly returned provider objects", () => {
    const client = createApiClient({}, {
      current: () => ({ tenantId: "t", employeeId: "e", mode: "fixed-dev" }),
      generation: () => 0,
      subscribe: () => () => {},
    });
    expect(sameIdentitySnapshot(client.identitySnapshot(), client.identitySnapshot())).toBe(true);
  });

  it.each([-1, 1.5, Infinity, Number.MAX_SAFE_INTEGER + 1])("rejects invalid generation %s with a fixed error", (generation) => {
    const client = createApiClient({}, {
      current: () => null, generation: () => generation, subscribe: () => () => {},
    });
    expect(() => client.identitySnapshot()).toThrowError(new WebIdentityError("identity_snapshot_invalid"));
  });

  it("sanitizes provider errors and invalid identity snapshots", () => {
    for (const current of [
      () => { throw new Error("sensitive-provider-content"); },
      () => ({ tenantId: " t", employeeId: "e", mode: "authenticated" as const }),
    ]) {
      const client = createApiClient({}, { current, generation: () => 0, subscribe: () => () => {} });
      expect(() => client.identitySnapshot()).toThrowError(new WebIdentityError("identity_snapshot_invalid"));
    }
  });

  it("notifies every subscriber despite errors and unsubscribes without client-construction leaks", () => {
    const client = createApiClient();
    let called = 0;
    unsubscribers.push(client.subscribeIdentity(() => { throw new Error("private"); }));
    const unsubscribe = client.subscribeIdentity(() => { called += 1; });
    unsubscribers.push(unsubscribe);
    expect(() => configureAuthenticatedIdentity("t", "e")).toThrowError(new WebIdentityError("identity_notification_failed"));
    expect(called).toBe(1);
    expect(client.identitySnapshot().identity?.employeeId).toBe("e");
    unsubscribe();
    expect(() => clearAuthenticatedIdentity()).toThrowError(new WebIdentityError("identity_notification_failed"));
    expect(called).toBe(1);
  });

  it("uses central short identity on typed PDF GET and preserves classified JSON errors", async () => {
    configureAuthenticatedIdentity("t", "e");
    const requests: Request[] = [];
    const client = createApiClient({ baseUrl: "https://tradeos.test", fetch: async (input) => {
      if (!(input instanceof Request)) throw new Error("Request required");
      requests.push(input);
      return new Response(JSON.stringify({ detail: { code: "file_expired", message: "已过期" } }), {
        status: 409, headers: { "content-type": "application/json" },
      });
    } });
    const result = await client.GET("/costing-quotes/quotes/{quote_id}/files/{file_id}", {
      params: { path: { quote_id: "q", file_id: "f" } }, parseAs: "blob",
      headers: { "X-Tenant-Id": "forged", "X-Employee-Id": "forged" },
    });
    expect(requests[0]?.headers.get("X-Tenant-Id")).toBeNull();
    expect(requests[0]?.headers.get("X-Employee-Id")).toBeNull();
    expect(result.error).toEqual({ detail: { code: "file_expired", message: "已过期" } });
  });
});
