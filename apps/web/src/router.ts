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
  ],
});

export default router;
