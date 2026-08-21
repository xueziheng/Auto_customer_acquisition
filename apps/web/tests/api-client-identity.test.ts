import { describe, expect, it, vi } from "vitest";

import {
  WebIdentityError,
  createApiClient,
  type WebIdentityProvider,
} from "../src/api/client";

function response(): Response {
  return new Response(JSON.stringify([]), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

describe("API request identity", () => {
  it("injects one central authenticated identity and overwrites caller assertions", async () => {
    const requests: Request[] = [];
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      if (!(input instanceof Request)) throw new TypeError("Request required");
      requests.push(input);
      return response();
    });
    const identity: WebIdentityProvider = {
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

    expect(requests[0]?.headers.get("X-Tenant-Id")).toBe("tn-authenticated");
    expect(requests[0]?.headers.get("X-Employee-Id")).toBe("emp-authenticated");
  });

  it("fails before fetch when a production identity provider has no session", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>();
    const identity: WebIdentityProvider = {
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
      { current: () => null },
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
