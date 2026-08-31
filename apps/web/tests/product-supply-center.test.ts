import { createApp, nextTick, type App as VueApp } from "vue";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "../src/App.vue";
import { createApiClient } from "../src/api/client";
import router from "../src/router";

const mountedApps: VueApp[] = [];

afterEach(() => {
  mountedApps.splice(0).forEach((app) => app.unmount());
  void router.replace("/");
});

async function eventually(assertion: () => void): Promise<void> {
  let latest: unknown;
  for (let attempt = 0; attempt < 80; attempt += 1) {
    await nextTick();
    await new Promise((resolve) => setTimeout(resolve, 0));
    try {
      assertion();
      return;
    } catch (error) {
      latest = error;
    }
  }
  throw latest;
}

describe("ProductSupplyCenter", () => {
  it("reloads the safe list with source_only=true after the operator turns on the filter", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      const path = new URL((input as Request).url).pathname;
      if (path === "/notifications") return new Response("[]", { status: 200 });
      if (path === "/products") return new Response("[]", { status: 200 });
      return new Response("{}", { status: 500 });
    });
    const root = document.createElement("div");
    document.body.replaceChildren(root);
    const app = createApp(App);
    app.provide(
      "tradeos-api-client",
      createApiClient({ baseUrl: "https://tradeos.test", fetch }),
    );
    app.use(router);
    await router.replace("/products");
    app.mount(root);
    mountedApps.push(app);

    await eventually(() => {
      expect(root.querySelector("input[type=checkbox]")).not.toBeNull();
    });
    (root.querySelector("input[type=checkbox]") as HTMLInputElement).click();
    await eventually(() => {
      expect(fetch.mock.calls.some(([input]) => (
        new URL((input as Request).url).searchParams.get("source_only") === "true"
      ))).toBe(true);
    });
  });
});
