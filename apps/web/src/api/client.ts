import createClient, { type ClientOptions } from "openapi-fetch";

import type { paths } from "./api";

export function createApiClient(options: ClientOptions = {}) {
  return createClient<paths>({
    baseUrl: import.meta.env.VITE_API_BASE_URL ?? "",
    ...options,
  });
}

export const apiClient = createApiClient();
