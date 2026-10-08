import { createRouter, createWebHistory } from "vue-router";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: "/platform",
      name: "platform-console",
      component: () => import("./components/PlatformConsole.vue"),
    },
    {
      path: "/",
      redirect: "/crm/handoffs",
    },
    {
      path: "/commands",
      name: "command-center",
      component: () => import("./views/command-center/CommandCenter.vue"),
    },
    {
      path: "/demand",
      name: "demand-radar",
      component: () => import("./views/demand-radar/DemandRadar.vue"),
    },
    {
      path: "/demand/needs/:needId",
      name: "validated-need-detail",
      component: () => import("./views/demand-radar/ValidatedNeedDetail.vue"),
    },
    {
      path: "/prospects/accounts",
      name: "customer-discovery",
      component: () => import("./views/customer-discovery/CustomerDiscovery.vue"),
    },
    {
      path: "/campaigns",
      name: "campaign-center",
      component: () => import("./views/campaigns/CampaignCenter.vue"),
    },
    {
      path: "/inbox",
      name: "smart-inbox",
      component: () => import("./views/inbox/SmartInbox.vue"),
    },
    {
      path: "/inbox/mailbox",
      name: "my-mailbox",
      component: () => import("./views/inbox/MyMailbox.vue"),
    },
    {
      path: "/approvals",
      name: "approval-center",
      component: () => import("./views/approvals/ApprovalCenter.vue"),
    },
    {
      path: "/crm/opportunities",
      name: "crm-opportunities",
      component: () => import("./views/crm/OpportunityList.vue"),
    },
    {
      path: "/crm/handoffs",
      name: "crm-handoffs",
      component: () => import("./views/crm/HandoffQueue.vue"),
    },
    {
      path: "/crm/handoffs/:handoffId",
      name: "crm-handoff-detail",
      component: () => import("./views/crm/HandoffQueue.vue"),
    },
    {
      path: "/crm/outreach",
      name: "crm-outreach",
      component: () => import("./views/OutreachWorkbench.vue"),
    },
    {
      path: "/crm/sending-identities",
      name: "crm-sending-identities",
      component: () => import("./views/SendingIdentityCenter.vue"),
    },
    {
      path: "/notifications",
      name: "notifications",
      component: () => import("./views/NotificationCenter.vue"),
    },
    {
      path: "/products",
      name: "products",
      component: () => import("./views/products/ProductSupplyCenter.vue"),
      meta: { phase: "phase2-sourcing-automation", operation: "products" },
    },
    {
      path: "/sourcing",
      name: "sourcing",
      component: () => import("./views/sourcing/SourcingCenter.vue"),
      meta: { phase: "phase2-sourcing-automation", operation: "sourcing" },
    },
    {
      path: "/sourcing/:caseId",
      name: "sourcing-case-detail",
      component: () => import("./views/sourcing/SourcingCaseDetail.vue"),
    },
    {
      path: "/costing-quotes",
      name: "costing-quotes",
      component: () => import("./views/costing-quotes/CostingQuotes.vue"),
      meta: { phase: "phase2-costing-quotation", operation: "costing-quotes" },
    },
    {
      path: "/costing-quotes/quotes/:quoteId",
      name: "costing-quote-version",
      component: () => import("./views/costing-quotes/CostingQuotes.vue"),
      meta: { phase: "phase2-costing-quotation", operation: "costing-quotes" },
    },
    {
      path: "/team",
      name: "team",
      component: () => import("./views/team/TeamCenter.vue"),
    },
    {
      path: "/work-uploads",
      name: "work-uploads",
      component: () => import("./views/work-uploads/WorkUploads.vue"),
    },
    {
      path: "/commitments",
      name: "commitments",
      component: () => import("./views/commitments/CommitmentCenter.vue"),
    },
    {
      path: "/runs",
      name: "runs",
      component: () => import("./views/runs/RunCenter.vue"),
    },
    {
      path: "/settings",
      name: "settings",
      component: () => import("./views/settings/SettingsCenter.vue"),
    },
    {
      path: "/billing",
      name: "billing",
      component: () => import("./views/billing/BillingUnavailable.vue"),
      meta: { phase: "phase3-disabled" },
    },
  ],
});

export default router;
