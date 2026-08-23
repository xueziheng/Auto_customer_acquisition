import { describe, expect, it, vi } from "vitest";

import {
  WebIdentityError,
  createApiClient,
  type WebIdentityProvider,
} from "../src/api/client";

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
    expect(request.headers.get("x-tenant-id")).toBe(uploadView.tenant_id);
    expect(request.headers.get("x-employee-id")).toBe(uploadView.employee_id);
    expect(await request.text()).toBe("pdf-bytes");
    const url = new URL(request.url);
    expect(url.pathname).toBe("/work-uploads");
    expect(url.searchParams.get("artifact_kind")).toBe("pdf");
    expect(url.searchParams.get("source_kind")).toBe("pdf_text");
    expect(url.searchParams.get("customer_timezone")).toBe("Asia/Shanghai");
    expect(url.searchParams.get("occurred_at")).toBe("2026-08-23T07:30:00Z");
  });
});
