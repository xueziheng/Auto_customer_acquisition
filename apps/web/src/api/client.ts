import createClient, {
  type ClientOptions,
  type Middleware,
} from "openapi-fetch";

import type { components, paths } from "./api";

type RawArtifactKind = components["schemas"]["RawArtifactKind"];
type WorkSourceKind = components["schemas"]["WorkSourceKind"];
type WorkUploadView = components["schemas"]["WorkUploadView"];

export interface WorkArtifactUpload {
  artifactKind: RawArtifactKind;
  body: Blob;
  customerTimezone: string;
  occurredAt: string;
  sourceKind: WorkSourceKind;
}

export interface WorkArtifactUploadResult {
  data?: WorkUploadView;
  response: Response;
}

export class WebIdentityError extends Error {}

export interface WebRequestIdentity {
  employeeId: string;
  mode: "authenticated" | "fixed-dev";
  tenantId: string;
}

export interface WebIdentityProvider {
  current(): WebRequestIdentity | null;
  generation(): number;
  subscribe(listener: () => void): () => void;
}

export interface WebIdentitySnapshot {
  readonly identity: Readonly<WebRequestIdentity> | null;
  readonly generation: number;
}

export interface ApiIdentityReader {
  identitySnapshot(): WebIdentitySnapshot;
  subscribeIdentity(listener: () => void): () => void;
}

let authenticatedIdentity: WebRequestIdentity | null = null;
let identityGeneration = 0;
let csrfToken: string | null = null;
export function configureSessionCsrf(value: string | null): void { csrfToken = value; }
export function sessionCsrf(): string | null { return csrfToken; }
export function currentIdentity(): WebRequestIdentity | null { return runtimeIdentityProvider.current(); }
export function apiBaseUrl(): string {
  return import.meta.env.PROD ? "/api" : import.meta.env.VITE_API_BASE_URL || "";
}
const identityListeners = new Set<() => void>();

function publishIdentity(identity: WebRequestIdentity | null): void {
  if (!Number.isSafeInteger(identityGeneration + 1)) throw new WebIdentityError("identity_generation_invalid");
  authenticatedIdentity = identity;
  identityGeneration += 1;
  let failed = false;
  for (const listener of [...identityListeners]) {
    try { listener(); } catch { failed = true; }
  }
  if (failed) throw new WebIdentityError("identity_notification_failed");
}

function exactIdentity(value: string, field: string): string {
  if (!value || value !== value.trim() || value.length > 128) {
    throw new WebIdentityError(`${field}_invalid`);
  }
  return value;
}

export function configureAuthenticatedIdentity(
  tenantId: string,
  employeeId: string,
): void {
  publishIdentity(Object.freeze({
    employeeId: exactIdentity(employeeId, "employee_identity"),
    mode: "authenticated",
    tenantId: exactIdentity(tenantId, "tenant_identity"),
  }));
}

export function clearAuthenticatedIdentity(): void {
  csrfToken = null;
  publishIdentity(null);
}

export interface ControlledWebConfig {
  owner: string;
  tenantId: string;
  identities: readonly { employeeId: string; label: string }[];
}

export function controlledWebConfig(): ControlledWebConfig | null {
  const raw = import.meta.env.VITE_CONTROLLED_CONFIG;
  if (!import.meta.env.DEV || import.meta.env.PROD || !raw) return null;
  try {
    const value = JSON.parse(raw);
    if (!/^[a-f0-9]{32}$/.test(value.owner) || !Array.isArray(value.identities) || value.identities.length < 1 || value.identities.length > 10) throw new Error();
    exactIdentity(value.tenantId, "tenant_identity");
    const seen = new Set<string>();
    for (const identity of value.identities) {
      exactIdentity(identity.employeeId, "employee_identity");
      if (typeof identity.label !== "string" || !identity.label || identity.label.length > 100 || seen.has(identity.employeeId)) throw new Error();
      seen.add(identity.employeeId);
    }
    return Object.freeze({ owner: value.owner, tenantId: value.tenantId, identities: Object.freeze(value.identities.map((i: {employeeId: string; label: string}) => Object.freeze({employeeId: i.employeeId, label: i.label}))) });
  } catch { throw new WebIdentityError("controlled_configuration_invalid"); }
}

export function configureControlledIdentity(employeeId: string): void {
  const config = controlledWebConfig();
  if (!config || !config.identities.some((i) => i.employeeId === employeeId)) throw new WebIdentityError("controlled_identity_rejected");
  publishIdentity(Object.freeze({ employeeId, mode: "fixed-dev", tenantId: config.tenantId }));
}

const runtimeIdentityProvider: WebIdentityProvider = {
  generation: () => identityGeneration,
  subscribe(listener) {
    identityListeners.add(listener);
    return () => { identityListeners.delete(listener); };
  },
  current(): WebRequestIdentity | null {
    if (authenticatedIdentity) return authenticatedIdentity;
    const tenantId = import.meta.env.VITE_TENANT_ID;
    const employeeId = import.meta.env.VITE_EMPLOYEE_ID;
    if (import.meta.env.DEV && tenantId && employeeId) {
      return {
        employeeId: exactIdentity(employeeId, "employee_identity"),
        mode: "fixed-dev",
        tenantId: exactIdentity(tenantId, "tenant_identity"),
      };
    }
    if (tenantId || employeeId) {
      throw new WebIdentityError("partial_dev_identity_rejected");
    }
    return null;
  },
};

function identityMiddleware(provider: WebIdentityProvider): Middleware {
  const generations = new WeakMap<Request, number>();
  return {
    onRequest({ request }): Request {
      const bound = bindIdentity(request, provider);
      generations.set(request, provider.generation());
      generations.set(bound, provider.generation());
      return bound;
    },
    onResponse({ request, response }): Response {
      checkResponse(response, generations.get(request) ?? -1, provider);
      return response;
    },
  };
}

function bindIdentity(request: Request, provider: WebIdentityProvider): Request {
  const identity = provider.current();
  const headers = new Headers(request.headers);
  headers.delete("X-Tenant-Id");
  headers.delete("X-Employee-Id");
  if (identity?.mode === "fixed-dev") {
    headers.set("X-Tenant-Id", identity.tenantId);
    headers.set("X-Employee-Id", identity.employeeId);
  }
  if (identity?.mode === "authenticated") {
    headers.set("X-TradeOS-Request", "1");
    if (!["GET", "HEAD", "OPTIONS"].includes(request.method) && csrfToken) headers.set("X-CSRF-Token", csrfToken);
  }
  return new Request(request, { headers, credentials: "same-origin" });
}

function checkResponse(response: Response, generation: number, provider: WebIdentityProvider): void {
  if (generation !== provider.generation()) throw new WebIdentityError("stale_identity_response");
  if (response.status === 401 && provider === runtimeIdentityProvider) clearAuthenticatedIdentity();
}

export function createApiClient(
  options: ClientOptions = {},
  identityProvider: WebIdentityProvider = runtimeIdentityProvider,
) {
  const client = createClient<paths>({
    baseUrl: apiBaseUrl(),
    credentials: "same-origin",
    ...options,
  });
  client.use(identityMiddleware(identityProvider));
  const baseUrl = options.baseUrl ?? (apiBaseUrl() || globalThis.location.origin);
  const transport = options.fetch ?? ((request: Request) => globalThis.fetch(request));
  const RequestConstructor = options.Request ?? Request;

  async function uploadWorkArtifact(
    upload: WorkArtifactUpload,
  ): Promise<WorkArtifactUploadResult> {
    const query = new URLSearchParams({
      artifact_kind: upload.artifactKind,
      customer_timezone: upload.customerTimezone,
      occurred_at: upload.occurredAt,
      source_kind: upload.sourceKind,
    });
    const generation = identityProvider.generation();
    const request = bindIdentity(
      new RequestConstructor(
        `${baseUrl.replace(/\/$/, "")}/work-uploads?${query.toString()}`,
        {
          body: upload.body,
          headers: {
            "Content-Type": upload.body.type || "application/octet-stream",
          },
          method: "POST",
        },
      ),
      identityProvider,
    );
    const response = await transport(request);
    checkResponse(response, generation, identityProvider);
    const data = response.status === 201
      ? await response.json() as WorkUploadView
      : undefined;
    if (data && generation !== identityProvider.generation()) throw new WebIdentityError("stale_identity_response");
    return { data, response };
  }

  function identitySnapshot(): WebIdentitySnapshot {
    try {
      const generation = identityProvider.generation();
      if (!Number.isSafeInteger(generation) || generation < 0) throw new Error();
      const current = identityProvider.current();
      if (current && current.mode !== "authenticated" && current.mode !== "fixed-dev") throw new Error();
      const identity = current ? Object.freeze({
        employeeId: exactIdentity(current.employeeId, "employee_identity"),
        tenantId: exactIdentity(current.tenantId, "tenant_identity"),
        mode: current.mode,
      }) : null;
      return Object.freeze({ identity, generation });
    } catch {
      throw new WebIdentityError("identity_snapshot_invalid");
    }
  }

  return Object.assign(client, {
    uploadWorkArtifact,
    identitySnapshot,
    subscribeIdentity: (listener: () => void) => identityProvider.subscribe(listener),
  });
}

export const apiClient = createApiClient();
