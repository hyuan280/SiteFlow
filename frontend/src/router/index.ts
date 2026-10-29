import { createRouter, createWebHistory } from "vue-router";

import MainLayout from "@/components/layout/MainLayout.vue";
import { useAuthStore } from "@/stores/auth";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: "/login",
      name: "login",
      component: () => import("@/views/LoginView.vue"),
    },
    {
      path: "/",
      component: MainLayout,
      meta: { requiresAuth: true },
      children: [
        { path: "", redirect: "/dashboard" },
        {
          path: "dashboard",
          name: "dashboard",
          component: () => import("@/views/DashboardView.vue"),
        },
        {
          path: "analytics",
          name: "analytics",
          component: () => import("@/views/AnalyticsView.vue"),
        },
        {
          path: "runs",
          name: "runs",
          component: () => import("@/views/RunsView.vue"),
        },
        {
          path: "runs/:runId/lanes/:laneKey",
          name: "run-lane",
          component: () => import("@/views/RunLaneView.vue"),
        },
        {
          path: "browsers",
          name: "browsers",
          component: () => import("@/views/BrowserView.vue"),
        },
        {
          path: "features",
          name: "features",
          component: () => import("@/views/FeaturesView.vue"),
        },
        {
          path: "templates",
          name: "templates",
          component: () => import("@/views/TemplatesView.vue"),
        },
        {
          path: "sites",
          name: "sites",
          component: () => import("@/views/SitesView.vue"),
        },
        {
          path: "notifications",
          name: "notifications",
          component: () => import("@/views/NotificationsView.vue"),
        },
        {
          path: "plugins",
          name: "plugins",
          component: () => import("@/views/PluginsView.vue"),
        },
        {
          path: "settings",
          name: "settings",
          component: () => import("@/views/SettingsView.vue"),
        },
      ],
    },
    {
      path: "/:pathMatch(.*)*",
      redirect: "/dashboard",
    },
  ],
});

router.beforeEach(async (to) => {
  const auth = useAuthStore();
  if (!auth.initialized) {
    await auth.fetchMe();
  }
  if (to.matched.some((record) => record.meta.requiresAuth) && !auth.isAuthenticated) {
    return { name: "login", query: { redirect: to.fullPath } };
  }
  if (to.name === "login" && auth.isAuthenticated) {
    return { name: "dashboard" };
  }
  return true;
});

export default router;
