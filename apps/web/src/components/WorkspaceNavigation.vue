<script setup lang="ts">
import { computed, inject, onBeforeUnmount, onMounted, ref } from "vue";
import { apiClient, createApiClient } from "../api/client";
import { useQuoteRequestScope } from "../views/costing-quotes/quote-request-scope";
import { RouterLink, useRoute } from "vue-router";
import { sectionForPath, workspaceForPath, workspaceGroups } from "../navigation";

const props = defineProps<{ level: "primary" | "secondary" }>();
const route = useRoute();
const client = inject<ReturnType<typeof createApiClient>>("tradeos-api-client", apiClient);
const employeeRole = ref<string | null>(null);
let mounted = false;
onBeforeUnmount(() => { mounted = false; });
const gate = useQuoteRequestScope(client, () => [], () => {
  employeeRole.value = null;
  if (mounted) globalThis.queueMicrotask(() => void loadRole());
});
async function loadRole(): Promise<void> {
  if (props.level !== "secondary" || !gate.hasIdentity.value) return;
  const operation = gate.begin("navigation-role");
  if (!operation?.valid()) return;
  try {
    const result = await client.GET("/auth/session", { signal: operation.signal, cache: "no-store" });
    if (!operation.valid()) return;
    const identity = client.identitySnapshot().identity;
    employeeRole.value = result.response.ok && result.data?.employee.is_active
      && result.data.employee.employee_id === identity?.employeeId
      && result.data.employee.tenant_id === identity?.tenantId
      ? result.data.employee.role : null;
  } catch { if (operation.valid()) employeeRole.value = null; }
}
onMounted(() => { mounted = true; void loadRole(); });
const canReadSupply = computed(() => employeeRole.value !== null
  && ["boss", "product", "sourcing", "finance"].includes(employeeRole.value));
const group = computed(() => workspaceForPath(route.path));
const section = computed(() => sectionForPath(route.path));
const links = computed(() => props.level === "primary"
  ? workspaceGroups
  : group.value.links.filter(link => link.to !== "/products" || canReadSupply.value));
</script>

<template>
  <nav
    :aria-label="level === 'primary' ? '主导航' : `${group.label}页面`"
    :class="['workspace-navigation', level]"
  >
    <RouterLink
      v-for="link in links"
      :key="link.to"
      :to="link.to"
      :class="{ 'is-current': link.to === (level === 'primary' ? group.to : section?.to) }"
      :aria-current="link.to === (level === 'primary' ? group.to : section?.to) ? 'page' : undefined"
    >
      {{ link.label }}
    </RouterLink>
  </nav>
</template>

<style scoped>
.workspace-navigation { display: flex; gap: 6px; min-width: 0; overflow-x: auto; white-space: nowrap; }
.workspace-navigation a { flex-shrink: 0; padding: 8px 12px; border-radius: 6px; text-decoration: none; line-height: 1.5; }
.primary a { color: var(--topbar-text); opacity: .85; }
.primary a:hover, .primary a.is-current { background: rgba(255, 255, 255, .14); opacity: 1; }
.primary a.is-current { font-weight: 700; }
.secondary { flex-shrink: 0; padding: 8px var(--gutter); border-bottom: 1px solid var(--border); background: var(--surface); }
.secondary a { color: var(--text-secondary); }
.secondary a:hover { background: var(--canvas); }
.secondary a.is-current { background: var(--fact-soft); color: var(--fact); font-weight: 700; }
@media (max-width: 700px) {
  .primary { order: 3; flex-basis: 100%; }
  .workspace-navigation a { padding: 8px 10px; }
}
</style>
