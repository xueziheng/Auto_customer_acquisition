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
  publishIdentity(null);
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
    if (import.meta.env.PROD) {
      throw new WebIdentityError("authenticated_identity_missing");
    }
    return null;
  },
};

function identityMiddleware(provider: WebIdentityProvider): Middleware {
  return {
    onRequest({ request }): Request {
      return bindIdentity(request, provider);
    },
  };
}

function bindIdentity(request: Request, provider: WebIdentityProvider): Request {
  const identity = provider.current();
  const headers = new Headers(request.headers);
  headers.delete("X-Tenant-Id");
  headers.delete("X-Employee-Id");
  if (identity) {
    headers.set("X-Tenant-Id", identity.tenantId);
    headers.set("X-Employee-Id", identity.employeeId);
  }
  return new Request(request, { headers });
}

export function createApiClient(
  options: ClientOptions = {},
  identityProvider: WebIdentityProvider = runtimeIdentityProvider,
) {
  const client = createClient<paths>({
    baseUrl: import.meta.env.VITE_API_BASE_URL ?? "",
    ...options,
  });
  client.use(identityMiddleware(identityProvider));
  const baseUrl = options.baseUrl ?? import.meta.env.VITE_API_BASE_URL ?? globalThis.location.origin;
  const transport = options.fetch ?? globalThis.fetch;
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
    const data = response.status === 201
      ? await response.json() as WorkUploadView
      : undefined;
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
