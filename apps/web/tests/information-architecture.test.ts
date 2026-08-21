import { describe, expect, it } from "vitest";

import router from "../src/router";

const phase1Pages = [
  "/commands",
  "/demand",
  "/prospects/accounts",
  "/campaigns",
  "/inbox",
  "/crm/opportunities",
  "/products",
  "/sourcing",
  "/costing-quotes",
  "/team",
  "/work-uploads",
  "/commitments",
  "/approvals",
  "/runs",
  "/settings",
  "/billing",
] as const;

describe("Phase 1 information architecture", () => {
  it("registers every handbook page without Phase 2 automation routes", () => {
    const paths = new Set(router.getRoutes().map((route) => route.path));

    for (const path of phase1Pages) expect(paths.has(path), path).toBe(true);
    expect([...paths].some((path) => path.includes("subscriptions"))).toBe(false);
    expect([...paths].some((path) => path.includes("wallet"))).toBe(false);
    expect([...paths].some((path) => path.includes("whatsapp"))).toBe(false);
  });

  it("marks product, sourcing and costing routes as manual Phase 1 work", () => {
    for (const path of ["/products", "/sourcing", "/costing-quotes"]) {
      const route = router.getRoutes().find((candidate) => candidate.path === path);
      expect(route?.meta.phase).toBe("phase1-manual");
      expect(route?.meta.operation).toBeTruthy();
    }
  });

  it("keeps billing visibly disabled until Phase 3", () => {
    const route = router.getRoutes().find((candidate) => candidate.path === "/billing");
    expect(route?.meta.phase).toBe("phase3-disabled");
  });
});
