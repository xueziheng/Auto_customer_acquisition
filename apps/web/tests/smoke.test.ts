import { createApp } from "vue";
import { describe, expect, expectTypeOf, it, vi } from "vitest";

import App from "../src/App.vue";
import { createApiClient } from "../src/api/client";
import type { paths } from "../src/api/api";
import router from "../src/router";

type OpportunityListResponse = paths["/crm/opportunities"]["get"]["responses"][200]["content"]["application/json"];
type HandoffQueueResponse = paths["/crm/handoffs"]["get"]["responses"][200]["content"]["application/json"];

describe("web application foundation", () => {
  it("registers the exact opportunity and handoff routes", () => {
    const app = createApp(App);

    expect(() => app.use(router)).not.toThrow();
    expect(router.getRoutes().map((route) => route.path)).toContain("/crm/opportunities");
    expect(router.getRoutes().map((route) => route.path)).toContain("/crm/handoffs");
  });

  it("uses generated CRM paths through the typed fetch wrapper without network access", async () => {
    const fetch = vi.fn(async (request: Request) => {
      expect(request.url).toBe("https://tradeos.test/crm/opportunities");
      return new Response(JSON.stringify([]), {
        headers: { "content-type": "application/json" },
        status: 200,
      });
    });
    const client = createApiClient({ baseUrl: "https://tradeos.test", fetch });

    const response = await client.GET("/crm/opportunities");

    expect(response.data).toEqual([]);
    expect(fetch).toHaveBeenCalledOnce();
    expectTypeOf<OpportunityListResponse>().toExtend<ReadonlyArray<unknown>>();
    expectTypeOf<HandoffQueueResponse>().toExtend<ReadonlyArray<unknown>>();
  });
});

describe("slice4 internal operations routes and types", () => {
  it("registers the outreach, sending identity and notification routes", () => {
    const app = createApp(App);

    expect(() => app.use(router)).not.toThrow();
    expect(router.getRoutes().map((route) => route.path)).toContain("/crm/outreach");
    expect(router.getRoutes().map((route) => route.path)).toContain("/crm/sending-identities");
    expect(router.getRoutes().map((route) => route.path)).toContain("/notifications");
  });

  it("consumes generated slice4 paths through the typed fetch wrapper", async () => {
    type EnrollmentsResponse =
      paths["/crm/enrollments"]["get"]["responses"][200]["content"]["application/json"];
    type NotificationsResponse =
      paths["/notifications"]["get"]["responses"][200]["content"]["application/json"];
    const fetch = vi.fn(async (request: Request) => {
      expect(request.url).toMatch(/https:\/\/tradeos\.test\/(crm\/enrollments|notifications)$/);
      return new Response(JSON.stringify([]), {
        headers: { "content-type": "application/json" },
        status: 200,
      });
    });
    const client = createApiClient({ baseUrl: "https://tradeos.test", fetch });

    const enrollments = await client.GET("/crm/enrollments");
    const notifications = await client.GET("/notifications");

    expect(enrollments.data).toEqual([]);
    expect(notifications.data).toEqual([]);
    expect(fetch).toHaveBeenCalledTimes(2);
    expectTypeOf<EnrollmentsResponse>().toExtend<ReadonlyArray<unknown>>();
    expectTypeOf<NotificationsResponse>().toExtend<ReadonlyArray<unknown>>();
  });
});
