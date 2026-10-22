import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { createPlatformClient } from "./platform-auth";

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("navigator", { locks: { request: async (_name: string, _options: unknown, callback: () => Promise<void>) => callback() } });
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

const session = (username: string) => ({
  username, display_name: username, role: "platform_admin",
  csrf_token: "synthetic-csrf", expires_at: "2099-01-01T00:00:00Z",
});

it("旧平台请求的迟到401不能撤销新登录身份", async () => {
  let finish!: (response: Response) => void;
  const client = createPlatformClient(async (input, init) => {
    const path = String(input);
    if (path.endsWith("/auth/login")) return Response.json(session(JSON.parse(String(init?.body)).username));
    return new Promise<Response>(resolve => { finish = resolve; });
  });
  await client.login("first", "synthetic-password");
  const old = client.overview().catch(() => null);
  await client.login("second", "synthetic-password");
  finish(new Response(null, { status: 401 }));
  await old;
  expect(client.snapshot().identity?.username).toBe("second");
});
