import { createRouter, createWebHistory } from "vue-router";

const router = createRouter({
  history: createWebHistory(),
  routes: [
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
