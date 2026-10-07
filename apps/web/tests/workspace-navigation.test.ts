import { createApp, h, nextTick, type App } from "vue";
import { createMemoryHistory, createRouter } from "vue-router";
import { afterEach, describe, expect, it } from "vitest";
import WorkspaceNavigation from "../src/components/WorkspaceNavigation.vue";
import applicationRouter from "../src/router";

let app: App | undefined;
afterEach(() => { app?.unmount(); document.body.replaceChildren(); });

async function mountNavigation(path: string) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: applicationRouter.getRoutes().map(route => ({ path: route.path, component: { template: "<div />" } })),
  });
  await router.replace(path);
  const host = document.createElement("div");
  document.body.append(host);
  app = createApp({ render: () => h("div", [h(WorkspaceNavigation, { level: "primary" }), h(WorkspaceNavigation, { level: "secondary" })]) });
  app.use(router).mount(host);
  await nextTick();
  return { host, router };
}

describe("统一工作区导航", () => {
  it.each([
    ["/crm/handoffs/hf_synthetic", "工作台", "待跟进"],
    ["/approvals?approval_id=apr_synthetic", "工作台", "待审批"],
    ["/demand/needs/need_synthetic", "客户", "需求证据"],
    ["/sourcing/case_synthetic", "客户", "寻源"],
    ["/costing-quotes/quotes/quote_synthetic", "客户", "成本与报价"],
    ["/inbox/mailbox", "消息", "我的邮箱"],
    ["/runs?run=run_synthetic", "企业设置", "运行记录"],
  ])("详情深链 %s 保留所属导航和唯一当前项", async (path, primary, secondary) => {
    const { host } = await mountNavigation(path);
    expect([...host.querySelectorAll('.primary a[aria-current="page"]')].map(link => link.textContent)).toEqual([primary]);
    expect([...host.querySelectorAll('.secondary a[aria-current="page"]')].map(link => link.textContent)).toEqual([secondary]);
  });

  it("用户可切换五组并通过二级入口打开实际注册的页面", async () => {
    const { host, router } = await mountNavigation("/crm/handoffs");
    const primaryLinks = [...host.querySelectorAll<HTMLAnchorElement>(".primary a")];
    expect(primaryLinks).toHaveLength(5);
    for (const primary of primaryLinks) {
      primary.click();
      await new Promise(resolve => setTimeout(resolve, 0));
      await nextTick();
      expect(router.currentRoute.value.path).toBe(primary.getAttribute("href"));
      for (const secondary of host.querySelectorAll<HTMLAnchorElement>(".secondary a")) {
        expect(applicationRouter.resolve(secondary.getAttribute("href")!).matched.length).toBeGreaterThan(0);
      }
    }
    const runs = host.querySelector<HTMLAnchorElement>('.secondary a[href="/runs"]')!;
    runs.click();
    await new Promise(resolve => setTimeout(resolve, 0));
    await nextTick();
    expect(router.currentRoute.value.path).toBe("/runs");
    expect(runs.getAttribute("aria-current")).toBe("page");
  });
});
