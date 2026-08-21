import { createRouter, createWebHistory } from "vue-router";

const router = createRouter({
  history: createWebHistory(),
  routes: [
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
  ],
});

export default router;
