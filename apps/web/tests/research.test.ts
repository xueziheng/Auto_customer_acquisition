import { createApp, nextTick } from "vue";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "../src/api/client";
import CommandCenter from "../src/views/command-center/CommandCenter.vue";
import DemandRadar from "../src/views/demand-radar/DemandRadar.vue";
import CustomerDiscovery from "../src/views/customer-discovery/CustomerDiscovery.vue";
import RunCenter from "../src/views/runs/RunCenter.vue";
import SettingsCenter from "../src/views/settings/SettingsCenter.vue";
import router from "../src/router";

const access = { state: "configured_unverified", provider: "tavily", can_confirm_research: true, remaining_lower_bound: null, checked_at: null, runtime_activation: "not_verified" };
const proposal = {
  proposal_id: "dpr_controlled", raw_text: "只研究美国铰链", interpretation_summary: "推断",
  expected_behavior_changes: ["不补全联系人、不发信、不报价"], state: "pending_confirmation",
  created_at: "2026-08-27T00:00:00Z", execution_mode: "research_only", can_confirm: true,
  planned_discovery_lanes: ["importer", "distributor", "ecommerce"], research_access: access,
  confirmation_blocked_reason: null,
  parsed_fields: { execution_mode: "research_only", target_countries: "US", target_categories: "hinges", max_search_queries: "3", max_pages_read: "7", max_signals: "5", max_hypotheses: "2", campaign_id: "", queries: '[{"query":"US hinges importer","discovery_lane":"importer","country":"US","category":"hinges","limit":3}]' },
};
const signal = {
  signal_id: "sig_controlled", signal_type: "product_launch", entity_name: "Synthetic Hinges",
  raw_observation: "We distribute hinges", possible_need: "可能需要铰链", status: "captured",
  observed_at: "2026-08-27T00:00:00Z", source_type: "web_page", source_ref: "web:test",
  source_url: "https://example.test/about", page_hash: "a".repeat(64), snapshot_artifact_ref: "art_controlled",
  research_evidence: { proposal_id: "dpr_controlled", query: "US hinges importer", discovery_lane: "importer", query_country: "US", query_category: "hinges", source_kind: "directory_listing", identity_status: "pending_verification", company_name: null, website_domain: null, country: null, identity_quote: null, country_quote: null, source_url: "https://example.test/about" },
};
const research = { execution_mode: "research_only", planned_discovery_lanes: ["importer", "distributor", "ecommerce"], discovery_lanes: ["importer"], completion_reason: "budget_exhausted", stop_reason: "quota_exhausted", searches_used: 9, pages_used: 1, signal_count: 1, hypothesis_count: 0, pending_verification_count: 1, validated_need_count: 0, qualified_opportunity_count: 0, queued_count: 0, consumed_credits: 1, reserved_credits: 2, uncertain_credits: 3 };
const run = { run_id: "run_controlled", workflow_type: "demand_discovery", workflow_version: 2, subject_ref: "dpr_controlled", current_step: "complete", status: "completed", created_at: "2026-08-27T00:00:00Z", last_activity_at: "2026-08-27T00:00:00Z", next_poll_at: null, retry_count: 0, last_error: null, research };

async function mount(component: Parameters<typeof createApp>[0], routes: Record<string, unknown>) {
  const requests: string[] = [];
  const fetch = vi.fn<typeof globalThis.fetch>(async (input) => {
    const request = input as Request;
    const path = new URL(request.url).pathname;
    requests.push(`${request.method} ${path}`);
    if (routes[path] instanceof Error) throw routes[path];
    if (routes[path] instanceof Response) return routes[path].clone();
    const exists = path in routes || path.startsWith("/demand/");
    return new Response(JSON.stringify(routes[path] ?? []), { status: exists ? 200 : 503, headers: { "content-type": "application/json" } });
  });
  const root = document.createElement("div");
  document.body.replaceChildren(root);
  const app = createApp(component);
  app.provide("tradeos-api-client", createApiClient({ baseUrl: "https://tradeos.test", fetch }));
  await router.replace("/commands");
  app.use(router);
  app.mount(root);
  cleanup.push(() => app.unmount());
  await settle();
  return { root, requests, app };
}

const cleanup: (() => void)[] = [];
afterEach(() => { cleanup.splice(0).forEach((unmount) => unmount()); });

async function settle() {
  for (let i = 0; i < 10; i++) { await nextTick(); await new Promise((resolve) => setTimeout(resolve, 0)); }
}

describe("公开研究展示", () => {
  it.each([new TypeError("network"), new Response("{}", { status: 503 })])("摘要读取失败和空记录可区分 %s", async (failure) => {
    const routes: Record<string, unknown> = { "/runs": failure };
    const { root } = await mount(DemandRadar, routes);
    expect(root.textContent).toContain("研究运行摘要读取失败");
    expect(root.textContent).not.toContain("暂无研究 Run");
    routes["/runs"] = [];
    [...root.querySelectorAll("button")].find((button) => button.textContent?.trim() === "刷新")!.click();
    await settle();
    expect(root.textContent).toContain("最近记录中暂无研究 Run 摘要");
    expect(root.textContent).not.toContain("研究运行摘要读取失败");
  });
  it.each([false, true])("确认503后只读恢复启动状态，不自动重试：已有Run=%s", async (started) => {
    const routes: Record<string, unknown> = {
      "/commands/discovery-proposals": proposal,
      "/commands/discovery-proposals/dpr_controlled/confirm": new Response("{}", { status: 503 }),
      "/commands/discovery-proposals/dpr_controlled": { ...proposal, state: "confirmed", can_confirm: true },
      "/commands/discovery-proposals/dpr_controlled/execution": { state: started ? "started" : "not_started", run_id: started ? "run_recovered" : null, can_resume: !started },
    };
    const { root, requests } = await mount(CommandCenter, routes);
    const input = root.querySelector("textarea")!;
    input.value = "只研究";
    input.dispatchEvent(new Event("input"));
    root.querySelector("form")!.dispatchEvent(new Event("submit"));
    await settle();
    [...root.querySelectorAll("button")].find((button) => button.textContent?.includes("确认并启动"))!.click();
    await settle();
    [...root.querySelectorAll("button")].find((button) => button.textContent?.includes("刷新提案状态"))!.click();
    await settle();
    expect(requests.filter((request) => request.endsWith("/confirm"))).toHaveLength(1);
    if (started) {
      expect(root.textContent).toContain("run_recovered");
      expect(root.textContent).not.toContain("恢复启动");
    } else {
      expect(root.textContent).toContain("提案已确认，尚未创建 Run");
      routes["/commands/discovery-proposals/dpr_controlled/confirm"] = { proposal_id: "dpr_controlled", directive_id: "dir_controlled", run_id: "run_recovered", workflow_type: "demand_discovery" };
      routes["/commands/discovery-proposals/dpr_controlled/execution"] = { state: "started", run_id: "run_recovered", can_resume: false };
      [...root.querySelectorAll("button")].find((button) => button.textContent?.includes("恢复启动"))!.click();
      await settle();
      expect(root.textContent).toContain("run_recovered");
      expect(requests.filter((request) => request.endsWith("/confirm"))).toHaveLength(2);
    }
  });
  it("负面快照下明确是重新核验请求，不宣称免费已启用", async () => {
    const { root } = await mount(CommandCenter, {
      "/commands/discovery-proposals": { ...proposal, research_access: { ...access, state: "paid_enabled", confirmation_requires_recheck: true } },
    });
    const input = root.querySelector("textarea")!;
    input.value = "只研究";
    input.dispatchEvent(new Event("input"));
    root.querySelector("form")!.dispatchEvent(new Event("submit"));
    await settle();
    expect(root.textContent).toContain("确认重新核验后研究");
    expect(root.textContent).toContain("上次核验发现付费已开启");
    expect(root.textContent).toContain("不代表已允许搜索");
  });
  it.each([DemandRadar, CustomerDiscovery])("页面刷新同步更新研究摘要，失败不当成无记录 %s", async (component) => {
    const routes: Record<string, unknown> = {
      "/prospects/accounts": [],
      "/runs": [{ ...run, status: "running", research: { ...research, consumed_credits: 0, stop_reason: null, completion_reason: null } }],
    };
    const { root, requests } = await mount(component, routes);
    expect(root.textContent).toContain("已消耗 0");
    routes["/runs"] = [{ ...run, research: { ...research, consumed_credits: 3 } }];
    const refresh = [...root.querySelectorAll("button")].find((button) => button.textContent?.trim() === "刷新")!;
    refresh.click();
    await settle();
    expect(root.textContent).toContain("已消耗 3");
    routes["/runs"] = new Response("{}", { status: 503 });
    refresh.click();
    await settle();
    expect(root.textContent).toContain("研究运行摘要读取失败");
    expect(root.textContent).not.toContain("暂无研究 Run");
    expect(requests.filter((request) => request === "GET /runs")).toHaveLength(3);
  });
  it("研究摘要出现后数据层导航不被固定高度容器压缩，仍能切换假设", async () => {
    const { root, app } = await mount(DemandRadar, { "/demand/signals": [signal], "/runs": [run] });
    const tabs = root.querySelector<HTMLElement>('nav[aria-label="需求雷达数据层"]')!;
    expect(tabs.style.flexShrink).toBe("0");
    [...tabs.querySelectorAll("button")].find((button) => button.textContent?.includes("需求假设"))!.click();
    await settle();
    expect(root.querySelector(".radar-content")!.textContent).toContain("Need Hypothesis");
    expect(root.querySelector(".radar-content")!.textContent).not.toContain("Demand Signal");
    app.unmount();
  });
  it.each([new TypeError("network"), new Response("{}", { status: 503 })])("POST结果未知时只读刷新，不自动重试或声称未执行 %s", async (failure) => {
    const { root, requests, app } = await mount(CommandCenter, {
      "/commands/discovery-proposals": proposal,
      "/commands/discovery-proposals/dpr_controlled/confirm": failure,
      "/commands/discovery-proposals/dpr_controlled": { ...proposal, state: "confirmed", can_confirm: false },
    });
    const input = root.querySelector("textarea")!;
    input.value = "只研究美国铰链";
    input.dispatchEvent(new Event("input"));
    root.querySelector("form")!.dispatchEvent(new Event("submit"));
    await settle();
    const confirm = [...root.querySelectorAll("button")].find((button) => button.textContent?.includes("确认并启动"))!;
    confirm.click();
    await settle();
    expect(root.textContent).toContain("提交结果待核实");
    expect(confirm.disabled).toBe(true);
    expect(root.querySelector(".control-note")!.textContent).not.toContain("未执行");
    [...root.querySelectorAll("button")].find((button) => button.textContent?.includes("刷新提案状态"))!.click();
    await settle();
    expect(root.querySelector(".proposal-state")!.textContent).toContain("已确认");
    expect(requests.filter((request) => request.endsWith("/confirm"))).toHaveLength(1);
    app.unmount();
  });
  it.each(["confirm", "reject"])("POST成功后GET失败仍保留已提交事实 %s", async (action) => {
    const { root, app } = await mount(CommandCenter, {
      "/commands/discovery-proposals": proposal,
      "/commands/discovery-proposals/dpr_controlled": new TypeError("network"),
      [`/commands/discovery-proposals/dpr_controlled/${action}`]: action === "confirm"
        ? { proposal_id: "dpr_controlled", directive_id: "dir_controlled", run_id: "run_controlled", workflow_type: "demand_discovery" }
        : { proposal_id: "dpr_controlled", state: "rejected" },
    });
    const input = root.querySelector("textarea")!;
    input.value = "只研究美国铰链";
    input.dispatchEvent(new Event("input"));
    root.querySelector("form")!.dispatchEvent(new Event("submit"));
    await settle();
    [...root.querySelectorAll("button")].find((button) => button.textContent?.includes(action === "confirm" ? "确认并启动" : "拒绝，不执行"))!.click();
    await settle();
    expect(root.querySelector(".proposal-state")!.textContent).toContain(action === "confirm" ? "已确认" : "已拒绝");
    expect(root.textContent).toContain("刷新提案状态");
    expect(root.textContent).not.toContain("决定未提交");
    if (action === "confirm") expect(root.textContent).toContain("run_controlled");
    app.unmount();
  });
  it.each([null, "budget_missing", "not_configured"])("确认前展示真实预算且遵守后端禁用依据 %s", async (reason) => {
    const { root, requests, app } = await mount(CommandCenter, { "/commands/discovery-proposals": { ...proposal, can_confirm: reason === null, confirmation_blocked_reason: reason } });
    const input = root.querySelector("textarea")!;
    input.value = "只研究美国铰链";
    input.dispatchEvent(new Event("input"));
    root.querySelector("form")!.dispatchEvent(new Event("submit"));
    await settle();
    expect(root.textContent).toContain("只研究");
    expect(root.textContent).toContain("进口商候选");
    expect(root.textContent).toContain("总页面读取上限");
    expect(root.querySelector(".caps-card")!.textContent).toContain("7");
    expect(root.textContent).not.toContain("Campaign未设置");
    expect(root.textContent).toContain("US hinges importer");
    const confirm = [...root.querySelectorAll("button")].find((button) => button.textContent?.includes("确认并启动"))!;
    expect(confirm.disabled).toBe(reason !== null);
    expect(requests.filter((value) => value.endsWith("/confirm"))).toEqual([]);
    app.unmount();
  });

  it("需求雷达保留目录待核验证据而不称采购确认", async () => {
    const { root, app } = await mount(DemandRadar, { "/demand/signals": [signal], "/runs": [run] });
    expect(root.textContent).toContain("免费额度耗尽");
    expect(root.textContent).toContain("进口商候选");
    expect(root.textContent).toContain("目录收录");
    expect(root.textContent).toContain("待核验");
    expect(root.textContent).toContain("不代表运输记录");
    expect(root.textContent).not.toContain("已验证采购需求");
    expect(root.querySelector('a[href="https://example.test/about"]')).not.toBeNull();
    app.unmount();
  });

  it("客户发现显示同租户研究证据并提供只研究入口", async () => {
    const account = { account_id: "acc_controlled", name: "Synthetic", country: "US", website_domain: "example.test", source_signal_refs: ["sig_controlled"], research_signals: [signal] };
    const { root, app } = await mount(CustomerDiscovery, { "/runs": [run], "/prospects/accounts": [account], "/prospects/accounts/acc_controlled": { account, contacts: [] } });
    expect(root.textContent).toContain("免费额度耗尽");
    root.querySelector<HTMLElement>('.account-list li[role="button"]')!.click();
    await settle();
    expect(root.textContent).toContain("只研究");
    expect(root.textContent).toContain("进口商候选");
    expect(root.textContent).toContain("待核验");
    app.unmount();
  });

  it("Run区别额度耗尽与无结果，并以持久预留显示用量", async () => {
    const { root, app } = await mount(RunCenter, { "/runs": [run], "/runs/run_controlled": { summary: run, steps: [], tool_calls: [], artifacts: [], approvals: [] } });
    expect(root.textContent).toContain("免费额度耗尽");
    expect(root.textContent).toContain("已消耗 1");
    expect(root.textContent).toContain("未决预留 5");
    expect(root.textContent).toContain("计划线路");
    expect(root.textContent).toContain("已留证线路");
    expect(root.textContent).not.toContain("没有买家");
    app.unmount();
  });

  it("设置明确首次配置未核实及运行时未证实", async () => {
    const { root, app } = await mount(SettingsCenter, { "/settings/research": access });
    expect(root.textContent).toContain("已配置，账户尚未核实");
    expect(root.textContent).toContain("运行时激活尚未证实");
    expect(root.textContent).toContain("模型与基础设施仍可能产生费用");
    app.unmount();
  });
});
