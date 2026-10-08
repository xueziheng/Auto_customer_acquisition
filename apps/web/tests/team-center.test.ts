import { createApp, nextTick, type App as VueApp } from "vue";
import { describe, expect, it, vi } from "vitest";

import type { components } from "../src/api/api";
import { createApiClient } from "../src/api/client";
import App from "../src/App.vue";
import router from "../src/router";

type Employee = components["schemas"]["EmployeeView"];
type Territory = components["schemas"]["TerritoryAssignmentView"];

const employees: Employee[] = [
  {
    employee_id: "emp_01K39P9M5D6K4A91YEQ80EJZ0X",
    is_active: true,
    languages: ["zh", "en"],
    manager_id: null,
    max_active_accounts: 40,
    name: "张三",
    role: "sales",
    team_id: "team_01K39P9M5D6K4A91YEQ80EJZ0X",
    tenant_id: "tn_01K39P9M5D6K4A91YEQ80EJZ0X",
    timezone: "Asia/Shanghai",
    user_id: null,
  },
  {
    employee_id: "emp_01K39P9M5D6K4A91YEQ80EJZ0Y",
    is_active: true,
    languages: ["en"],
    manager_id: null,
    max_active_accounts: null,
    name: "李经理",
    role: "manager",
    team_id: null,
    tenant_id: "tn_01K39P9M5D6K4A91YEQ80EJZ0X",
    timezone: "Europe/London",
    user_id: null,
  },
];

const territory: Territory[] = [{
  backup_employee_id: employees[1]!.employee_id,
  buyer_types: ["wholesaler"],
  countries: ["US", "CA"],
  effective_from: "2026-08-01T00:00:00Z",
  effective_until: null,
  employee_id: employees[0]!.employee_id,
  languages: ["en"],
  manager_id: employees[1]!.employee_id,
  need_categories: ["industrial_hardware"],
  priority: 1,
  product_categories: ["hinges"],
  tenant_id: employees[0]!.tenant_id,
}];

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

async function eventually(assertion: () => void): Promise<void> {
  let latestError: unknown;
  for (let attempt = 0; attempt < 80; attempt += 1) {
    await nextTick();
    await new Promise((resolve) => setTimeout(resolve, 0));
    try {
      assertion();
      return;
    } catch (error) {
      latestError = error;
    }
  }
  throw latestError;
}

async function mountTeam(fetch: typeof globalThis.fetch): Promise<{
  app: VueApp;
  root: HTMLElement;
}> {
  const root = document.createElement("div");
  document.body.replaceChildren(root);
  const app = createApp(App);
  app.provide(
    "tradeos-api-client",
    createApiClient({ baseUrl: "https://tradeos.test", fetch }),
  );
  app.use(router);
  await router.replace("/team");
  app.mount(root);
  await new Promise((resolve) => setTimeout(resolve, 0));
  return { app, root };
}

describe("TeamCenter", () => {
  it("renders active employees and deterministic territory rules without fuzzy scores", async () => {
    const requested: string[] = [];
    const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
      if (!(input instanceof Request)) throw new TypeError("Request required");
      const path = new URL(input.url).pathname;
      requested.push(path);
      if (path === "/team/employees") return jsonResponse(employees);
      if (path === "/team/territory") return jsonResponse(territory);
      return jsonResponse({ code: "unexpected", message: "unexpected" }, 500);
    });

    const { root } = await mountTeam(fetch);

    await eventually(() => {
      expect(root.textContent).toContain("张三");
      expect(root.textContent).toContain("李经理");
      expect(root.textContent).toContain("US · CA");
      expect(root.textContent).toContain("industrial_hardware");
      expect(root.textContent).toContain("优先级 1");
    });
    expect(requested.filter((path) => path.startsWith("/team/")).sort()).toEqual([
      "/team/employees",
      "/team/territory",
    ]);
    expect(root.textContent).not.toContain("接口尚未装配");
    expect(root.textContent).not.toMatch(/能力分|绩效分|AI 评分/);
  });

  it("shows a truthful permission message when the boss-only query is rejected", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>(async () =>
      jsonResponse({ code: "permission_denied", message: "denied" }, 403),
    );

    const { root } = await mountTeam(fetch);

    await eventually(() => {
      expect(root.textContent).toContain("只有企业管理员可以查看完整团队与分配矩阵");
    });
  });
});
