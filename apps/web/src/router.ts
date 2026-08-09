import { createRouter, createWebHistory } from "vue-router";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: "/crm/opportunities",
      name: "crm-opportunities",
      component: () => import("./views/crm/OpportunityList.vue"),
    },
  ],
});

export default router;
